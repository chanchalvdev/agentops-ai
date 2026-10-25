"""Admin CLI. Talks to the database and models directly (no HTTP server needed).

agentops ingest datasets/corpus --tenant tenant_a
agentops search "RPO for payments" --mode hybrid
agentops ask "What is the payments RPO in v2?"
agentops run "What is the RPO in v2, in seconds?" --mode graph --trace
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.ingestion.parsers import SUPPORTED_TYPES
from app.retrieval.schemas import RetrievalMode
from app.services import Services, build_services

cli = typer.Typer(no_args_is_help=True, add_completion=False)
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
        ctx = services.agents.new_context(tenant, "cli")
        r = await services.agents.run(ctx, mode, question)  # type: ignore[arg-type]
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

    asyncio.run(_with_services(run))


def main() -> None:
    cli()
