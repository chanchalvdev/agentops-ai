"""Run a dataset through the real agent and score every case.

Each case runs as the dedicated evaluation tenant through AgentService, the same code path as the
API, so persisted executions, traces, and the ledger of every case can be inspected afterwards.
The runner also plays the human: when a case pauses for approval it gives the decision the case
specifies. Workspace cases start from a freshly seeded copy of the evaluation workspace.
Cases run one at a time: one local model serves them all, so parallelism would only queue.
"""

import logging
import shutil
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from app.agents import custom_loop, graph, nodes
from app.agents.result import AgentRunResult
from app.agents.tuning import AgentTuning
from app.core.security import DOCUMENT_PERMISSIONS, WORKSPACE_PERMISSIONS
from app.evaluation.dataset import Dataset, EvalCase, Mode
from app.evaluation.evaluators import CaseRun, EvaluatorResult, evaluate
from app.evaluation.judge import JUDGE_VERSION, judge
from app.evaluation.report import CaseReport, DatasetInfo, RunReport, aggregate
from app.evaluation.store import EvaluationStore
from app.ingestion.parsers import SUPPORTED_TYPES
from app.monitoring.cost import summarize
from app.observability.interfaces import Score

if TYPE_CHECKING:
    from app.services import Services

logger = logging.getLogger(__name__)
EVAL_PERMISSIONS = DOCUMENT_PERMISSIONS | WORKSPACE_PERMISSIONS
_MAX_DECISIONS = 3


def run_config(services: "Services", mode: Mode, use_judge: bool) -> dict[str, Any]:
    """Everything the scores depend on, stored with the run so two runs can be compared."""
    s = services.settings
    return {
        "mode": mode,
        "models": {
            "main": s.llm_model_main,
            "small": s.llm_model_small,
            "embedding": s.embedding_model,
            "provider": s.llm_provider,
        },
        "prompt_version": nodes.PROMPT_VERSION if mode == "graph" else custom_loop.PROMPT_VERSION,
        "workflow_version": (
            graph.WORKFLOW_VERSION if mode == "graph" else custom_loop.WORKFLOW_VERSION
        ),
        "retrieval": {
            "top_k": s.retrieval_top_k,
            "candidate_k": s.retrieval_candidate_k,
            "rrf_k": s.rrf_k,
            "chunk_strategy": s.chunk_strategy,
            "chunk_size_chars": s.chunk_size_chars,
            "chunk_overlap_chars": s.chunk_overlap_chars,
            "mode": s.retrieval_mode,
            "min_vector_score": s.retrieval_min_vector_score,
        },
        "tuning": AgentTuning.from_settings(s).as_dict(),
        "caches": {
            "query_embeddings": s.cache_query_embeddings,
            "tool_results": s.cache_tool_results,
            "semantic": s.semantic_cache_enabled,
            "semantic_min_similarity": s.semantic_cache_min_similarity,
        },
        "judge": JUDGE_VERSION if use_judge else None,
        "eval_tenant": s.eval_tenant,
    }


class EvaluationRunner:
    def __init__(self, services: "Services", store: EvaluationStore | None = None) -> None:
        self.s = services
        self.store = store or EvaluationStore(services.sessions)
        self.tenant = services.settings.eval_tenant

    async def prepare_corpus(self, dirs: list[Path]) -> int:
        """Ingest the evaluation corpus into the evaluation tenant (idempotent by content)."""
        files = sorted(
            f
            for d in dirs
            for f in (d.rglob("*") if d.is_dir() else [d])
            if f.is_file() and f.suffix.lower() in SUPPORTED_TYPES
        )
        for f in files:
            doc, created = await self.s.ingestion.create_document(
                self.tenant, f.name, f.read_bytes(), {"source": "eval-corpus"}
            )
            if created:
                await self.s.ingestion.process_document(doc.id)
        return len(files)

    def _seed_workspace(self, case: EvalCase) -> Path:
        root = self.s.workspaces.for_tenant(self.tenant).root
        shutil.rmtree(root)  # the evaluation tenant's own directory only
        seed = self.s.settings.eval_workspace_seed
        if seed.is_dir():
            shutil.copytree(seed, root)
        else:
            root.mkdir(parents=True)
        for path, content in case.workspace_files.items():
            # The runner is the operator, not the agent: fixtures may be files the agent's jail
            # denies (a planted .env is the point of the secret-leak case). Containment still holds.
            rel = PurePosixPath(path)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"fixture path escapes the workspace: {path}")
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return root

    @staticmethod
    def _files(root: Path, case: EvalCase) -> dict[str, str | None]:
        paths = {*case.expect_files, *case.expect_absent, *case.expect_unchanged}
        return {p: (root / p).read_text() if (root / p).is_file() else None for p in sorted(paths)}

    async def _decide(self, case: EvalCase, result: AgentRunResult) -> AgentRunResult:
        decisions = 0
        while result.status == "awaiting_approval" and case.approval and decisions < _MAX_DECISIONS:
            pending = [
                a
                for a in await self.s.approvals.find(self.tenant, "pending", 200)
                if a.execution_id == result.execution_id
            ]
            if not pending:
                break
            result = await self.s.agents.decide(
                self.tenant,
                pending[0].id,
                approved=case.approval == "approve",
                reason="evaluation runner",
                permissions=EVAL_PERMISSIONS,
            )
            decisions += 1
        return result

    async def run_case(
        self, case: EvalCase, mode: Mode, run_id: str, use_judge: bool = False
    ) -> CaseReport:
        if mode not in case.modes:
            return CaseReport(id=case.id, category=case.category, passed=None, status="skipped")
        root = self._seed_workspace(case) if case.uses_workspace else None
        before = self._files(root, case) if root else {}
        ctx = self.s.agents.new_context(
            self.tenant,
            f"eval-{run_id[:8]}-{case.id}"[:64],
            allowed_tools=case.allowed_tools,
            permissions=EVAL_PERMISSIONS,
        )
        ctx.source = "eval"
        started = time.perf_counter()
        result = await self._decide(case, await self.s.agents.run(ctx, mode, case.question))
        latency_ms = round((time.perf_counter() - started) * 1000, 1)  # incl. approval resumes
        run = CaseRun(
            case=case,
            result=result,
            max_steps=ctx.limits.max_steps,
            files_before=before,
            files_after=self._files(root, case) if root else {},
        )
        evaluations = evaluate(run)
        failures = [f"{e.name}: {e.detail}" for e in evaluations if e.passed is False]
        report = CaseReport(
            id=case.id,
            category=case.category,
            passed=not failures,
            status=result.status,
            execution_id=result.execution_id,
            latency_ms=latency_ms,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            model_calls=result.model_calls,
            tool_calls=result.tool_calls,
            cost_usd=None if result.cost_usd is None else float(result.cost_usd),
            usage=summarize(
                result.ledger,
                self.s.settings.reference_price_input_per_mtok,
                self.s.settings.reference_price_output_per_mtok,
            ),
            evaluators=evaluations,
            failures=failures,
            answer=result.answer[:2000],
        )
        if use_judge and result.status in ("answered", "insufficient_evidence"):
            report.judge = await self._judge(case, result)
        self._push_scores(report, evaluations)
        return report

    async def _judge(self, case: EvalCase, result: AgentRunResult) -> dict[str, Any]:
        try:
            scores = await judge(self.s.llm, case, result)
        except Exception as exc:  # a judge outage must not fail the deterministic evaluation
            logger.warning("judge failed", extra={"case": case.id, "error": repr(exc)})
            return {"error": type(exc).__name__}
        if scores is None:
            return {"error": "invalid_judge_output"}
        return {"scores": scores.normalized(), "explanation": scores.explanation}

    def _push_scores(self, report: CaseReport, evaluations: list[EvaluatorResult]) -> None:
        if report.execution_id is None:
            return
        scores = [Score(report.execution_id, "eval.passed", float(bool(report.passed)))]
        scores += [
            Score(report.execution_id, f"eval.{e.name}", float(e.passed), e.detail or None)
            for e in evaluations
            if e.passed is not None
        ]
        if report.judge and "scores" in report.judge:
            scores += [
                Score(report.execution_id, f"judge.{k}", v)
                for k, v in report.judge["scores"].items()
            ]
        for score in scores:
            self.s.telemetry.score(score, "eval")

    async def run(
        self,
        dataset: Dataset,
        *,
        mode: Mode = "graph",
        use_judge: bool = False,
        case_ids: list[str] | None = None,
        categories: list[str] | None = None,
        requested_by: str = "cli",
        corpus_dirs: list[Path] | None = None,
        on_case: Callable[[CaseReport], None] | None = None,
        on_start: Callable[[uuid.UUID], None] | None = None,
        profile: str | None = None,
    ) -> RunReport:
        cases = [
            c
            for c in dataset.cases
            if (not case_ids or c.id in case_ids) and (not categories or c.category in categories)
        ]
        await self.store.expire_stale(self.s.settings.eval_stale_after_s)
        await self.prepare_corpus(corpus_dirs or self.s.settings.eval_corpus_dirs)
        config = run_config(self.s, mode, use_judge)
        config["case_filter"] = {"ids": case_ids, "categories": categories}
        config["profile"] = profile  # experiment profile name (datasets/experiments.json)
        dataset_id = await self.store.upsert_dataset(dataset)
        row = await self.store.start_run(dataset_id, requested_by, mode, config)
        if on_start:
            on_start(row.id)
        report = RunReport(
            run_id=row.id,
            dataset=DatasetInfo(
                name=dataset.name,
                version=dataset.version,
                sha256=dataset.sha256,
                cases=len(cases),
            ),
            mode=mode,
            config=config,
            started_at=datetime.now(UTC),
        )
        error: str | None = None
        try:
            for case in cases:
                try:
                    case_report = await self.run_case(case, mode, row.id.hex, use_judge)
                except Exception as exc:  # one broken case must not lose the whole run
                    logger.exception("evaluation case crashed", extra={"case": case.id})
                    case_report = CaseReport(
                        id=case.id,
                        category=case.category,
                        passed=False,
                        status="error",
                        failures=[f"runner: {type(exc).__name__}"],
                    )
                await self.store.add_result(row.id, case_report)
                report.cases.append(case_report)
                if on_case:
                    on_case(case_report)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            report.aggregate = aggregate(report.cases)
            report.finished_at = datetime.now(UTC)
            await self.store.finish_run(row.id, report.aggregate, error)
        return report
