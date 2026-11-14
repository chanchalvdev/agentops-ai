"""Persistence for evaluation datasets, runs, and per-case results."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError
from app.db.models import EvaluationCase, EvaluationDataset, EvaluationResult, EvaluationRun
from app.evaluation.dataset import Dataset
from app.evaluation.evaluators import EvaluatorResult
from app.evaluation.report import CaseReport, DatasetInfo, RunReport
from app.monitoring.cost import UsageSummary


class EvaluationStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def upsert_dataset(self, dataset: Dataset) -> uuid.UUID:
        async with self._sessions() as session, session.begin():
            dataset_id: uuid.UUID | None = await session.scalar(
                insert(EvaluationDataset)
                .values(
                    id=uuid.uuid4(),
                    name=dataset.name,
                    version=dataset.version,
                    sha256=dataset.sha256,
                    case_count=len(dataset.cases),
                )
                .on_conflict_do_nothing(constraint="uq_eval_datasets_name_sha")
                .returning(EvaluationDataset.id)
            )
            if dataset_id is None:  # same content stored before
                existing = await session.scalar(
                    select(EvaluationDataset.id).where(
                        EvaluationDataset.name == dataset.name,
                        EvaluationDataset.sha256 == dataset.sha256,
                    )
                )
                assert existing is not None
                return existing
            await session.execute(
                insert(EvaluationCase),
                [
                    {
                        "id": uuid.uuid4(),
                        "dataset_id": dataset_id,
                        "case_id": c.id,
                        "category": c.category,
                        "payload": c.model_dump(mode="json"),
                    }
                    for c in dataset.cases
                ],
            )
            return dataset_id

    async def start_run(
        self, dataset_id: uuid.UUID, requested_by: str, mode: str, config: dict[str, Any]
    ) -> EvaluationRun:
        async with self._sessions() as session, session.begin():
            row = EvaluationRun(
                id=uuid.uuid4(),
                dataset_id=dataset_id,
                requested_by=requested_by,
                status="running",
                mode=mode,
                config=config,
                aggregate={},
                total_cases=0,
                passed_cases=0,
            )
            session.add(row)
        return row

    async def add_result(self, run_id: uuid.UUID, case: CaseReport) -> None:
        values = case.model_dump(mode="json", exclude={"id", "evaluators", "usage"})
        async with self._sessions() as session, session.begin():
            await session.execute(
                insert(EvaluationResult)
                .values(
                    run_id=run_id,
                    case_id=case.id,
                    evaluators=[e.model_dump() for e in case.evaluators],
                    usage=case.usage.model_dump(mode="json") if case.usage else None,
                    **values,
                )
                .on_conflict_do_nothing(constraint="uq_eval_results_run_case")
            )

    async def finish_run(
        self, run_id: uuid.UUID, aggregate: dict[str, Any], error: str | None = None
    ) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(EvaluationRun)
                .where(EvaluationRun.id == run_id)
                .values(
                    status="failed" if error else "completed",
                    aggregate=aggregate,
                    total_cases=aggregate.get("total", 0),
                    passed_cases=aggregate.get("passed", 0),
                    pass_rate=aggregate.get("pass_rate"),
                    error=error,
                    finished_at=datetime.now(UTC),
                )
            )

    async def expire_stale(self, max_age_s: float) -> int:
        """Mark runs still "running" after `max_age_s` as failed. Their process ended without
        reaching finish_run (a graceful cancel does reach it); left alone they would look like
        work in progress forever. Returns how many were marked."""
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(EvaluationRun)
                .where(
                    EvaluationRun.status == "running",
                    EvaluationRun.started_at < now - timedelta(seconds=max_age_s),
                )
                .values(
                    status="failed",
                    error=f"abandoned: still running after {max_age_s / 3600:g} h; the process "
                    "ended without finishing (killed, or the machine slept)",
                    finished_at=now,
                )
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_runs(self, requested_by: str | None, limit: int = 50) -> list[EvaluationRun]:
        """Runs started by `requested_by`, or every run when None (operator view: runs started
        from the CLI are recorded as "cli" and must be visible to operators too)."""
        stmt = select(EvaluationRun)
        if requested_by is not None:
            stmt = stmt.where(EvaluationRun.requested_by == requested_by)
        async with self._sessions() as session:
            rows = await session.scalars(
                stmt.order_by(EvaluationRun.started_at.desc()).limit(limit)
            )
            return list(rows)

    async def report(self, requested_by: str | None, run_id: uuid.UUID) -> RunReport:
        stmt = select(EvaluationRun).where(EvaluationRun.id == run_id)
        if requested_by is not None:
            stmt = stmt.where(EvaluationRun.requested_by == requested_by)
        async with self._sessions() as session:
            run = await session.scalar(stmt)
            if run is None:
                raise NotFoundError("Evaluation run not found")
            dataset = await session.get(EvaluationDataset, run.dataset_id)
            assert dataset is not None
            results = await session.scalars(
                select(EvaluationResult)
                .where(EvaluationResult.run_id == run_id)
                .order_by(EvaluationResult.id)
            )
            cases = [
                CaseReport(
                    id=r.case_id,
                    category=r.category,
                    passed=r.passed,
                    status=r.status,
                    execution_id=r.execution_id,
                    latency_ms=r.latency_ms,
                    input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens,
                    model_calls=r.model_calls,
                    tool_calls=r.tool_calls,
                    cost_usd=None if r.cost_usd is None else float(r.cost_usd),
                    usage=UsageSummary.model_validate(r.usage) if r.usage else None,
                    evaluators=[EvaluatorResult.model_validate(e) for e in r.evaluators],
                    judge=r.judge,
                    failures=r.failures,
                    answer=r.answer,
                )
                for r in results
            ]
        return RunReport(
            run_id=run.id,
            dataset=DatasetInfo(
                name=dataset.name,
                version=dataset.version,
                sha256=dataset.sha256,
                cases=dataset.case_count,
            ),
            mode=run.mode,
            config=run.config,
            started_at=run.started_at,
            finished_at=run.finished_at,
            aggregate=run.aggregate,
            cases=cases,
        )
