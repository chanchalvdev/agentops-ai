"""Baseline vs optimized benchmark from recorded evaluation reports (product.txt 12).

Every number comes from a stored run; nothing is estimated. An optimization "wins" only when it
is faster or cheaper (tokens are the real cost of a local model) without unacceptable quality
loss, judged with the same regression thresholds as the CI gate.
"""

from typing import Any

from app.evaluation.regression import Thresholds, compare
from app.evaluation.report import RunReport


def row(report: RunReport) -> dict[str, Any]:
    a = report.aggregate
    return {
        "profile": report.config.get("profile") or "(unnamed)",
        "run_id": str(report.run_id),
        "cases": a.get("total"),
        "success_rate": a.get("pass_rate"),
        "quality_score": a.get("quality_score"),
        "p50_ms": (a.get("latency_ms") or {}).get("p50"),
        "p95_ms": (a.get("latency_ms") or {}).get("p95"),
        "model_calls_per_request": a.get("model_calls_mean"),
        "tool_calls_per_request": a.get("tool_calls_mean"),
        "tokens_per_request": a.get("tokens_per_request"),
        "tokens_per_successful_task": a.get("tokens_per_successful_task"),
        "cost_per_request_usd": a.get("cost_per_request_usd"),
        "cost_per_successful_task_usd": a.get("cost_per_successful_task_usd"),
        "hosted_equivalent_per_success_usd": (a.get("hosted_equivalent") or {}).get(
            "per_successful_task_usd"
        ),
        "retry_rate": a.get("retry_rate"),
        "cache_hits": a.get("cache_hits"),
    }


def _change(base: float | None, head: float | None) -> float | None:
    return None if not base or head is None else round((head - base) / base, 4)


def verdict(base: RunReport, head: RunReport, t: Thresholds) -> dict[str, Any]:
    b, h = row(base), row(head)
    cmp = compare(base, head, t)
    # Latency regressions are what we are measuring here, not a quality breach.
    quality_breaches = [x for x in cmp.breaches if not x.startswith("p95 latency")]
    gains = {
        "p50": _change(b["p50_ms"], h["p50_ms"]),
        "p95": _change(b["p95_ms"], h["p95_ms"]),
        "tokens_per_request": _change(b["tokens_per_request"], h["tokens_per_request"]),
        "model_calls_per_request": _change(
            b["model_calls_per_request"], h["model_calls_per_request"]
        ),
    }
    improved = any(v is not None and v < -0.05 for v in gains.values())
    return {
        "base": b["profile"],
        "head": h["profile"],
        "relative_change": gains,
        "quality_breaches": quality_breaches,
        "newly_failing": cmp.newly_failing,
        "newly_passing": cmp.newly_passing,
        "successful": improved and not quality_breaches,
    }


def _fmt(v: Any, pct: bool = False) -> str:
    if v is None:
        return "n/a"
    if pct:
        return f"{v:.0%}"
    if isinstance(v, float):
        return f"{v:,.1f}" if abs(v) >= 1 else f"{v:.3f}"
    return str(v)


def markdown(reports: list[RunReport], t: Thresholds) -> str:
    rows = [row(r) for r in reports]
    lines = [
        "| profile | success | quality (judge) | p50 s | p95 s | model calls/req | "
        "tool calls/req | tokens/req | tokens/success | cost/req | cost/success |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['profile']} | {_fmt(r['success_rate'], True)} | {_fmt(r['quality_score'])} | "
            f"{_fmt((r['p50_ms'] or 0) / 1000)} | {_fmt((r['p95_ms'] or 0) / 1000)} | "
            f"{_fmt(r['model_calls_per_request'])} | {_fmt(r['tool_calls_per_request'])} | "
            f"{_fmt(r['tokens_per_request'])} | {_fmt(r['tokens_per_successful_task'])} | "
            f"${_fmt(r['cost_per_request_usd'])} | ${_fmt(r['cost_per_successful_task_usd'])} |"
        )
    if len(reports) > 1:
        lines += ["", f"Verdicts against `{rows[0]['profile']}`:"]
        for head in reports[1:]:
            v = verdict(reports[0], head, t)
            change = ", ".join(
                f"{k} {_fmt(x, True) if x is not None else 'n/a'}"
                for k, x in v["relative_change"].items()
            )
            lines.append(
                f"- **{v['head']}**: {'SUCCESS' if v['successful'] else 'NOT ACCEPTED'} "
                f"({change}); quality breaches: {v['quality_breaches'] or 'none'}; "
                f"newly failing: {v['newly_failing'] or 'none'}; "
                f"newly passing: {v['newly_passing'] or 'none'}"
            )
    lines += [
        "",
        "Local models cost $0 per token (pricing source `local`); tokens and latency are the real "
        "cost on this hardware. Hosted-equivalent cost is reported only when reference prices are "
        "configured, and is labelled hypothetical.",
    ]
    return "\n".join(lines)
