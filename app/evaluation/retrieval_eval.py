"""Retrieval-only evaluation of the golden set (Day 3 deferral: "RAG metrics recall@k, MRR").

Every case that names `expected_sources` is searched exactly as asked, through the same retriever
and configuration the agent uses, with no model involved. That makes it deterministic and fast
(seconds, not the ~20 minutes of a full agent run), so it is the signal the drift check uses for
retrieval: a chunking or retrieval regression shows up here before it shows up in answers.

Metrics, per case then averaged (k = top_k):
- hit@k: at least one expected file appears in the top k
- recall@k: fraction of the expected files that appear in the top k
- MRR: 1 / rank of the first chunk from an expected file (0 if none)
- answer coverage@k: of the case's `must_contain` answer strings that actually occur in its
  expected source documents, the fraction found in the text of retrieved passages *from those
  documents* (a match in an unrelated file does not count). File-level
  metrics saturate on this small corpus (5 documents, 10 chunks: top-5 returns half of it, and a
  measured run with 250-char chunks still scored recall 1.0); coverage asks whether the passage
  that holds the answer was retrieved, which is what the answer step actually needs.
- source distribution: share of all returned chunks per file, to spot a shifted corpus or ranking
"""

import statistics
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.errors import NotFoundError
from app.db.models import Document, RetrievalEvalRun
from app.evaluation.dataset import Dataset

if TYPE_CHECKING:
    from app.services import Services


class RetrievalCase(BaseModel):
    id: str
    category: str
    expected: list[str]
    retrieved: list[str]  # filenames of the top-k chunks, in rank order
    hit: bool
    recall: float
    reciprocal_rank: float
    answer_groups: int = 0  # must_contain groups that occur in the expected documents
    answer_covered: int = 0  # of those, groups found in the retrieved passages
    latency_ms: float


class RetrievalReport(BaseModel):
    run_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    dataset: dict[str, Any]
    profile: str | None = None
    config: dict[str, Any]
    metrics: dict[str, Any]
    cases: list[RetrievalCase]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _p(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))], 1)


def _has(text: str, alternatives: list[str]) -> bool:
    folded = " ".join(text.split()).casefold()
    return any(" ".join(a.split()).casefold() in folded for a in alternatives)


def answer_coverage(
    groups: list[list[str]], source_text: str, retrieved_text: str
) -> tuple[int, int]:
    """(answer groups present in the source documents, of those present in the retrieved text).
    Groups the documents never contain (computed values like "900 seconds") are not counted."""
    present = [g for g in groups if _has(source_text, g)]
    return len(present), sum(1 for g in present if _has(retrieved_text, g))


def score_case(
    case_id: str,
    category: str,
    expected: list[str],
    retrieved: list[str],
    latency_ms: float,
    coverage: tuple[int, int] = (0, 0),
) -> RetrievalCase:
    wanted = set(expected)
    first = next((i for i, f in enumerate(retrieved, 1) if f in wanted), None)
    return RetrievalCase(
        id=case_id,
        category=category,
        expected=expected,
        retrieved=retrieved,
        hit=first is not None,
        recall=round(len(wanted & set(retrieved)) / len(wanted), 4),
        reciprocal_rank=round(1 / first, 4) if first else 0.0,
        answer_groups=coverage[0],
        answer_covered=coverage[1],
        latency_ms=round(latency_ms, 1),
    )


def retrieval_metrics(cases: list[RetrievalCase], k: int) -> dict[str, Any]:
    if not cases:
        return {"cases": 0, "k": k}
    returned = Counter(f for c in cases for f in c.retrieved)
    total = sum(returned.values())
    latencies = [c.latency_ms for c in cases]
    with_answers = [c for c in cases if c.answer_groups]
    return {
        "cases": len(cases),
        "k": k,
        "hit_rate": round(statistics.mean(float(c.hit) for c in cases), 4),
        "recall_at_k": round(statistics.mean(c.recall for c in cases), 4),
        "mrr": round(statistics.mean(c.reciprocal_rank for c in cases), 4),
        "answer_coverage": (
            round(statistics.mean(c.answer_covered / c.answer_groups for c in with_answers), 4)
            if with_answers
            else None
        ),
        "answer_cases": len(with_answers),
        "answer_misses": [c.id for c in with_answers if c.answer_covered < c.answer_groups],
        "empty_results": sum(1 for c in cases if not c.retrieved),
        "source_distribution": {
            f: round(n / total, 4) for f, n in sorted(returned.items()) if total
        },
        "latency_ms": {"p50": _p(latencies, 0.5), "p95": _p(latencies, 0.95)},
        "misses": [c.id for c in cases if not c.hit],
    }


async def run_retrieval_eval(
    services: "Services",
    dataset: Dataset,
    *,
    k: int = 5,
    requested_by: str = "cli",
    profile: str | None = None,
    persist: bool = True,
) -> RetrievalReport:
    from app.evaluation.runner import EvaluationRunner  # avoids an import cycle at module load

    runner = EvaluationRunner(services)
    await runner.prepare_corpus(services.settings.eval_corpus_dirs)
    tenant = services.settings.eval_tenant
    async with services.sessions() as session:
        docs = await session.execute(
            select(Document.filename, Document.extracted_text).where(
                Document.tenant_id == tenant, Document.is_latest
            )
        )
        source_text = {name: text for name, text in docs.all()}
    cases: list[RetrievalCase] = []
    for case in dataset.cases:
        if not case.expected_sources or case.uses_workspace:
            continue
        started = time.perf_counter()
        result = await services.retriever.search(tenant, case.question, top_k=k)
        elapsed = (time.perf_counter() - started) * 1000
        sources = "\n".join(source_text.get(f, "") for f in case.expected_sources)
        # Only passages from the expected documents count: an answer string that also occurs in an
        # unrelated document (e.g. "15 minutes" in the failover runbook) is not the answer.
        wanted = set(case.expected_sources)
        retrieved_text = "\n".join(c.text for c in result.results if c.filename in wanted)
        cases.append(
            score_case(
                case.id,
                case.category,
                case.expected_sources,
                [c.filename for c in result.results],
                elapsed,
                answer_coverage(case.must_contain, sources, retrieved_text),
            )
        )
    s = services.settings
    report = RetrievalReport(
        dataset={"name": dataset.name, "version": dataset.version, "sha256": dataset.sha256},
        profile=profile,
        config={
            **services.retriever.config(),
            "top_k": k,
            "chunk_strategy": s.chunk_strategy,
            "chunk_size_chars": s.chunk_size_chars,
            "chunk_overlap_chars": s.chunk_overlap_chars,
            "eval_tenant": tenant,
        },
        metrics=retrieval_metrics(cases, k),
        cases=cases,
    )
    if persist:
        await save_retrieval_report(services, report, requested_by)
    return report


async def save_retrieval_report(
    services: "Services", report: RetrievalReport, requested_by: str
) -> None:
    async with services.sessions() as session, session.begin():
        session.add(
            RetrievalEvalRun(
                id=report.run_id,
                requested_by=requested_by,
                dataset_name=report.dataset["name"],
                dataset_version=report.dataset["version"],
                dataset_sha256=report.dataset["sha256"],
                profile=report.profile,
                config=report.config,
                metrics=report.metrics,
                cases=[c.model_dump() for c in report.cases],
                created_at=report.created_at,
            )
        )


def _to_report(row: RetrievalEvalRun) -> RetrievalReport:
    return RetrievalReport(
        run_id=row.id,
        dataset={
            "name": row.dataset_name,
            "version": row.dataset_version,
            "sha256": row.dataset_sha256,
        },
        profile=row.profile,
        config=row.config,
        metrics=row.metrics,
        cases=[RetrievalCase.model_validate(c) for c in row.cases],
        created_at=row.created_at,
    )


async def list_retrieval_reports(
    services: "Services", limit: int = 20, *, exclude_experiments: bool = False
) -> list[RetrievalReport]:
    """Newest first. `exclude_experiments` keeps only runs made under the server's own
    configuration (experiment profiles record their name; e.g. a deliberate degraded probe)."""
    stmt = select(RetrievalEvalRun)
    if exclude_experiments:
        stmt = stmt.where(RetrievalEvalRun.profile.is_(None))
    async with services.sessions() as session:
        rows = await session.scalars(stmt.order_by(RetrievalEvalRun.created_at.desc()).limit(limit))
        return [_to_report(r) for r in rows]


async def get_retrieval_report(services: "Services", run_id: uuid.UUID) -> RetrievalReport:
    async with services.sessions() as session:
        row = await session.get(RetrievalEvalRun, run_id)
    if row is None:
        raise NotFoundError("Retrieval evaluation run not found")
    return _to_report(row)
