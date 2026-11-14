"""Evaluation API (product.txt 14). A run takes minutes on a local model, so POST returns 202
with the run id and the run continues in the background; poll GET /evaluations/runs/{id}.
One run at a time per process: they share the single local model, a second would only queue.
Operator (admin) tenants only: a run occupies the shared model for many minutes."""

import asyncio
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AdminDep, ServicesDep, rate_limited
from app.core.errors import ConflictError, NotFoundError
from app.evaluation.dataset import Category, Mode, load_dataset
from app.evaluation.regression import Comparison, compare, load_thresholds
from app.evaluation.report import RunReport
from app.evaluation.runner import EvaluationRunner
from app.evaluation.store import EvaluationStore

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])


class EvaluationRunRequest(BaseModel):
    # A dataset NAME inside the configured datasets directory, never a path.
    dataset: str = Field(default="golden_questions", pattern=r"^[a-z0-9_-]{1,64}$")
    mode: Mode = "graph"
    judge: bool = False
    case_ids: list[str] | None = Field(default=None, max_length=100)
    categories: list[Category] | None = None


class EvaluationRunStarted(BaseModel):
    run_id: uuid.UUID
    status: str


class EvaluationRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requested_by: str
    status: str
    mode: str
    total_cases: int
    passed_cases: int
    pass_rate: float | None
    config: dict[str, Any]
    error: str | None
    started_at: datetime
    finished_at: datetime | None


@router.post(
    "/run",
    status_code=202,
    response_model=EvaluationRunStarted,
    dependencies=[rate_limited("expensive")],
)
async def run_evaluation(
    body: EvaluationRunRequest, services: ServicesDep, principal: AdminDep
) -> EvaluationRunStarted:
    if services.eval_task is not None and not services.eval_task.done():
        raise ConflictError("An evaluation run is already in progress")
    path = services.settings.eval_datasets_dir / f"{body.dataset}.jsonl"
    if not path.is_file():
        raise NotFoundError("Dataset not found")
    dataset = load_dataset(path)
    started: asyncio.Future[uuid.UUID] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        EvaluationRunner(services).run(
            dataset,
            mode=body.mode,
            use_judge=body.judge,
            case_ids=body.case_ids,
            categories=list(body.categories) if body.categories else None,
            requested_by=principal.tenant_id,
            on_start=started.set_result,
        )
    )
    services.eval_task = task
    waiters: set[asyncio.Future[Any]] = {started, task}
    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    if not started.done():  # failed before the run row existed (e.g. corpus ingestion)
        task.result()  # re-raises the error as a normal API error
    return EvaluationRunStarted(run_id=started.result(), status="running")


@router.get("/runs", response_model=list[EvaluationRunOut])
async def list_runs(
    services: ServicesDep, principal: AdminDep, limit: int = Query(default=20, ge=1, le=100)
) -> Any:
    return await EvaluationStore(services.sessions).list_runs(
        None, limit
    )  # operator-only route: every run


@router.get("/runs/{run_id}", response_model=RunReport, summary="Run with per-case results")
async def get_run(run_id: uuid.UUID, services: ServicesDep, principal: AdminDep) -> RunReport:
    return await EvaluationStore(services.sessions).report(None, run_id)


@router.get("/compare", response_model=Comparison, summary="Compare two runs; lists breaches")
async def compare_runs(
    base: uuid.UUID, head: uuid.UUID, services: ServicesDep, principal: AdminDep
) -> Comparison:
    store = EvaluationStore(services.sessions)
    thresholds = services.settings.eval_datasets_dir / "eval_thresholds.json"
    return compare(
        await store.report(None, base),
        await store.report(None, head),
        load_thresholds(thresholds if thresholds.is_file() else None),
    )
