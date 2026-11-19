"""Admin CLI. Talks to the database and models directly (no HTTP server needed).

agentops ingest datasets/corpus --tenant tenant_a
agentops search "RPO for payments" --mode hybrid
agentops ask "What is the payments RPO in v2?"
agentops run "What is the RPO in v2, in seconds?" --mode graph --trace
agentops approvals                      # pending human decisions
agentops reindex [--tenant t]           # re-chunk + re-embed after a config change
agentops approve <approval-id>          # or: agentops reject <approval-id> --reason "..."
agentops eval run --judge --out reports/eval.json [--baseline datasets/baselines/x.json]
agentops eval compare base.json head.json
agentops eval retrieval --out reports/retrieval.json          # recall@k / hit rate / MRR, no LLM
agentops experiment list
agentops experiment run optimized --kind agent --judge --out reports/bench-optimized.json
agentops experiment benchmark reports/bench-baseline.json reports/bench-optimized.json
agentops drift check [--eval-report r.json] [--retrieval-report r.json]   # exit 1 on drift
agentops metrics --hours 24
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
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
from app.evaluation.retrieval_eval import RetrievalReport, run_retrieval_eval
from app.evaluation.runner import EvaluationRunner
from app.experiments.benchmark import markdown
from app.experiments.configurations import load_profiles
from app.experiments.runner import run_profile
from app.ingestion.parsers import SUPPORTED_TYPES
from app.monitoring.alerts import CheckOut, MonitoringService
from app.monitoring.metrics import operational_metrics
from app.retrieval.schemas import RetrievalMode
from app.services import Services, build_services

cli = typer.Typer(no_args_is_help=True, add_completion=False)
eval_cli = typer.Typer(no_args_is_help=True, help="Run and compare evaluations (product.txt 11).")
cli.add_typer(eval_cli, name="eval")
experiment_cli = typer.Typer(no_args_is_help=True, help="Configuration profiles and benchmarks.")
cli.add_typer(experiment_cli, name="experiment")
drift_cli = typer.Typer(no_args_is_help=True, help="Drift detection (product.txt 13).")
cli.add_typer(drift_cli, name="drift")
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
def reindex(
    tenant: Annotated[
        str | None, typer.Option(help="Only this tenant (default: every tenant)")
    ] = None,
) -> None:
    """Re-chunk and re-embed every document, e.g. after changing chunking or the embedding
    model. Each document keeps serving its previous index until its new one is committed."""

    async def run(services: Services) -> None:
        from sqlalchemy import select

        from app.db.models import Document

        query = select(Document.id, Document.tenant_id, Document.filename, Document.version)
        if tenant:
            query = query.where(Document.tenant_id == tenant)
        async with services.sessions() as session:
            rows = (await session.execute(query.order_by(Document.tenant_id))).all()
        for doc_id, owner, name, version in rows:
            try:
                await services.ingestion.mark_for_reindex(owner, doc_id)
            except AppError as exc:
                typer.secho(f"  skipped  {owner}/{name} v{version}: {exc.message}", fg="yellow")
                continue
            status = await services.ingestion.process_document(doc_id)
            doc = await services.ingestion.get(owner, doc_id)
            typer.echo(
                f"  {status.value:<10} {owner}/{name} v{version} chunks={doc.chunk_count}"
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
            for a in await services.approvals.find(tenant, "pending", 5):
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
        rows = await services.approvals.find(tenant, None if status == "all" else status)
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


@eval_cli.command("retrieval")
def eval_retrieval(
    dataset: Path = Path("datasets/golden_questions.jsonl"),
    k: Annotated[int, typer.Option(help="top k passages per question")] = 5,
    out: Annotated[Path | None, typer.Option(help="Write the JSON report here")] = None,
) -> None:
    """Retrieval-only evaluation: recall@k, hit rate, MRR, source distribution (no model)."""
    data = load_dataset(dataset)
    report: RetrievalReport | None = None

    async def run(services: Services) -> None:
        nonlocal report
        report = await run_retrieval_eval(services, data, k=k)

    asyncio.run(_with_services(run))
    assert report is not None
    _print_retrieval(report)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report.model_dump_json(indent=2))
        typer.echo(f"Report: {out}")


def _print_retrieval(report: RetrievalReport) -> None:
    m = report.metrics
    typer.echo(
        f"Retrieval {report.run_id} profile={report.profile} k={m.get('k')} cases={m.get('cases')}"
        f"\n  hit_rate={m.get('hit_rate')} recall@k={m.get('recall_at_k')} mrr={m.get('mrr')} "
        f"empty={m.get('empty_results')} p95={(m.get('latency_ms') or {}).get('p95')} ms"
        f"\n  config={json.dumps(report.config)}"
    )
    if m.get("misses"):
        typer.secho(f"  misses: {m['misses']}", fg="yellow")


@experiment_cli.command("list")
def experiment_list(file: Path = Path("datasets/experiments.json")) -> None:
    """Show the configured profiles and their overrides."""
    for p in load_profiles(file).values():
        typer.echo(f"{p.name:<22} [{p.purpose}] {p.description}")
        typer.echo(f"{'':<22} {json.dumps(p.overrides)}")


@experiment_cli.command("run")
def experiment_run(
    profile: str,
    kind: Annotated[str, typer.Option(help="agent | retrieval")] = "agent",
    mode: Annotated[str, typer.Option(help="graph | loop")] = "graph",
    judge: Annotated[bool, typer.Option(help="Add advisory LLM-judge scores")] = False,
    case: Annotated[list[str] | None, typer.Option(help="Only these case ids")] = None,
    category: Annotated[list[str] | None, typer.Option(help="Only these categories")] = None,
    out: Annotated[Path | None, typer.Option(help="Write the JSON report here")] = None,
    file: Path = Path("datasets/experiments.json"),
    dataset: Path = Path("datasets/golden_questions.jsonl"),
) -> None:
    """Run the golden set under a profile's settings (its own services, cold caches)."""
    if kind not in ("agent", "retrieval") or mode not in ("graph", "loop"):
        raise typer.BadParameter("kind must be agent|retrieval and mode graph|loop")
    chosen = load_profiles(file).get(profile)
    if chosen is None:
        raise typer.BadParameter(f"unknown profile {profile}")
    data = load_dataset(dataset)
    configure_logging("WARNING")

    def progress(c: CaseReport) -> None:
        mark = {True: "PASS", False: "FAIL", None: "skip"}[c.passed]
        typer.echo(f"  {mark} {c.id:<24} {c.status:<22} {((c.latency_ms or 0) / 1000):6.1f}s")

    report = asyncio.run(
        run_profile(
            get_settings(),
            chosen,
            data,
            kind=kind,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            judge=judge,
            case_ids=case,
            categories=category,
            on_case=progress,
        )
    )
    if isinstance(report, RetrievalReport):
        _print_retrieval(report)
    else:
        typer.echo("\n" + summary(report))
    target = out or Path("reports") / f"experiment-{profile}-{kind}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.model_dump_json(indent=2))
    typer.echo(f"Report: {target}")


@experiment_cli.command("benchmark")
def experiment_benchmark(
    reports: list[Path],
    out: Annotated[Path | None, typer.Option(help="Write the markdown table here")] = None,
    thresholds: Path | None = None,
) -> None:
    """Compare agent reports (first = baseline): cost, latency, success, quality, calls."""
    table = markdown([load_report(p) for p in reports], _thresholds(thresholds))
    typer.echo(table)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(table + "\n")


@drift_cli.command("check")
def drift_check(
    eval_report: Annotated[Path | None, typer.Option(help="Agent eval report to check")] = None,
    eval_baseline: Annotated[Path | None, typer.Option(help="Override the baseline")] = None,
    retrieval_report: Annotated[Path | None, typer.Option(help="Retrieval report")] = None,
    retrieval_baseline: Annotated[Path | None, typer.Option(help="Override")] = None,
    persist: Annotated[bool, typer.Option(help="Store the check and its alerts")] = True,
) -> None:
    """Compare quality, retrieval, data, and operational metrics with their references.
    Exit 1 on measured drift, 0 otherwise (suspected drift is printed, not failed)."""
    result: CheckOut | None = None

    async def run(services: Services) -> None:
        nonlocal result
        result = await MonitoringService(services).check(
            "cli",
            eval_head=load_report(eval_report) if eval_report else None,
            eval_base=load_report(eval_baseline) if eval_baseline else None,
            retrieval_head=_retrieval(retrieval_report),
            retrieval_base=_retrieval(retrieval_baseline),
            persist=persist,
        )

    asyncio.run(_with_services(run))
    assert result is not None
    colors = {"ok": "green", "insufficient_data": "white"}
    for sig in result.report.signals:
        label = sig.status if sig.status != "drift" else f"DRIFT/{sig.evidence}"
        typer.secho(
            f"  {sig.category:<11} {sig.metric:<34} {label:<18} {sig.message[:120]}",
            fg=colors.get(sig.status, "red" if sig.evidence == "measured" else "yellow"),
        )
    status = result.report.status
    typer.secho(
        f"\nDrift status: {status} ({len(result.alerts)} alerts, check {result.id})",
        fg={"ok": "green", "suspected": "yellow"}.get(status, "red"),
        bold=True,
    )
    if status == "drift":
        raise typer.Exit(1)


def _retrieval(path: Path | None) -> RetrievalReport | None:
    return RetrievalReport.model_validate_json(path.read_text()) if path else None


@cli.command()
def metrics(
    hours: float = 24.0,
    tenant: Annotated[str | None, typer.Option(help="Tenant; omit for all tenants")] = None,
) -> None:
    """Operational metrics over the last N hours (latency, errors, retries, tokens, cost)."""

    async def run(services: Services) -> None:
        now = datetime.now(UTC)
        typer.echo(
            json.dumps(
                await operational_metrics(
                    services.sessions, start=now - timedelta(hours=hours), end=now, tenant_id=tenant
                ),
                indent=2,
            )
        )

    asyncio.run(_with_services(run))


def main() -> None:
    cli()
