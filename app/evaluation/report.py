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
        # None if any case had unknown pricing: never report a partial sum as the total
        "cost_usd": None
        if any(c.cost_usd is None for c in ran)
        else round(sum(c.cost_usd or 0 for c in ran), 6),
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
