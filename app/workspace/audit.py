"""Audit log for workspace operations.

Every operation the jail or the executor handles is recorded here, whoever asked (the agent or a
user through the API/terminal), including rejections. Agent calls are also in the execution
ledger (execution_events); this table answers "what happened to this workspace?" on its own.
"""

import uuid
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import WorkspaceAuditEvent

Operation = Literal["list", "read", "search", "write", "edit", "exec"]
AuditStatus = Literal["ok", "rejected", "error"]


class AuditEntry(BaseModel):
    tenant_id: str
    actor: Literal["agent", "user"]
    execution_id: uuid.UUID | None = None
    key_id: str | None = None
    operation: Operation
    target: str  # relative path or the command line; never file contents
    status: AuditStatus
    error_code: str | None = None
    exit_code: int | None = None
    bytes: int | None = None
    duration_ms: float


class AuditOut(AuditEntry):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class AuditSink(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...


class MemoryAudit:
    """For unit tests and tools used without a database."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


class DbAudit:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(self, entry: AuditEntry) -> None:
        async with self._sessions() as session, session.begin():
            session.add(WorkspaceAuditEvent(**entry.model_dump()))

    async def recent(self, tenant_id: str, limit: int = 100) -> list[WorkspaceAuditEvent]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(WorkspaceAuditEvent)
                .where(WorkspaceAuditEvent.tenant_id == tenant_id)
                .order_by(WorkspaceAuditEvent.id.desc())
                .limit(limit)
            )
            return list(rows)
