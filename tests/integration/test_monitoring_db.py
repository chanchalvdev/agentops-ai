"""Day 4 on Postgres: corpus versioning for cache keys, per-case cost persistence, retrieval
evaluation, drift checks with alert records, operational metrics, admin-only monitoring, rate
limiting, and the experiments API."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.evaluation.dataset import Dataset, EvalCase
from app.evaluation.retrieval_eval import run_retrieval_eval
from app.monitoring.alerts import MonitoringService
from app.services import Services
from tests.conftest import KEY_B

pytestmark = pytest.mark.integration

DR = (
    "# Disaster recovery\n\nThe recovery point objective (RPO) is 5 minutes.\n\n"
    "# Escalation\n\nThe incident commander pages the communications lead."
)


def dataset(cases: list[dict[str, Any]]) -> Dataset:
    parsed = [EvalCase.model_validate({"dataset_version": "t1", **c}) for c in cases]
    return Dataset(name="golden_questions", version="t1", sha256="s" * 64, cases=parsed)


@pytest.fixture
def corpus(tmp_path: Path, services: Services) -> Path:
    d = tmp_path / "corpus"
    d.mkdir()
    (d / "dr.md").write_text(DR)
    (d / "other.md").write_text("# Cafeteria\n\nLunch is served at noon on weekdays.")
    services.settings.eval_corpus_dirs = [d]
    return d


async def test_corpus_version_changes_with_the_corpus(services: Services, ingest: Any) -> None:
    v0 = await services.ingestion.corpus_version("tenant_a")
    doc = await ingest("dr.md", DR)
    v1 = await services.ingestion.corpus_version("tenant_a")
    assert v1 != v0
    assert await services.ingestion.corpus_version("tenant_b") == v0.split(":")[0] + ":-"
    await services.ingestion.delete("tenant_a", doc.id)
    assert await services.ingestion.corpus_version("tenant_a") != v1


async def test_retrieval_eval_scores_and_persists(services: Services, corpus: Path) -> None:
    ds = dataset(
        [
            {
                "id": "rpo",
                "category": "answerable",
                "question": "What is the recovery point objective RPO?",
                "expected_sources": ["dr.md"],
                "must_contain": [["5 minutes"], ["900 seconds"]],  # 900 s is not in the doc
            },
            {"id": "skip", "category": "unanswerable", "question": "Who is the CEO?"},
        ]
    )
    report = await run_retrieval_eval(services, ds, k=2, requested_by="tenant_a")
    m = report.metrics
    assert m["cases"] == 1 and m["hit_rate"] == 1.0 and m["recall_at_k"] == 1.0
    assert m["answer_coverage"] == 1.0  # only the group the document states is counted
    assert report.cases[0].answer_groups == 1
    assert report.config["eval_tenant"] == services.settings.eval_tenant

    check = await MonitoringService(services).check(
        "tenant_a", retrieval_base=report, retrieval_head=report
    )
    retrieval = [s for s in check.report.signals if s.category == "retrieval"]
    assert {s.status for s in retrieval} == {"ok"}


async def test_answer_coverage_ignores_matches_in_unrelated_documents(
    services: Services, corpus: Path
) -> None:
    (corpus / "other.md").write_text("# Cafeteria\n\nLunch is served 5 minutes after the bell.")
    ds = dataset(
        [
            {
                "id": "lunch",
                "category": "answerable",
                "question": "When is lunch served in the cafeteria?",
                "expected_sources": ["dr.md"],
                "must_contain": [["5 minutes"]],
            }
        ]
    )
    services.retriever.default_mode = "keyword"
    report = await run_retrieval_eval(services, ds, k=1, persist=False)
    case = report.cases[0]
    assert case.retrieved == ["other.md"]
    assert case.answer_groups == 1 and case.answer_covered == 0  # "5 minutes" came from other.md


async def test_drift_check_detects_a_retrieval_regression_and_records_alerts(
    services: Services, corpus: Path, client: httpx.AsyncClient
) -> None:
    ds = dataset(
        [
            {
                "id": f"c{i}",
                "category": "answerable",
                "question": "What is the recovery point objective RPO?",
                "expected_sources": ["dr.md"],
                "must_contain": [["5 minutes"]],
            }
            for i in range(10)
        ]
    )
    base = await run_retrieval_eval(services, ds, k=2, persist=False)
    services.retriever.default_mode = "keyword"  # deliberately broken: no keyword overlap below
    broken_ds = dataset(
        [
            {**c.model_dump(exclude={"dataset_version"}), "question": "objective of recovery?"}
            for c in ds.cases
        ]
    )
    broken_ds.sha256 = ds.sha256  # same dataset identity, degraded configuration
    services.retriever.min_vector_score = 0.99
    head = await run_retrieval_eval(services, broken_ds, k=1, persist=False)
    head.metrics["k"] = base.metrics["k"]
    head.metrics["answer_coverage"] = 0.0  # keep the assertion independent of fake vectors

    check = await MonitoringService(services).check(
        "tenant_a", retrieval_base=base, retrieval_head=head
    )
    assert check.report.status == "drift"
    alert = next(a for a in check.alerts if a.metric == "answer_coverage")
    assert alert.evidence == "measured" and alert.severity == "critical"
    assert "config changed" in alert.message

    latest = (await client.get("/api/v1/monitoring/drift")).json()
    assert latest["id"] == str(check.id) and latest["report"]["status"] == "drift"
    alerts = (await client.get("/api/v1/monitoring/alerts")).json()
    assert any(a["metric"] == "answer_coverage" for a in alerts)


async def test_monitoring_is_operator_only(client: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", "/api/v1/monitoring/summary"),
        ("GET", "/api/v1/monitoring/drift"),
        ("POST", "/api/v1/monitoring/check"),
        ("GET", "/api/v1/experiments"),
        ("GET", "/api/v1/evaluations/runs"),
    ):
        response = await client.request(method, path, headers={"X-API-Key": KEY_B})
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "permission_denied"


async def test_check_endpoint_and_summary(client: httpx.AsyncClient) -> None:
    check = await client.post("/api/v1/monitoring/check")
    assert check.status_code == 200, check.text
    body = check.json()
    assert body["report"]["status"] in ("ok", "suspected", "drift", "insufficient_data")
    categories = {s["category"] for s in body["report"]["signals"]}
    assert categories == {"quality", "retrieval", "data", "operational"}
    summary = (await client.get("/api/v1/monitoring/summary")).json()
    assert summary["drift_status"] == body["report"]["status"]
    assert "operational" in summary and "data" in summary


async def test_metrics_count_the_callers_executions_only(
    client: httpx.AsyncClient, ingest: Any
) -> None:
    await ingest("dr.md", DR)
    run = await client.post("/api/v1/agent/run", json={"question": "What is the RPO?"})
    assert run.status_code == 200, run.text
    mine = (await client.get("/api/v1/metrics")).json()
    assert mine["tenant_id"] == "tenant_a" and mine["executions"] == 1
    assert mine["success_rate"] == 1.0 and mine["cost_usd"] == 0.0
    assert mine["model_calls_per_execution"] >= 1
    theirs = (await client.get("/api/v1/metrics", headers={"X-API-Key": KEY_B})).json()
    assert theirs["executions"] == 0


async def test_rate_limit_returns_429_with_retry_after(
    client: httpx.AsyncClient, services: Services
) -> None:
    services.rate_limiter.limits["agent"] = 1
    first = await client.post("/api/v1/agent/run", json={"question": "anything"})
    assert first.status_code == 200
    second = await client.post("/api/v1/agent/run", json={"question": "anything"})
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "too_many_requests"
    assert int(second.headers["Retry-After"]) >= 1
    # Other tenants have their own bucket.
    other = await client.post(
        "/api/v1/agent/run", json={"question": "anything"}, headers={"X-API-Key": KEY_B}
    )
    assert other.status_code == 200


async def test_experiments_api_lists_profiles_and_runs_retrieval(
    client: httpx.AsyncClient, services: Services, corpus: Path, tmp_path: Path
) -> None:
    profiles = tmp_path / "experiments.json"
    profiles.write_text(
        json.dumps(
            {
                "version": "t",
                "profiles": [
                    {"name": "base", "description": "defaults", "overrides": {}},
                    {
                        "name": "vector",
                        "description": "vector only",
                        "overrides": {"retrieval_mode": "vector"},
                    },
                ],
            }
        )
    )
    services.settings.experiments_file = profiles
    listed = (await client.get("/api/v1/experiments")).json()
    assert [p["name"] for p in listed["profiles"]] == ["base", "vector"]
    missing = await client.post("/api/v1/experiments/run", json={"profile": "nope"})
    assert missing.status_code == 404
    bad = await client.post("/api/v1/experiments/run", json={"profile": "../etc"})
    assert bad.status_code == 422


async def test_eval_results_persist_cost_and_usage(services: Services, corpus: Path) -> None:
    from app.evaluation.runner import EvaluationRunner

    ds = dataset(
        [
            {
                "id": "rpo",
                "category": "answerable",
                "question": "What is the RPO?",
                "expected_status": ["answered"],
            }
        ]
    )
    runner = EvaluationRunner(services)
    report = await runner.run(ds, requested_by="tenant_a", profile="base")
    stored = await runner.store.report("tenant_a", report.run_id)
    case = stored.cases[0]
    assert case.cost_usd == 0.0 and case.usage is not None
    assert case.usage.model_calls == case.model_calls
    assert stored.config["profile"] == "base" and "tuning" in stored.config
    a = stored.aggregate
    assert a["cost_per_request_usd"] == 0.0
    assert a["hosted_equivalent"]["total_usd"] is None  # no reference prices configured
