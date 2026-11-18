"""Monitoring API (product.txt 13, 14): drift checks, alerts, and operational metrics.

GET /api/v1/metrics is per tenant (any caller sees its own executions). The monitoring endpoints
are system-wide (all tenants, evaluation history), so they are operator (admin) only.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import AdminDep, PrincipalDep, ServicesDep, rate_limited
from app.evaluation.retrieval_eval import list_retrieval_reports
from app.monitoring.alerts import AlertOut, CheckOut, MonitoringService
from app.monitoring.metrics import data_metrics, operational_metrics

router = APIRouter(prefix="/api/v1", tags=["monitoring"])


def _cache_stats(services: Any) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    if services.retriever.query_cache is not None:
        stats["query_embeddings"] = vars(services.retriever.query_cache.stats)
    if services.harness.tool_cache is not None:
        stats["tool_results"] = vars(services.harness.tool_cache.stats)
    if services.agents.semantic_cache is not None:
        c = services.agents.semantic_cache
        stats["semantic"] = {"hits": c.hits, "misses": c.misses}
    return stats


@router.get("/metrics", summary="Operational metrics for the caller's executions")
async def metrics(
    services: ServicesDep,
    principal: PrincipalDep,
    hours: float = Query(default=24, gt=0, le=24 * 30),
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "tenant_id": principal.tenant_id,
        **await operational_metrics(
            services.sessions,
            start=now - timedelta(hours=hours),
            end=now,
            tenant_id=principal.tenant_id,
        ),
        "process": {  # this API process only, since it started
            "caches": _cache_stats(services),
            "rate_limited_requests": services.rate_limiter.rejected,
        },
    }


@router.get("/monitoring/summary", summary="Latest drift status, open alerts, and trends")
async def summary(services: ServicesDep, _: AdminDep) -> dict[str, Any]:
    monitor = MonitoringService(services)
    latest = await monitor.latest_check()
    now = datetime.now(UTC)
    t = monitor.thresholds()
    return {
        "drift_status": latest.report.status if latest else None,
        "last_check_at": latest.created_at if latest else None,
        "open_alerts": [a.model_dump(mode="json") for a in await monitor.alerts(limit=20)],
        "evaluation_history": await monitor.eval_history(),
        "retrieval_history": [
            {
                "run_id": str(r.run_id),
                "created_at": r.created_at.isoformat(),
                "profile": r.profile,
                **{k: r.metrics.get(k) for k in ("hit_rate", "recall_at_k", "mrr", "cases")},
            }
            for r in await list_retrieval_reports(services, limit=20)
        ],
        "operational": await operational_metrics(
            services.sessions, start=now - timedelta(hours=t.window_hours), end=now
        ),
        "data": await data_metrics(services.sessions, since=now - timedelta(hours=t.window_hours)),
        "caches": _cache_stats(services),
    }


@router.get("/monitoring/drift", response_model=CheckOut | None, summary="Latest drift check")
async def drift(services: ServicesDep, _: AdminDep) -> CheckOut | None:
    return await MonitoringService(services).latest_check()


@router.get("/monitoring/alerts", response_model=list[AlertOut], summary="Alert records")
async def alerts(
    services: ServicesDep,
    _: AdminDep,
    status: str | None = Query(default="open", pattern="^(open|acknowledged)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AlertOut]:
    return await MonitoringService(services).alerts(limit=limit, status=status)


class AcknowledgeRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


@router.post(
    "/monitoring/alerts/{alert_id}/acknowledge",
    response_model=AlertOut,
    summary="Acknowledge an open alert (kept on record, removed from the open list)",
)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    services: ServicesDep,
    principal: AdminDep,
    body: AcknowledgeRequest | None = None,
) -> AlertOut:
    return await MonitoringService(services).acknowledge(
        alert_id, by=principal.tenant_id, note=body.note if body else None
    )


@router.post(
    "/monitoring/check",
    response_model=CheckOut,
    summary="Run a drift check now (reads stored runs and metrics; no model calls)",
    dependencies=[rate_limited("expensive")],
)
async def check(services: ServicesDep, principal: AdminDep) -> CheckOut:
    return await MonitoringService(services).check(principal.tenant_id)
