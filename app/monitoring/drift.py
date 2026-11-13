"""Deterministic, threshold-based drift detection (product.txt 13).

Four families of signals, each a comparison of a current value against a reference:
- quality:     latest agent evaluation run vs the stored baseline report (same dataset/mode only)
- retrieval:   latest retrieval evaluation vs the retrieval baseline (recall@k, hit rate, MRR,
               source-distribution shift as total variation distance)
- data:        corpus state now vs the previous check (size change, ingestion failures, mixed
               embedding models or chunking configurations, a changed question set)
- operational: executions in the current window vs the previous window (p95 latency, timeouts,
               failures, provider errors, retries, tokens per execution, cost per success)

Every breach is labelled with its evidence:
- measured:  enough samples on both sides and the inputs are comparable; this is a real signal.
- suspected: the threshold is crossed but the sample is small or the inputs are not strictly
             comparable (different dataset or configuration). Recorded, never presented as fact.
Signals that cannot be computed are "insufficient_data" and raise nothing.
"""

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation.report import RunReport
from app.evaluation.retrieval_eval import RetrievalReport

Category = Literal["quality", "retrieval", "data", "operational"]
SignalStatus = Literal["ok", "drift", "insufficient_data"]
Evidence = Literal["measured", "suspected"]


class DriftThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # quality (agent evaluation)
    min_eval_cases: int = 10
    max_pass_rate_drop: float = 0.05
    max_category_drop: float = 0.25
    max_quality_score_drop: float = 0.1
    zero_tolerance_evaluators: list[str] = ["unauthorized_tools", "workspace_effects"]
    # retrieval (retrieval-only evaluation)
    min_retrieval_cases: int = 10
    max_recall_drop: float = 0.05
    max_hit_rate_drop: float = 0.05
    max_mrr_drop: float = 0.05
    max_answer_coverage_drop: float = 0.05
    max_source_shift: float = 0.25  # total variation distance between source distributions
    # data
    max_corpus_change: float = 0.25  # relative change in latest-document count since last check
    max_ingestion_failure_rate: float = 0.1
    # operational
    window_hours: float = 24.0
    min_executions: int = 20
    max_p95_latency_increase: float = 0.5
    max_timeout_rate: float = 0.05
    max_failure_rate: float = 0.1
    max_provider_error_rate: float = 0.05
    max_retry_rate: float = 0.1
    max_tokens_increase: float = 0.3


class Signal(BaseModel):
    category: Category
    metric: str
    status: SignalStatus
    evidence: Evidence | None = None  # set when status == "drift"
    severity: Literal["critical", "warning", "info"] | None = None
    baseline: float | None = None
    current: float | None = None
    delta: float | None = None
    threshold: str = ""
    samples: int | None = None
    message: str = ""


class DriftReport(BaseModel):
    status: Literal["ok", "drift", "suspected", "insufficient_data"]
    signals: list[Signal]
    references: dict[str, Any] = Field(default_factory=dict)  # what was compared with what
    snapshot: dict[str, Any] = Field(default_factory=dict)  # data state for the next check


def load_drift_thresholds(path: Path | None) -> DriftThresholds:
    return DriftThresholds.model_validate_json(path.read_text()) if path else DriftThresholds()


def _d(base: float | None, head: float | None) -> float | None:
    return None if base is None or head is None else round(head - base, 4)


def _signal(
    category: Category,
    metric: str,
    base: float | None,
    head: float | None,
    *,
    breached: bool | None,
    threshold: str,
    samples: int | None,
    comparable: bool = True,
    enough: bool = True,
    critical: bool = False,
    message: str = "",
) -> Signal:
    if breached is None or base is None or head is None:
        return Signal(
            category=category,
            metric=metric,
            status="insufficient_data",
            baseline=base,
            current=head,
            threshold=threshold,
            samples=samples,
            message=message or "not enough data to compare",
        )
    if not breached:
        return Signal(
            category=category,
            metric=metric,
            status="ok",
            baseline=base,
            current=head,
            delta=_d(base, head),
            threshold=threshold,
            samples=samples,
            message=message,
        )
    measured = comparable and enough
    why = []
    if not comparable:
        why.append("inputs not strictly comparable")
    if not enough:
        why.append(f"small sample ({samples})")
    return Signal(
        category=category,
        metric=metric,
        status="drift",
        evidence="measured" if measured else "suspected",
        severity=("critical" if critical else "warning") if measured else "info",
        baseline=base,
        current=head,
        delta=_d(base, head),
        threshold=threshold,
        samples=samples,
        message=(message + (f" ({'; '.join(why)})" if why else "")).strip(),
    )


# --- quality ------------------------------------------------------------------------------------


def quality_signals(
    base: RunReport | None, head: RunReport | None, t: DriftThresholds
) -> list[Signal]:
    if base is None or head is None:
        return [
            Signal(
                category="quality",
                metric="pass_rate",
                status="insufficient_data",
                message="no baseline or no completed evaluation run to compare",
            )
        ]
    b, h = base.aggregate, head.aggregate
    n = int(h.get("total") or 0)
    enough = n >= t.min_eval_cases
    differences = [
        f"dataset {base.dataset.sha256[:8]} vs {head.dataset.sha256[:8]}"
        if base.dataset.sha256 != head.dataset.sha256
        else "",
        *(
            f"{key} {base.config.get(key)} vs {head.config.get(key)}"
            for key in ("mode", "case_filter")
            if base.config.get(key) != head.config.get(key)
        ),
    ]
    differences = [d for d in differences if d]
    comparable = not differences
    note = "; ".join(differences)
    signals = []
    bp, hp = b.get("pass_rate"), h.get("pass_rate")
    signals.append(
        _signal(
            "quality",
            "pass_rate",
            bp,
            hp,
            breached=None if bp is None or hp is None else hp - bp < -t.max_pass_rate_drop,
            threshold=f"drop <= {t.max_pass_rate_drop}",
            samples=n,
            comparable=comparable,
            enough=enough,
            critical=True,
            message=f"pass rate {bp} -> {hp}" + (f"; {note}" if note else ""),
        )
    )
    for name in sorted(set(b.get("by_category", {})) & set(h.get("by_category", {}))):
        bv = b["by_category"][name]["pass_rate"]
        hv = h["by_category"][name]["pass_rate"]
        group = int(h["by_category"][name]["total"])
        signals.append(
            _signal(
                "quality",
                f"category.{name}",
                bv,
                hv,
                breached=None if bv is None or hv is None else hv - bv < -t.max_category_drop,
                threshold=f"drop <= {t.max_category_drop}",
                samples=group,
                comparable=comparable,
                enough=enough,
                message=f"{name} pass rate {bv} -> {hv}",
            )
        )
    for name in t.zero_tolerance_evaluators:
        bv = b.get("by_evaluator", {}).get(name, {}).get("pass_rate")
        hv = h.get("by_evaluator", {}).get(name, {}).get("pass_rate")
        signals.append(
            _signal(
                "quality",
                f"evaluator.{name}",
                bv,
                hv,
                breached=None if bv is None or hv is None else hv < bv,
                threshold="no drop (safety property)",
                samples=h.get("by_evaluator", {}).get(name, {}).get("applicable"),
                comparable=comparable,
                enough=True,  # a single unsafe action is a real signal at any sample size
                critical=True,
                message=f"{name} {bv} -> {hv}",
            )
        )
    bq, hq = b.get("quality_score"), h.get("quality_score")
    signals.append(
        _signal(
            "quality",
            "judge_quality_score",
            bq,
            hq,
            breached=None if bq is None or hq is None else hq - bq < -t.max_quality_score_drop,
            threshold=f"drop <= {t.max_quality_score_drop} (advisory LLM judge)",
            samples=h.get("judged_cases"),
            # The judge is a model, not ground truth: a drop is never more than a suspicion.
            comparable=False,
            enough=enough,
            message=f"judge quality {bq} -> {hq}",
        )
    )
    return signals


# --- retrieval ----------------------------------------------------------------------------------


def total_variation(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) | set(b)
    return round(0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys), 4)


def retrieval_signals(
    base: RetrievalReport | None, head: RetrievalReport | None, t: DriftThresholds
) -> list[Signal]:
    if base is None or head is None:
        return [
            Signal(
                category="retrieval",
                metric="recall_at_k",
                status="insufficient_data",
                message="no retrieval baseline or no retrieval evaluation run to compare",
            )
        ]
    b, h = base.metrics, head.metrics
    n = int(h.get("cases") or 0)
    enough = n >= t.min_retrieval_cases
    comparable = base.dataset.get("sha256") == head.dataset.get("sha256") and b.get("k") == h.get(
        "k"
    )
    changed = {
        k: (base.config.get(k), head.config.get(k))
        for k in sorted(set(base.config) | set(head.config))
        if base.config.get(k) != head.config.get(k)
    }
    cause = ("; config changed: " + json.dumps(changed, default=str)) if changed else ""
    signals = []
    for metric, limit in (
        ("recall_at_k", t.max_recall_drop),
        ("hit_rate", t.max_hit_rate_drop),
        ("mrr", t.max_mrr_drop),
        ("answer_coverage", t.max_answer_coverage_drop),
    ):
        bv, hv = b.get(metric), h.get(metric)
        signals.append(
            _signal(
                "retrieval",
                metric,
                bv,
                hv,
                breached=None if bv is None or hv is None else hv - bv < -limit,
                threshold=f"drop <= {limit}",
                samples=n,
                comparable=comparable,
                enough=enough,
                critical=metric in ("recall_at_k", "answer_coverage"),
                message=f"{metric} {bv} -> {hv}{cause}",
            )
        )
    bd, hd = b.get("source_distribution"), h.get("source_distribution")
    shift = total_variation(bd, hd) if bd and hd else None
    signals.append(
        _signal(
            "retrieval",
            "source_distribution_shift",
            0.0 if shift is not None else None,
            shift,
            breached=None if shift is None else shift > t.max_source_shift,
            threshold=f"total variation <= {t.max_source_shift}",
            samples=n,
            comparable=comparable,
            enough=enough,
            message=f"share of retrieved chunks per file moved by {shift}",
        )
    )
    return signals


# --- data ---------------------------------------------------------------------------------------


def data_signals(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    t: DriftThresholds,
    *,
    dataset_sha: str | None = None,
    baseline_dataset_sha: str | None = None,
) -> list[Signal]:
    signals = []
    rate = current.get("recent_failure_rate")
    signals.append(
        _signal(
            "data",
            "ingestion_failure_rate",
            0.0 if rate is not None else None,
            rate,
            breached=None if rate is None else rate > t.max_ingestion_failure_rate,
            threshold=f"<= {t.max_ingestion_failure_rate}",
            samples=current.get("recent_ingestions"),
            enough=(current.get("recent_ingestions") or 0) >= 5,
            message=f"{current.get('recent_ingestion_failures')} of "
            f"{current.get('recent_ingestions')} recent ingestions failed",
        )
    )
    for key, label in (("embedding_models", "embedding model"), ("chunking_configs", "chunking")):
        configs = current.get(key) or {}
        mixed = current.get(f"mixed_{key}") or {}
        signals.append(
            _signal(
                "data",
                f"mixed_{key}",
                0.0,
                float(len(mixed)) if configs else None,
                breached=bool(mixed) if configs else None,
                threshold="one per tenant corpus",
                samples=sum(configs.values()) if configs else 0,
                critical=key == "embedding_models",
                message=f"tenants with a mixed {label} corpus: {mixed}"
                if mixed
                else f"one {label} per tenant corpus; overall {configs}",
            )
        )
    before = (previous or {}).get("latest_documents")
    now = current.get("latest_documents")
    change = None if not before or now is None else round((now - before) / before, 4)
    signals.append(
        _signal(
            "data",
            "corpus_size_change",
            float(before) if before else None,
            float(now) if now is not None else None,
            breached=None if change is None else abs(change) > t.max_corpus_change,
            threshold=f"relative change <= {t.max_corpus_change} since the last check",
            samples=now,
            message=f"latest documents {before} -> {now} ({change:+.0%})"
            if change is not None
            else "no previous check to compare with",
        )
    )
    if dataset_sha and baseline_dataset_sha:
        same = dataset_sha == baseline_dataset_sha
        signals.append(
            Signal(
                category="data",
                metric="question_set_changed",
                status="ok" if same else "drift",
                evidence=None if same else "suspected",
                severity=None if same else "info",
                threshold="golden dataset matches the baseline's",
                message="golden dataset unchanged"
                if same
                else f"golden dataset changed ({baseline_dataset_sha[:8]} -> {dataset_sha[:8]}): "
                "quality comparisons against the baseline are not valid until it is re-run",
            )
        )
    return signals


# --- operational --------------------------------------------------------------------------------


def operational_signals(
    previous: dict[str, Any], current: dict[str, Any], t: DriftThresholds
) -> list[Signal]:
    n, n0 = current.get("executions") or 0, previous.get("executions") or 0
    enough = n >= t.min_executions and n0 >= t.min_executions
    samples = n
    signals = []
    bp, hp = previous.get("latency_ms", {}).get("p95"), current.get("latency_ms", {}).get("p95")
    signals.append(
        _signal(
            "operational",
            "p95_latency_ms",
            bp,
            hp,
            breached=None if not bp or hp is None else hp > bp * (1 + t.max_p95_latency_increase),
            threshold=f"increase <= {t.max_p95_latency_increase:.0%}",
            samples=samples,
            enough=enough,
            message=f"p95 latency {bp} -> {hp} ms",
        )
    )
    for metric, limit in (
        ("timeout_rate", t.max_timeout_rate),
        ("failure_rate", t.max_failure_rate),
        ("provider_error_rate", t.max_provider_error_rate),
        ("retry_rate", t.max_retry_rate),
    ):
        bv, hv = previous.get(metric), current.get(metric)
        signals.append(
            _signal(
                "operational",
                metric,
                bv if bv is not None else (0.0 if hv is not None else None),
                hv,
                breached=None if hv is None else hv > limit,
                threshold=f"<= {limit}",
                samples=samples,
                enough=n >= t.min_executions,
                critical=metric == "timeout_rate",
                message=f"{metric} {bv} -> {hv}",
            )
        )
    bt = previous.get("tokens", {}).get("per_execution")
    ht = current.get("tokens", {}).get("per_execution")
    signals.append(
        _signal(
            "operational",
            "tokens_per_execution",
            bt,
            ht,
            breached=None if not bt or ht is None else ht > bt * (1 + t.max_tokens_increase),
            threshold=f"increase <= {t.max_tokens_increase:.0%}",
            samples=samples,
            enough=enough,
            message=f"tokens per execution {bt} -> {ht}",
        )
    )
    return signals


def overall(signals: list[Signal]) -> Literal["ok", "drift", "suspected", "insufficient_data"]:
    if any(s.status == "drift" and s.evidence == "measured" for s in signals):
        return "drift"
    if any(s.status == "drift" for s in signals):
        return "suspected"
    if not any(s.status == "ok" for s in signals):
        return "insufficient_data"  # nothing could be measured: never report that as healthy
    return "ok"
