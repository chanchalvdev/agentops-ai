"""Evaluation report: per-case results, aggregate metrics, and a readable summary.

The same model is written to JSON by the CLI, stored in the database, and compared by the
regression check, so a baseline file and a stored run are interchangeable.
"""

import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.evaluation.evaluators import EvaluatorResult
from app.monitoring.cost import HOSTED_EQUIVALENT_LABEL, UsageSummary


class CaseReport(BaseModel):
    id: str
    category: str
    passed: bool | None  # None = skipped (case does not support this mode)
    status: str
    execution_id: uuid.UUID | None = None
    latency_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    cost_usd: float | None = 0.0
    usage: UsageSummary | None = None  # retries, embedding calls, cache hits (from the ledger)
    evaluators: list[EvaluatorResult] = Field(default_factory=list)
    judge: dict[str, Any] | None = None
    failures: list[str] = Field(default_factory=list)
    answer: str | None = None


class DatasetInfo(BaseModel):
    name: str
    version: str
    sha256: str
    cases: int


class RunReport(BaseModel):
    run_id: uuid.UUID
    dataset: DatasetInfo
    mode: str
    config: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None = None
    aggregate: dict[str, Any] = Field(default_factory=dict)
    cases: list[CaseReport] = Field(default_factory=list)


def _rate(passed: int, total: int) -> float | None:
    return round(passed / total, 4) if total else None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 1)


def _per(total: float | None, n: int, digits: int = 6) -> float | None:
    return None if total is None or n == 0 else round(total / n, digits)


def cost_metrics(ran: list[CaseReport], passed: int) -> dict[str, Any]:
    """Cost and efficiency per request and per successful task (product.txt 12)."""
    costs = [c.cost_usd for c in ran]
    # None if any case had unknown pricing: never report a partial sum as the total
    cost = None if any(c is None for c in costs) else round(sum(c or 0 for c in costs), 6)
    tokens = sum(c.input_tokens + c.output_tokens for c in ran)
    usages = [c.usage for c in ran if c.usage is not None]
    hosted = [u.hosted_equivalent_usd for u in usages]
    hosted_total = (
        round(sum(h or 0 for h in hosted), 6)
        if usages and len(usages) == len(ran) and all(h is not None for h in hosted)
        else None
    )
    model_calls = sum(c.model_calls for c in ran)
    retries = sum(u.retries for u in usages)
    return {
        "cost_usd": cost,
        "cost_per_request_usd": _per(cost, len(ran)),
        "cost_per_successful_task_usd": _per(cost, passed),
        "tokens_per_request": _per(tokens, len(ran), 1),
        "tokens_per_successful_task": _per(tokens, passed, 1),
        "hosted_equivalent": {
            "label": HOSTED_EQUIVALENT_LABEL,
            "total_usd": hosted_total,
            "per_request_usd": _per(hosted_total, len(ran)),
            "per_successful_task_usd": _per(hosted_total, passed),
        },
        "retries": retries,
        "retry_rate": _per(retries, model_calls, 4),
        "failed_model_calls": sum(u.failed_model_calls for u in usages),
        "embedding_calls_mean": _per(sum(u.embedding_calls for u in usages), len(usages), 2),
        "cache_hits": {
            "tool": sum(u.tool_cache_hits for u in usages),
            "embedding": sum(u.embedding_cache_hits for u in usages),
            "semantic": sum(1 for u in usages if u.semantic_cache_hit),
        },
    }


def aggregate(cases: list[CaseReport]) -> dict[str, Any]:
    ran = [c for c in cases if c.passed is not None]
    by_category: dict[str, dict[str, Any]] = {}
    for category in sorted({c.category for c in ran}):
        group = [c for c in ran if c.category == category]
        ok = sum(1 for c in group if c.passed)
        by_category[category] = {
            "total": len(group),
            "passed": ok,
            "pass_rate": _rate(ok, len(group)),
        }
    applicable: dict[str, list[bool]] = defaultdict(list)
    for c in ran:
        for e in c.evaluators:
            if e.passed is not None:
                applicable[e.name].append(e.passed)
    by_evaluator = {
        name: {"applicable": len(v), "passed": sum(v), "pass_rate": _rate(sum(v), len(v))}
        for name, v in sorted(applicable.items())
    }
    judged = [c.judge for c in ran if c.judge and "scores" in c.judge]
    judge = (
        {
            criterion: round(statistics.mean(j["scores"][criterion] for j in judged), 3)
            for criterion in ("correctness", "groundedness", "relevance", "completeness")
        }
        if judged
        else None
    )
    latencies = [c.latency_ms for c in ran if c.latency_ms is not None]
    passed = sum(1 for c in ran if c.passed)
    return {
        "total": len(ran),
        "passed": passed,
        "skipped": len(cases) - len(ran),
        "pass_rate": _rate(passed, len(ran)),
        "by_category": by_category,
        "by_evaluator": by_evaluator,
        "statuses": dict(Counter(c.status for c in ran)),
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 1) if latencies else None,
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
        },
        "tokens": {
            "input": sum(c.input_tokens for c in ran),
            "output": sum(c.output_tokens for c in ran),
        },
        "model_calls_mean": round(statistics.mean(c.model_calls for c in ran), 2) if ran else None,
        "tool_calls_mean": round(statistics.mean(c.tool_calls for c in ran), 2) if ran else None,
        **cost_metrics(ran, passed),
        # Mean of the four advisory judge criteria; None without --judge (never pass rate).
        "quality_score": round(statistics.mean(judge.values()), 3) if judge else None,
        "judge": judge,
        "judged_cases": len(judged),
    }


def summary(report: RunReport) -> str:
    a = report.aggregate
    lines = [
        f"Evaluation {report.run_id}  dataset={report.dataset.name}@{report.dataset.version} "
        f"mode={report.mode} model={report.config.get('models', {}).get('main')}",
        f"Passed {a['passed']}/{a['total']} ({(a['pass_rate'] or 0):.0%})"
        + (f", skipped {a['skipped']}" if a["skipped"] else ""),
        "",
        "By category:",
        *(
            f"  {name:<16} {v['passed']:>2}/{v['total']:<2} {(v['pass_rate'] or 0):>5.0%}"
            for name, v in a["by_category"].items()
        ),
        "By evaluator (applicable cases only):",
        *(
            f"  {name:<20} {v['passed']:>2}/{v['applicable']:<2} {(v['pass_rate'] or 0):>5.0%}"
            for name, v in a["by_evaluator"].items()
        ),
        f"Latency ms: mean={a['latency_ms']['mean']} p50={a['latency_ms']['p50']} "
        f"p95={a['latency_ms']['p95']}  tokens in/out={a['tokens']['input']}/"
        f"{a['tokens']['output']}  cost_usd={a['cost_usd']}",
    ]
    if a.get("judge"):
        lines.append(
            "LLM judge (0-1, advisory, not ground truth): "
            + " ".join(f"{k}={v}" for k, v in a["judge"].items())
        )
    failed = [c for c in report.cases if c.passed is False]
    if failed:
        lines += ["", "Failures:"]
        lines += [f"  {c.id} [{c.status}]: {'; '.join(c.failures)[:300]}" for c in failed]
    return "\n".join(lines)
