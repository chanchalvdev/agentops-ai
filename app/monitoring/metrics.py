"""Operational and data metrics read from the database (product.txt 13).

Operational: executions in a time window from `agent_executions` and their ledger events
(latency percentiles, timeout/failure rates, provider errors, retries, tokens, cost per success).
Data: the document corpus (status mix, ingestion failures, embedding/chunking configurations).
Both feed GET /api/v1/metrics and the drift check; nothing here calls a model.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AgentExecution, Document, ExecutionEvent

PROVIDER_ERRORS = ("provider_error", "provider_unavailable", "provider_timeout", "rate_limited")
TERMINAL = ("answered", "insufficient_evidence")


def _rate(n: int | None, d: int | None) -> float | None:
    return round((n or 0) / d, 4) if d else None


async def operational_metrics(
    sessions: async_sessionmaker[AsyncSession],
    *,
    start: datetime,
    end: datetime,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Metrics over executions created in [start, end). tenant_id=None means all tenants."""
    where = [AgentExecution.created_at >= start, AgentExecution.created_at < end]
    if tenant_id is not None:
        where.append(AgentExecution.tenant_id == tenant_id)
    finished = AgentExecution.total_ms.is_not(None)
    async with sessions() as session:
        row = (
            await session.execute(
                select(
                    func.count(),
                    func.count().filter(AgentExecution.status.in_(TERMINAL)),
                    func.count().filter(AgentExecution.status == "deadline_exceeded"),
                    func.count().filter(AgentExecution.status == "failed"),
                    func.count().filter(AgentExecution.status == "budget_exhausted"),
                    func.percentile_cont(0.5)
                    .within_group(AgentExecution.total_ms)
                    .filter(finished),
                    func.percentile_cont(0.95)
                    .within_group(AgentExecution.total_ms)
                    .filter(finished),
                    func.coalesce(func.sum(AgentExecution.input_tokens), 0),
                    func.coalesce(func.sum(AgentExecution.output_tokens), 0),
                    func.coalesce(func.sum(AgentExecution.model_calls), 0),
                    func.coalesce(func.sum(AgentExecution.tool_calls), 0),
                    func.sum(AgentExecution.cost_usd),
                    func.count().filter(AgentExecution.cost_usd.is_(None)),
                ).where(*where)
            )
        ).one()
        (n, ok, timeouts, failed, budget, p50, p95, tin, tout, mcalls, tcalls, cost, unpriced) = row
        events = (
            await session.execute(
                select(
                    func.count().filter(
                        and_(ExecutionEvent.kind == "model_call", ExecutionEvent.status == "ok")
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    and_(
                                        ExecutionEvent.kind == "model_call",
                                        ExecutionEvent.status == "ok",
                                    ),
                                    func.greatest(
                                        func.coalesce(
                                            ExecutionEvent.data["attempts"].astext.cast(Integer), 1
                                        )
                                        - 1,
                                        0,
                                    ),
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.count().filter(
                        and_(
                            ExecutionEvent.kind == "model_call",
                            ExecutionEvent.error_code.in_(PROVIDER_ERRORS),
                        )
                    ),
                    func.count().filter(ExecutionEvent.data["cache"].astext == "hit"),
                    func.count().filter(ExecutionEvent.kind == "cache"),
                )
                .join(AgentExecution, AgentExecution.id == ExecutionEvent.execution_id)
                .where(*where)
            )
        ).one()
    ok_model_calls, retries, provider_errors, tool_cache_hits, semantic_hits = events
    tokens = int(tin) + int(tout)
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "executions": n,
        "successful": ok,
        "success_rate": _rate(ok, n),
        "timeout_rate": _rate(timeouts, n),
        "failure_rate": _rate(failed, n),
        "budget_exhausted_rate": _rate(budget, n),
        "latency_ms": {
            "p50": None if p50 is None else round(p50, 1),
            "p95": None if p95 is None else round(p95, 1),
        },
        "tokens": {"input": int(tin), "output": int(tout), "per_execution": _rate(tokens, n)},
        "model_calls_per_execution": _rate(int(mcalls), n),
        "tool_calls_per_execution": _rate(int(tcalls), n),
        "provider_errors": provider_errors,
        "provider_error_rate": _rate(provider_errors, int(mcalls)),
        "retries": int(retries),
        "retry_rate": _rate(int(retries), ok_model_calls),
        "cache_hits": {"tool": tool_cache_hits, "semantic": semantic_hits},
        # None if any execution used an unpriced model: a partial sum is not a total.
        "cost_usd": None if unpriced else float(cost or 0),
        "cost_per_successful_task_usd": None if unpriced or not ok else float(cost or 0) / ok,
    }


async def window_pair(
    sessions: async_sessionmaker[AsyncSession],
    hours: float,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(previous window, current window) of equal length ending now."""
    now = now or datetime.now(UTC)
    span = timedelta(hours=hours)
    previous = await operational_metrics(
        sessions, start=now - 2 * span, end=now - span, tenant_id=tenant_id
    )
    current = await operational_metrics(sessions, start=now - span, end=now, tenant_id=tenant_id)
    return previous, current


async def data_metrics(
    sessions: async_sessionmaker[AsyncSession],
    *,
    since: datetime,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    where = [] if tenant_id is None else [Document.tenant_id == tenant_id]
    async with sessions() as session:
        statuses = dict(
            (
                await session.execute(
                    select(Document.status, func.count()).where(*where).group_by(Document.status)
                )
            ).all()
        )
        recent = (
            await session.execute(
                select(
                    func.count(),
                    func.count().filter(Document.status == "failed"),
                ).where(*where, Document.created_at >= since)
            )
        ).one()
        # Grouped per tenant: tenants (e.g. an experiment's eval tenant) may legitimately use
        # different settings; a mix *inside* one tenant's corpus is what breaks retrieval.
        per_tenant: dict[str, dict[str, dict[str, int]]] = {}
        for key, column in (
            ("embedding_models", Document.embedding_model),
            ("chunking_configs", Document.chunking_config),
        ):
            rows = (
                await session.execute(
                    select(Document.tenant_id, column, func.count())
                    .where(*where, Document.status == "ready", Document.is_latest)
                    .group_by(Document.tenant_id, column)
                )
            ).all()
            by_tenant: dict[str, dict[str, int]] = {}
            for tenant, value, count in rows:
                by_tenant.setdefault(tenant, {})[str(value)] = count
            per_tenant[key] = by_tenant
        mime = dict(
            (
                await session.execute(
                    select(Document.mime_type, func.count())
                    .where(*where, Document.is_latest)
                    .group_by(Document.mime_type)
                )
            ).all()
        )
        chunks = await session.scalar(
            select(func.coalesce(func.sum(Document.chunk_count), 0)).where(
                *where, Document.status == "ready", Document.is_latest
            )
        )
    total = sum(statuses.values())
    return {
        "documents": total,
        "latest_documents": sum(mime.values()),
        "chunks": int(chunks or 0),
        "status": {str(getattr(k, "value", k)): v for k, v in statuses.items()},
        "recent_ingestions": recent[0],
        "recent_ingestion_failures": recent[1],
        "recent_failure_rate": _rate(recent[1], recent[0]),
        "embedding_models": _totals(per_tenant["embedding_models"]),
        "chunking_configs": _totals(per_tenant["chunking_configs"]),
        "mixed_embedding_models": _mixed(per_tenant["embedding_models"]),
        "mixed_chunking_configs": _mixed(per_tenant["chunking_configs"]),
        "mime_types": mime,
    }


def _totals(by_tenant: dict[str, dict[str, int]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for configs in by_tenant.values():
        for value, count in configs.items():
            totals[value] = totals.get(value, 0) + count
    return totals


def _mixed(by_tenant: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Tenants whose own ready corpus holds more than one configuration."""
    return {tenant: configs for tenant, configs in by_tenant.items() if len(configs) > 1}
