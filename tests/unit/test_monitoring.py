"""Day 4 monitoring: drift signals (measured vs suspected), retrieval metrics, rate limiting,
semantic cache scoping, experiment profiles, and the benchmark verdict."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.agents.result import AgentRunResult
from app.agents.semantic_cache import SemanticCache, cacheable, cosine
from app.core.budgets import ExecutionLimits
from app.core.config import Settings
from app.core.errors import InvalidRequestError
from app.core.execution_context import ExecutionContext, LedgerEvent
from app.core.ratelimit import RateLimiter
from app.evaluation.regression import Thresholds
from app.evaluation.report import CaseReport, DatasetInfo, RunReport, aggregate
from app.evaluation.retrieval_eval import (
    RetrievalReport,
    answer_coverage,
    retrieval_metrics,
    score_case,
)
from app.experiments.benchmark import markdown, verdict
from app.experiments.configurations import Profile, apply, load_profiles
from app.llm.schemas import Usage
from app.monitoring.drift import (
    DriftThresholds,
    data_signals,
    operational_signals,
    overall,
    quality_signals,
    retrieval_signals,
    total_variation,
)

T = DriftThresholds()


def run_report(
    passed: list[bool],
    *,
    sha: str = "abc",
    mode: str = "graph",
    profile: str | None = None,
    latency: float = 1000.0,
    tokens: int = 1000,
) -> RunReport:
    cases = [
        CaseReport(
            id=f"c{i}",
            category="answerable",
            passed=p,
            status="answered",
            latency_ms=latency,
            input_tokens=tokens,
            model_calls=4,
            evaluators=[],
        )
        for i, p in enumerate(passed)
    ]
    return RunReport(
        run_id=uuid.uuid4(),
        dataset=DatasetInfo(name="golden", version="v1", sha256=sha, cases=len(cases)),
        mode=mode,
        config={"mode": mode, "case_filter": {"ids": None, "categories": None}, "profile": profile},
        started_at=datetime.now(UTC),
        aggregate=aggregate(cases),
        cases=cases,
    )


def retrieval_report(coverage: float, *, sha: str = "abc", cases: int = 17) -> RetrievalReport:
    return RetrievalReport(
        dataset={"name": "golden", "version": "v1", "sha256": sha},
        config={"mode": "hybrid", "chunk_size_chars": 1200},
        metrics={
            "cases": cases,
            "k": 5,
            "hit_rate": 1.0,
            "recall_at_k": 1.0,
            "mrr": 0.95,
            "answer_coverage": coverage,
            "source_distribution": {"a.md": 0.5, "b.md": 0.5},
        },
        cases=[],
    )


def by_metric(signals: list[Any], metric: str) -> Any:
    return next(s for s in signals if s.metric == metric)


# --- quality ------------------------------------------------------------------------------------


def test_quality_drop_on_comparable_runs_is_measured_drift() -> None:
    base = run_report([True] * 20)
    head = run_report([True] * 15 + [False] * 5)
    sig = by_metric(quality_signals(base, head, T), "pass_rate")
    assert sig.status == "drift" and sig.evidence == "measured" and sig.severity == "critical"
    assert sig.delta == -0.25


def test_quality_drop_on_a_different_dataset_is_only_suspected() -> None:
    base = run_report([True] * 20)
    head = run_report([True] * 15 + [False] * 5, sha="changed")
    sig = by_metric(quality_signals(base, head, T), "pass_rate")
    assert sig.status == "drift" and sig.evidence == "suspected" and sig.severity == "info"
    assert "not strictly comparable" in sig.message


def test_quality_drop_on_a_small_sample_is_only_suspected() -> None:
    sig = by_metric(
        quality_signals(run_report([True] * 4), run_report([True, False, False, False]), T),
        "pass_rate",
    )
    assert sig.evidence == "suspected" and "small sample" in sig.message


def test_no_run_means_insufficient_data_not_ok() -> None:
    sig = quality_signals(None, run_report([True]), T)[0]
    assert sig.status == "insufficient_data"
    assert overall([sig]) == "insufficient_data"  # nothing measured is not healthy


def test_stable_quality_is_ok() -> None:
    signals = quality_signals(run_report([True] * 20), run_report([True] * 20), T)
    assert by_metric(signals, "pass_rate").status == "ok"
    assert overall(signals) == "ok"


# --- retrieval ----------------------------------------------------------------------------------


def test_retrieval_answer_coverage_drop_is_detected() -> None:
    signals = retrieval_signals(retrieval_report(1.0), retrieval_report(0.9412), T)
    sig = by_metric(signals, "answer_coverage")
    assert sig.status == "drift" and sig.evidence == "measured" and sig.severity == "critical"
    assert by_metric(signals, "recall_at_k").status == "ok"
    assert overall(signals) == "drift"


def test_retrieval_config_change_is_named_in_the_message() -> None:
    head = retrieval_report(0.5)
    head.config = {"mode": "vector", "chunk_size_chars": 250}
    sig = by_metric(retrieval_signals(retrieval_report(1.0), head, T), "answer_coverage")
    assert "config changed" in sig.message and "vector" in sig.message


def test_total_variation_distance() -> None:
    assert total_variation({"a": 1.0}, {"a": 1.0}) == 0
    assert total_variation({"a": 1.0}, {"b": 1.0}) == 1.0
    assert total_variation({"a": 0.5, "b": 0.5}, {"a": 0.75, "b": 0.25}) == 0.25


def test_retrieval_case_scoring() -> None:
    c = score_case("x", "answerable", ["a.md", "b.md"], ["c.md", "a.md", "a.md"], 12.0, (2, 1))
    assert c.hit and c.recall == 0.5 and c.reciprocal_rank == 0.5
    m = retrieval_metrics([c], 3)
    assert m["answer_coverage"] == 0.5 and m["answer_misses"] == ["x"]
    assert m["source_distribution"] == {"a.md": 0.6667, "c.md": 0.3333}
    miss = score_case("y", "answerable", ["a.md"], ["c.md"], 1.0)
    assert not miss.hit and miss.reciprocal_rank == 0.0


def test_answer_coverage_ignores_values_the_documents_never_state() -> None:
    groups = [["5 minutes", "5 min"], ["300 seconds"]]  # 300 s is computed, not in the doc
    source = "The RPO is 5  Minutes using WAL streaming."
    assert answer_coverage(groups, source, "rpo is 5 minutes") == (1, 1)
    assert answer_coverage(groups, source, "unrelated passage") == (1, 0)


# --- data and operational -----------------------------------------------------------------------


def test_mixed_embedding_models_are_critical() -> None:
    current = {
        "recent_failure_rate": 0.0,
        "recent_ingestions": 10,
        "recent_ingestion_failures": 0,
        "embedding_models": {"nomic-embed-text": 5, "other": 1},
        "mixed_embedding_models": {"tenant_a": {"nomic-embed-text": 5, "other": 1}},
        "chunking_configs": {"structured:1200:200": 4, "fixed:250:0": 2},
        "mixed_chunking_configs": {},  # two configs, but each tenant uses one (e.g. an eval tenant)
        "latest_documents": 6,
    }
    signals = data_signals(current, {"latest_documents": 5}, T)
    sig = by_metric(signals, "mixed_embedding_models")
    assert sig.status == "drift" and sig.severity == "critical"
    assert "tenant_a" in sig.message
    assert by_metric(signals, "mixed_chunking_configs").status == "ok"
    assert by_metric(signals, "corpus_size_change").status == "ok"  # +20% < 25%


def test_changed_question_set_is_a_suspicion() -> None:
    signals = data_signals({}, None, T, dataset_sha="new", baseline_dataset_sha="old")
    sig = by_metric(signals, "question_set_changed")
    assert sig.status == "drift" and sig.evidence == "suspected"


def window(n: int, p95: float, timeouts: float = 0.0) -> dict[str, Any]:
    return {
        "executions": n,
        "latency_ms": {"p95": p95},
        "timeout_rate": timeouts,
        "failure_rate": 0.0,
        "provider_error_rate": 0.0,
        "retry_rate": 0.0,
        "tokens": {"per_execution": 3000},
    }


def test_operational_latency_and_timeouts() -> None:
    signals = operational_signals(window(30, 40_000), window(30, 70_000, timeouts=0.1), T)
    assert by_metric(signals, "p95_latency_ms").evidence == "measured"
    assert by_metric(signals, "timeout_rate").severity == "critical"
    few = operational_signals(window(3, 40_000), window(3, 70_000), T)
    assert by_metric(few, "p95_latency_ms").evidence == "suspected"


# --- rate limiting ------------------------------------------------------------------------------


def test_rate_limiter_refuses_then_refills() -> None:
    now = [0.0]
    limiter = RateLimiter({"agent": 2}, clock=lambda: now[0])
    assert limiter.acquire("a", "agent") is None
    assert limiter.acquire("a", "agent") is None
    wait = limiter.acquire("a", "agent")
    assert wait is not None and 29 < wait <= 30  # 2/min -> one token every 30 s
    assert limiter.acquire("b", "agent") is None  # per tenant
    now[0] = 30.0
    assert limiter.acquire("a", "agent") is None
    assert limiter.rejected == 1


# --- semantic cache -----------------------------------------------------------------------------


class Embed:
    model, dim = "fake", 3

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    async def embed_query(self, text: str) -> list[float]:
        return self.vectors[text]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.vectors[t] for t in texts]


def ctx(tenant: str = "tenant_a") -> ExecutionContext:
    return ExecutionContext(
        tenant_id=tenant,
        limits=ExecutionLimits(),
        allowed_tools=frozenset({"document_search"}),
    )


def result(**kw: Any) -> AgentRunResult:
    defaults: dict[str, Any] = dict(
        execution_id=uuid.uuid4(),
        request_id="r",
        mode="graph",
        status="answered",
        answer="RPO is 5 minutes [S1].",
        citations=[],
        tool_references=[],
        verification="supported",
        steps=1,
        model_calls=1,
        tool_calls=1,
        corrections={},
        usage=Usage(),
        cost_usd=Decimal(0),
        total_ms=1.0,
        ledger=[LedgerEvent(seq=0, kind="tool_call", name="document_search", status="ok")],
    )
    return AgentRunResult(**{**defaults, **kw})


async def test_semantic_cache_scopes_and_threshold() -> None:
    vectors = {
        "What is the RPO?": [1.0, 0.0, 0.0],
        "What's the RPO?": [0.99, 0.01, 0.0],  # paraphrase, cosine ~0.9999
        "What is the RTO?": [0.8, 0.6, 0.0],  # cosine 0.8: different question
    }
    version = {"tenant_a": "1", "tenant_b": "1"}

    async def corpus(tenant: str) -> str:
        return version[tenant]

    cache = SemanticCache(Embed(vectors), corpus, "cfg", min_similarity=0.97)  # type: ignore[arg-type]
    hit, scope, vec = await cache.lookup(ctx(), "graph", "What is the RPO?")
    assert hit is None
    assert cache.store(scope, vec, "What is the RPO?", result())

    hit, _, _ = await cache.lookup(ctx(), "graph", "What's the RPO?")
    assert hit is not None and hit.similarity >= 0.97
    assert (await cache.lookup(ctx(), "graph", "What is the RTO?"))[0] is None
    assert (await cache.lookup(ctx("tenant_b"), "graph", "What is the RPO?"))[0] is None
    assert (await cache.lookup(ctx(), "loop", "What is the RPO?"))[0] is None
    version["tenant_a"] = "2"  # a document changed: the cached answer is unreachable
    assert (await cache.lookup(ctx(), "graph", "What is the RPO?"))[0] is None


def test_only_verified_read_only_answers_are_cacheable() -> None:
    assert cacheable(result())
    assert not cacheable(result(verification="unavailable"))
    assert not cacheable(result(status="insufficient_evidence"))
    assert not cacheable(result(mode="loop", verification=None))
    write = LedgerEvent(seq=1, kind="tool_call", name="workspace_write", status="ok")
    assert not cacheable(result(ledger=[write]))
    approval = LedgerEvent(seq=1, kind="approval", name="workspace_exec", status="ok")
    assert not cacheable(result(ledger=[approval]))
    assert cosine([1, 0], [0, 1]) == 0


# --- experiments --------------------------------------------------------------------------------


def test_profiles_cannot_override_unsafe_settings() -> None:
    with pytest.raises(ValueError, match="not overridable"):
        Profile(name="bad", description="x", overrides={"workspace_root": "/"})
    with pytest.raises(ValueError, match="eval_tenant"):
        Profile(name="bad", description="x", overrides={"chunk_size_chars": 300})


def test_committed_profiles_load_and_apply() -> None:
    profiles = load_profiles(Path("datasets/experiments.json"))
    assert {"baseline", "optimized", "degraded-chunking"} <= set(profiles)
    s = apply(Settings(env="test"), profiles["optimized"])
    assert s.agent_rule_routing and s.cache_tool_results and s.agent_query_rewrite
    assert not apply(Settings(env="test"), profiles["optimized-no-rewrite"]).agent_query_rewrite
    with pytest.raises(InvalidRequestError):
        load_profiles(Path("datasets/golden_questions.jsonl"))


def test_benchmark_verdict_requires_a_gain_without_quality_loss() -> None:
    base = run_report([True] * 20, profile="baseline", latency=30_000, tokens=4000)
    faster = run_report([True] * 20, profile="optimized", latency=20_000, tokens=3000)
    worse = run_report([True] * 18 + [False] * 2, profile="cheap", latency=10_000, tokens=1000)
    t = Thresholds()
    assert verdict(base, faster, t)["successful"]
    v = verdict(base, worse, t)
    assert not v["successful"] and v["quality_breaches"] and v["newly_failing"] == ["c18", "c19"]
    table = markdown([base, faster, worse], t)
    assert "| optimized | 100%" in table and "NOT ACCEPTED" in table
