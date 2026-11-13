"""Experiments API (product.txt 14): list profiles and run one through the evaluation.

Only profile *names* from datasets/experiments.json are accepted, never settings, so a request
cannot change anything a profile file does not already allow. Retrieval experiments finish in
seconds and return their report; agent experiments take minutes and return 202 with a run id
(poll GET /api/v1/evaluations/runs/{id}). They share the one-run-at-a-time evaluation slot.
"""

import asyncio
import uuid
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.deps import AdminDep, ServicesDep, rate_limited
from app.core.errors import ConflictError, NotFoundError
from app.evaluation.dataset import Category, Mode, load_dataset
from app.evaluation.report import RunReport
from app.evaluation.retrieval_eval import list_retrieval_reports
from app.evaluation.runner import EvaluationRunner
from app.experiments.configurations import apply, load_profiles
from app.experiments.runner import run_profile
from app.services import build_services

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])


class ExperimentRunRequest(BaseModel):
    profile: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    kind: Literal["agent", "retrieval"] = "agent"
    mode: Mode = "graph"
    judge: bool = False
    case_ids: list[str] | None = Field(default=None, max_length=100)
    categories: list[Category] | None = None


@router.get("", summary="Experiment profiles and recent runs per profile")
async def list_experiments(services: ServicesDep, _: AdminDep) -> dict[str, Any]:
    from app.monitoring.alerts import MonitoringService

    profiles = load_profiles(services.settings.experiments_file)
    history = await MonitoringService(services).eval_history(limit=50)
    retrieval = await list_retrieval_reports(services, limit=50)
    return {
        "profiles": [p.model_dump() for p in profiles.values()],
        "agent_runs": [h for h in history if h["profile"]],
        "retrieval_runs": [
            {"run_id": str(r.run_id), "profile": r.profile, "metrics": r.metrics}
            for r in retrieval
            if r.profile
        ],
    }


@router.post(
    "/run",
    summary="Run a profile (retrieval: 200 + report; agent: 202 + run id)",
    dependencies=[rate_limited("expensive")],
)
async def run_experiment(
    body: ExperimentRunRequest, services: ServicesDep, principal: AdminDep
) -> JSONResponse:
    profiles = load_profiles(services.settings.experiments_file)
    profile = profiles.get(body.profile)
    if profile is None:
        raise NotFoundError("Experiment profile not found")
    path = services.settings.eval_datasets_dir / "golden_questions.jsonl"
    dataset = load_dataset(path)
    if body.kind == "retrieval":
        report = await run_profile(
            services.settings, profile, dataset, kind="retrieval", requested_by=principal.tenant_id
        )
        return JSONResponse(report.model_dump(mode="json"))
    if services.eval_task is not None and not services.eval_task.done():
        raise ConflictError("An evaluation run is already in progress")
    started: asyncio.Future[uuid.UUID] = asyncio.get_running_loop().create_future()

    async def run() -> RunReport:
        # Not run_profile(): the run id must be reported (on_start) before the run finishes.
        svc = build_services(apply(services.settings, profile))
        await svc.start()
        try:
            return await EvaluationRunner(svc).run(
                dataset,
                mode=body.mode,
                use_judge=body.judge,
                case_ids=body.case_ids,
                categories=list(body.categories) if body.categories else None,
                requested_by=principal.tenant_id,
                on_start=started.set_result,
                profile=profile.name,
            )
        finally:
            await svc.aclose()

    task = asyncio.create_task(run())
    services.eval_task = task
    waiters: set[asyncio.Future[Any]] = {started, task}
    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    if not started.done():
        task.result()  # re-raises the startup error as a normal API error
    return JSONResponse(
        {"run_id": str(started.result()), "status": "running", "profile": profile.name},
        status_code=202,
    )
