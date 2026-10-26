"""Admin CLI. Talks to the database and models directly (no HTTP server needed).

agentops ingest datasets/corpus --tenant tenant_a
agentops search "RPO for payments" --mode hybrid
agentops ask "What is the payments RPO in v2?"
agentops run "What is the RPO in v2, in seconds?" --mode graph --trace
agentops approvals                      # pending human decisions
agentops approve <approval-id>          # or: agentops reject <approval-id> --reason "..."
agentops eval run --judge --out reports/eval.json [--baseline datasets/baselines/x.json]
agentops eval compare base.json head.json
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer

from app.agents.result import AgentRunResult
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.evaluation.dataset import load_dataset
from app.evaluation.regression import Thresholds, check, compare, load_report, load_thresholds
from app.evaluation.report import CaseReport, RunReport, summary
from app.evaluation.runner import EvaluationRunner
from app.ingestion.parsers import SUPPORTED_TYPES
from app.retrieval.schemas import RetrievalMode
from app.services import Services, build_services

cli = typer.Typer(no_args_is_help=True, add_completion=False)
eval_cli = typer.Typer(no_args_is_help=True, help="Run and compare evaluations (product.txt 11).")
cli.add_typer(eval_cli, name="eval")
Tenant = Annotated[str, typer.Option(help="Tenant to act as")]


async def _with_services(fn: Callable[[Services], Awaitable[None]]) -> None:
    configure_logging("WARNING")
    services = build_services(get_settings())
    await services.start()
    try:
        await fn(services)
    finally:
        await services.aclose()


@cli.command()
def ingest(paths: list[Path], tenant: Tenant = "tenant_a") -> None:
    """Ingest files or directories (TXT/MD/PDF) synchronously and report status."""
    files = sorted(
        f
        for p in paths
        for f in ([p] if p.is_file() else p.rglob("*"))
        if f.is_file() and f.suffix.lower() in SUPPORTED_TYPES
    )
    if not files:
        raise typer.BadParameter("no supported files found")

    async def run(services: Services) -> None:
        for f in files:
            try:
                doc, created = await services.ingestion.create_document(
                    tenant, f.name, f.read_bytes()
                )
            except AppError as exc:
                typer.secho(f"  REJECTED {f.name}: {exc.message}", fg="red")
                continue
            status = (
                (await services.ingestion.process_document(doc.id)).value
                if created
                else "duplicate (unchanged)"
            )
            doc = await services.ingestion.get(tenant, doc.id)
            typer.echo(
                f"  {status:<22} {f.name} v{doc.version} chunks={doc.chunk_count}"
                + (f" error={doc.error}" if doc.error else "")
            )

    asyncio.run(_with_services(run))


@cli.command()
def search(
    query: str,
    tenant: Tenant = "tenant_a",
    top_k: int = 5,
    mode: Annotated[RetrievalMode, typer.Option()] = "hybrid",
) -> None:
    """Run retrieval and print ranked chunks with diagnostics."""

    async def run(services: Services) -> None:
        result = await services.retriever.search(tenant, query, top_k=top_k, mode=mode)
        for i, r in enumerate(result.results, 1):
            typer.secho(
                f"{i}. {r.filename} §{r.section or '-'} score={r.score:.4f} "
                f"vec#{r.vector_rank} kw#{r.keyword_rank}",
                bold=True,
            )
            typer.echo("   " + r.text[:200].replace("\n", " ") + "...")
        typer.echo(json.dumps(result.diagnostics.model_dump(), indent=2))

    asyncio.run(_with_services(run))


@cli.command()
def ask(question: str, tenant: Tenant = "tenant_a", top_k: int = 6) -> None:
    """Answer a question with validated citations (non-agentic RAG baseline)."""

    async def run(services: Services) -> None:
        a = await services.answerer.answer(tenant, question, top_k=top_k)
        typer.secho(f"[{a.status}] {a.answer}", bold=True)
        for c in a.citations:
            typer.echo(f"  {c.source_id}: {c.filename} §{c.section or '-'} p{c.page_start or '-'}")
        typer.echo(
            f"model={a.model} calls={a.model_calls} corrections={a.correction_attempts} "
            f"tokens={a.usage.input_tokens}+{a.usage.output_tokens} "
            f"retrieval={a.retrieval_ms:.0f}ms generation={a.generation_ms:.0f}ms"
        )
        if a.validation_errors:
            typer.secho(f"validation: {a.validation_errors}", fg="yellow")

    asyncio.run(_with_services(run))


@cli.command("run")
def run_agent(
    question: str,
    mode: Annotated[str, typer.Option(help="loop | graph")] = "graph",
    tenant: Tenant = "tenant_a",
    trace: Annotated[bool, typer.Option(help="Print the execution ledger")] = False,
) -> None:
    """Run the agent (custom loop or LangGraph workflow) and print the answer."""
    if mode not in ("loop", "graph"):
        raise typer.BadParameter("mode must be loop or graph")

    async def run(services: Services) -> None:
        ctx = services.agents.new_context(
            tenant,
            f"cli-{uuid.uuid4().hex[:12]}",
            permissions=services.auth.permissions_for(tenant),
        )
        ctx.source = "cli"
        r = await services.agents.run(ctx, mode, question)  # type: ignore[arg-type]
        _print_result(r, trace)
        if r.status == "awaiting_approval":
            for a in await services.approvals.list(tenant, "pending", 5):
                if a.execution_id == r.execution_id:
                    typer.secho(
                        f"  approval {a.id}: {a.tool_name} {_args(a.arguments)}", fg="yellow"
                    )

    asyncio.run(_with_services(run))


def _args(arguments: dict[str, object]) -> str:
    return json.dumps(arguments)[:300]


def _print_result(r: AgentRunResult, trace: bool) -> None:
    typer.secho(f"[{r.status}] {r.answer}", bold=True)
    for c in r.citations:
        typer.echo(f"  {c.source_id}: {c.filename} §{c.section or '-'} p{c.page_start or '-'}")
    for t in r.tool_references:
        typer.echo(f"  {t.source_id}: {t.tool} -> {json.dumps(t.output)[:120]}")
    typer.echo(
        f"execution={r.execution_id} steps={r.steps} model_calls={r.model_calls} "
        f"tool_calls={r.tool_calls} corrections={r.corrections} "
        f"verification={r.verification} tokens={r.usage.total_tokens} "
        f"time={r.total_ms / 1000:.1f}s"
    )
    if trace:
        for e in r.ledger:
            typer.echo(
                f"  {e.seq:>3} {e.kind:<10} {e.name:<24} {e.status:<8} "
                f"{e.error_code or ''} {e.latency_ms or ''}"
            )


@cli.command()
def approvals(tenant: Tenant = "tenant_a", status: str = "pending") -> None:
    """List tool calls waiting for (or given) a human decision."""

    async def run(services: Services) -> None:
        rows = await services.approvals.list(tenant, None if status == "all" else status)
        for a in rows:
            typer.echo(f"{a.id}  {a.status:<9} {a.tool_name:<16} {_args(a.arguments)}")
        if not rows:
            typer.echo("(none)")

    asyncio.run(_with_services(run))


def _decide(approval_id: str, tenant: str, approved: bool, reason: str | None) -> None:
    async def run(services: Services) -> None:
        r = await services.agents.decide(
            tenant,
            uuid.UUID(approval_id),
            approved=approved,
            reason=reason,
            permissions=services.auth.permissions_for(tenant),
        )
        _print_result(r, trace=False)

    asyncio.run(_with_services(run))


@cli.command()
def approve(approval_id: str, tenant: Tenant = "tenant_a", reason: str | None = None) -> None:
    """Approve a paused tool call and resume the run from its checkpoint."""
    _decide(approval_id, tenant, True, reason)


@cli.command()
def reject(approval_id: str, tenant: Tenant = "tenant_a", reason: str | None = None) -> None:
    """Reject a paused tool call; the run resumes without executing it."""
    _decide(approval_id, tenant, False, reason)


THRESHOLDS = Path("datasets/eval_thresholds.json")


def _thresholds(path: Path | None) -> Thresholds:
    return load_thresholds(path or (THRESHOLDS if THRESHOLDS.exists() else None))


def _print_breaches(breaches: list[str]) -> None:
    if breaches:
        typer.secho("REGRESSION CHECK FAILED:", fg="red", bold=True)
        for b in breaches:
            typer.secho(f"  - {b}", fg="red")
        raise typer.Exit(1)
    typer.secho("Regression check passed.", fg="green")


@eval_cli.command("run")
def eval_run(
    dataset: Path = Path("datasets/golden_questions.jsonl"),
    mode: Annotated[str, typer.Option(help="graph | loop")] = "graph",
    judge: Annotated[bool, typer.Option(help="Add advisory LLM-judge scores")] = False,
    case: Annotated[list[str] | None, typer.Option(help="Only these case ids")] = None,
    category: Annotated[list[str] | None, typer.Option(help="Only these categories")] = None,
    out: Annotated[Path | None, typer.Option(help="Write the JSON report here")] = None,
    baseline: Annotated[Path | None, typer.Option(help="Fail on regression vs this report")] = None,
    thresholds: Path | None = None,
) -> None:
    """Run the dataset through the agent, print a summary, write a JSON report."""
    if mode not in ("loop", "graph"):
        raise typer.BadParameter("mode must be loop or graph")
    data = load_dataset(dataset)

    def progress(c: CaseReport) -> None:
        mark = {True: "PASS", False: "FAIL", None: "skip"}[c.passed]
        color = {True: "green", False: "red", None: "yellow"}[c.passed]
        typer.secho(
            f"  {mark} {c.id:<24} {c.status:<22} {((c.latency_ms or 0) / 1000):6.1f}s "
            + ("; ".join(c.failures)[:110]),
            fg=color,
        )

    report: RunReport | None = None

    async def run(services: Services) -> None:
        nonlocal report
        selected = [
            c
            for c in data.cases
            if (not case or c.id in case) and (not category or c.category in category)
        ]
        typer.echo(f"Evaluating {len(selected)} cases ({data.name}@{data.version}) in {mode}")
        report = await EvaluationRunner(services).run(
            data,
            mode=mode,  # type: ignore[arg-type]
            use_judge=judge,
            case_ids=case,
            categories=category,
            on_case=progress,
        )

    asyncio.run(_with_services(run))
    assert report is not None
    typer.echo("\n" + summary(report))
    target = out or Path("reports") / f"eval-{report.run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.model_dump_json(indent=2))
    typer.echo(f"\nReport: {target}")
    if baseline:
        _print_breaches(check(report, load_report(baseline), _thresholds(thresholds)))


@eval_cli.command("compare")
def eval_compare(base: Path, head: Path, thresholds: Path | None = None) -> None:
    """Compare two JSON reports metric by metric; exit 1 on a threshold breach."""
    result = compare(load_report(base), load_report(head), _thresholds(thresholds))
    pr = result.pass_rate
    typer.echo(f"pass rate: {pr['base']} -> {pr['head']} ({pr['delta']})")
    for title, rows in (("category", result.categories), ("evaluator", result.evaluators)):
        for name, v in rows.items():
            typer.echo(f"  {title:<9} {name:<20} {v['base']} -> {v['head']} ({v['delta']})")
    if result.newly_failing:
        typer.secho(f"newly failing: {result.newly_failing}", fg="red")
    if result.newly_passing:
        typer.secho(f"newly passing: {result.newly_passing}", fg="green")
    _print_breaches(result.breaches)


@eval_cli.command("check")
def eval_check(report: Path, baseline: Path | None = None, thresholds: Path | None = None) -> None:
    """CI gate: exit 1 if the report breaches the thresholds (vs a baseline if given)."""
    head = load_report(report)
    _print_breaches(
        check(head, load_report(baseline) if baseline else None, _thresholds(thresholds))
    )


def main() -> None:
    cli()
