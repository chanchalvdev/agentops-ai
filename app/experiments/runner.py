"""Run a profile: build services with the profile's settings, then evaluate.

A profile gets its own Services (engine, caches, graph) so its caches start cold and nothing it
changes leaks into the server's own configuration. `kind="retrieval"` runs the retrieval-only
evaluation (seconds, no model); `kind="agent"` runs the full golden set through the agent.
"""

from collections.abc import Callable
from typing import Literal

from app.core.config import Settings
from app.evaluation.dataset import Dataset
from app.evaluation.report import CaseReport, RunReport
from app.evaluation.retrieval_eval import RetrievalReport, run_retrieval_eval
from app.evaluation.runner import EvaluationRunner
from app.experiments.configurations import Profile, apply
from app.services import build_services

Kind = Literal["agent", "retrieval"]


async def run_profile(
    base: Settings,
    profile: Profile,
    dataset: Dataset,
    *,
    kind: Kind = "agent",
    mode: Literal["graph", "loop"] = "graph",
    judge: bool = False,
    case_ids: list[str] | None = None,
    categories: list[str] | None = None,
    requested_by: str = "cli",
    k: int = 5,
    on_case: Callable[[CaseReport], None] | None = None,
) -> RunReport | RetrievalReport:
    services = build_services(apply(base, profile))
    await services.start()
    try:
        if kind == "retrieval":
            return await run_retrieval_eval(
                services, dataset, k=k, requested_by=requested_by, profile=profile.name
            )
        return await EvaluationRunner(services).run(
            dataset,
            mode=mode,
            use_judge=judge,
            case_ids=case_ids,
            categories=categories,
            requested_by=requested_by,
            on_case=on_case,
            profile=profile.name,
        )
    finally:
        await services.aclose()
