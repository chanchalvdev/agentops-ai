"""Drift checks and alert records: gather inputs, run the detector, persist the outcome.

A check compares:
- quality: the latest completed full evaluation run (same mode as the baseline) or a given
  report, against the baseline report file (settings.monitoring_eval_baseline)
- retrieval: the latest retrieval evaluation or a given one, against the retrieval baseline file
- data: the corpus now against the snapshot stored by the previous check
- operational: the current execution window against the previous window
Every breached signal becomes an alert row (measured or suspected), linked to its check.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.errors import ConflictError, NotFoundError
from app.db.models import DriftCheck, EvaluationRun, MonitoringAlert
from app.evaluation.dataset import load_dataset
from app.evaluation.regression import load_report
from app.evaluation.report import RunReport
from app.evaluation.retrieval_eval import RetrievalReport, list_retrieval_reports
from app.evaluation.store import EvaluationStore
from app.monitoring.drift import (
    DriftReport,
    DriftThresholds,
    data_signals,
    load_drift_thresholds,
    operational_signals,
    overall,
    quality_signals,
    retrieval_signals,
)
from app.monitoring.metrics import data_metrics, window_pair

if TYPE_CHECKING:
    from app.services import Services


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    check_id: uuid.UUID
    category: str
    metric: str
    severity: str
    evidence: str
    baseline_value: float | None
    current_value: float | None
    threshold: str
    message: str
    status: str
    created_at: datetime
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    ack_note: str | None = None


class CheckOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    requested_by: str
    report: DriftReport
    alerts: list[AlertOut]


def _optional_file(path: Path) -> Path | None:
    return path if path.is_file() else None


class MonitoringService:
    def __init__(self, services: "Services") -> None:
        self.s = services
        self.settings = services.settings

    def thresholds(self) -> DriftThresholds:
        return load_drift_thresholds(_optional_file(self.settings.monitoring_thresholds))

    def eval_baseline(self) -> RunReport | None:
        path = _optional_file(self.settings.monitoring_eval_baseline)
        return load_report(path) if path else None

    def retrieval_baseline(self) -> RetrievalReport | None:
        path = _optional_file(self.settings.monitoring_retrieval_baseline)
        return RetrievalReport.model_validate_json(path.read_text()) if path else None

    async def latest_eval_run(self, mode: str) -> RunReport | None:
        """Newest completed run of the whole dataset in this mode (filtered runs are partial)."""
        async with self.s.sessions() as session:
            rows = await session.scalars(
                select(EvaluationRun)
                .where(
                    EvaluationRun.status == "completed",
                    EvaluationRun.mode == mode,
                    # The server's own configuration, never an experiment profile's run.
                    EvaluationRun.config["profile"].astext.is_(None),
                )
                .order_by(EvaluationRun.started_at.desc())
                .limit(20)
            )
            candidates = [
                r
                for r in rows
                if not any((r.config.get("case_filter") or {}).values())  # full runs only
            ]
        if not candidates:
            return None
        run = candidates[0]
        return await EvaluationStore(self.s.sessions).report(run.requested_by, run.id)

    async def _previous_snapshot(self) -> dict[str, Any] | None:
        async with self.s.sessions() as session:
            row = await session.scalar(
                select(DriftCheck).order_by(DriftCheck.created_at.desc()).limit(1)
            )
        return (row.baseline or {}).get("data_snapshot") if row else None

    async def check(
        self,
        requested_by: str,
        *,
        eval_head: RunReport | None = None,
        eval_base: RunReport | None = None,
        retrieval_head: RetrievalReport | None = None,
        retrieval_base: RetrievalReport | None = None,
        persist: bool = True,
    ) -> CheckOut:
        t = self.thresholds()
        eval_base = eval_base or self.eval_baseline()
        if eval_head is None and eval_base is not None:
            eval_head = await self.latest_eval_run(eval_base.mode)
        retrieval_base = retrieval_base or self.retrieval_baseline()
        if retrieval_head is None:
            # The default "current" state is the server's own configuration, never an experiment
            # probe: a deliberately degraded profile is not a regression of the running system.
            latest = await list_retrieval_reports(self.s, limit=1, exclude_experiments=True)
            retrieval_head = latest[0] if latest else None

        now = datetime.now(UTC)
        data_now = await data_metrics(self.s.sessions, since=now - timedelta(hours=t.window_hours))
        previous_window, current_window = await window_pair(self.s.sessions, t.window_hours)
        golden = self.settings.eval_datasets_dir / "golden_questions.jsonl"
        dataset_sha = load_dataset(golden).sha256 if golden.is_file() else None

        signals = [
            *quality_signals(eval_base, eval_head, t),
            *retrieval_signals(retrieval_base, retrieval_head, t),
            *data_signals(
                data_now,
                await self._previous_snapshot(),
                t,
                dataset_sha=dataset_sha,
                baseline_dataset_sha=eval_base.dataset.sha256 if eval_base else None,
            ),
            *operational_signals(previous_window, current_window, t),
        ]
        report = DriftReport(
            status=overall(signals),
            signals=signals,
            references={
                "eval_baseline": str(eval_base.run_id) if eval_base else None,
                "eval_head": str(eval_head.run_id) if eval_head else None,
                "retrieval_baseline": str(retrieval_base.run_id) if retrieval_base else None,
                "retrieval_head": str(retrieval_head.run_id) if retrieval_head else None,
                "operational_windows": [previous_window["window"], current_window["window"]],
            },
            snapshot={"data": data_now, "operational": current_window},
        )
        check_id = uuid.uuid4()
        alerts = [
            MonitoringAlert(
                id=uuid.uuid4(),
                check_id=check_id,
                category=sig.category,
                metric=sig.metric,
                severity=sig.severity or "info",
                evidence=sig.evidence or "suspected",
                baseline_value=sig.baseline,
                current_value=sig.current,
                threshold=sig.threshold[:100],
                message=sig.message,
                status="open",
                created_at=now,
            )
            for sig in signals
            if sig.status == "drift"
        ]
        if persist:
            async with self.s.sessions() as session, session.begin():
                session.add(
                    DriftCheck(
                        id=check_id,
                        requested_by=requested_by,
                        status=report.status,
                        baseline={**report.references, "data_snapshot": data_now},
                        signals=[sig.model_dump(mode="json") for sig in signals],
                        created_at=now,
                    )
                )
                await session.flush()
                session.add_all(alerts)
        return CheckOut(
            id=check_id,
            created_at=now,
            requested_by=requested_by,
            report=report,
            alerts=[AlertOut.model_validate(a) for a in alerts],
        )

    async def latest_check(self) -> CheckOut | None:
        async with self.s.sessions() as session:
            row = await session.scalar(
                select(DriftCheck).order_by(DriftCheck.created_at.desc()).limit(1)
            )
            if row is None:
                return None
            alerts = await session.scalars(
                select(MonitoringAlert).where(MonitoringAlert.check_id == row.id)
            )
            refs = {k: v for k, v in (row.baseline or {}).items() if k != "data_snapshot"}
            return CheckOut(
                id=row.id,
                created_at=row.created_at,
                requested_by=row.requested_by,
                report=DriftReport.model_validate(
                    {"status": row.status, "signals": row.signals, "references": refs}
                ),
                alerts=[AlertOut.model_validate(a) for a in alerts],
            )

    async def alerts(self, limit: int = 50, status: str | None = "open") -> list[AlertOut]:
        async with self.s.sessions() as session:
            query = select(MonitoringAlert).order_by(MonitoringAlert.created_at.desc())
            if status:
                query = query.where(MonitoringAlert.status == status)
            rows = await session.scalars(query.limit(limit))
            return [AlertOut.model_validate(a) for a in rows]

    async def acknowledge(self, alert_id: uuid.UUID, by: str, note: str | None) -> AlertOut:
        """Mark an open alert as seen by a human. Acknowledging keeps the record (and the
        drift check it came from); it only takes the alert off the open list."""
        async with self.s.sessions() as session, session.begin():
            row = await session.get(MonitoringAlert, alert_id, with_for_update=True)
            if row is None:
                raise NotFoundError("Alert not found")
            if row.status != "open":
                raise ConflictError("Alert is already acknowledged")
            row.status = "acknowledged"
            row.acknowledged_by = by
            row.acknowledged_at = datetime.now(UTC)
            row.ack_note = note
            await session.flush()
            return AlertOut.model_validate(row)

    async def eval_history(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self.s.sessions() as session:
            rows = await session.scalars(
                select(EvaluationRun)
                .where(EvaluationRun.status == "completed")
                .order_by(EvaluationRun.started_at.desc())
                .limit(limit)
            )
            return [
                {
                    "run_id": str(r.id),
                    "started_at": r.started_at.isoformat(),
                    "mode": r.mode,
                    "profile": r.config.get("profile"),
                    "pass_rate": r.pass_rate,
                    "total": r.total_cases,
                    "partial": any((r.config.get("case_filter") or {}).values()),
                    "p95_latency_ms": (r.aggregate.get("latency_ms") or {}).get("p95"),
                    "quality_score": r.aggregate.get("quality_score"),
                }
                for r in rows
            ]
