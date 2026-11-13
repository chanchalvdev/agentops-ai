"""Day 4 optimization switches (product.txt 12): each one changes only what it claims to.

Graph switches run on the scripted fake model from test_agent_graph; caches run against the real
harness and TTLCache with injected clocks.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.agents import nodes
from app.agents.harness import Harness
from app.agents.policies import is_comparison, is_simple_lookup, needs_calculation, needs_metadata
from app.agents.tuning import AgentTuning
from app.core.budgets import ExecutionLimits
from app.core.cache import TTLCache
from app.core.execution_context import ExecutionContext, LedgerEvent
from app.llm.provider import FakeProvider, LLMRouter, Route
from app.llm.schemas import LLMRequest, LLMResponse, ToolCall
from app.monitoring.cost import hosted_equivalent, summarize
from app.tools.calculator import Calculator
from app.tools.registry import ToolRegistry
from tests.unit.test_agent_graph import GraphModel, make_ctx, make_runner, nodes_run
from tests.unit.test_agent_loop import CHUNK_A, CHUNK_B, FakeSearch, passage


@dataclass
class ModelSpy(GraphModel):
    """GraphModel that also records which model (main/small) served each prompt."""

    served: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, request: LLMRequest) -> LLMResponse:
        before = len(self.calls)
        response = super().__call__(request)
        self.served += [(c, request.model) for c in self.calls[before:]]
        return response


def runner_with(model: GraphModel, tuning: AgentTuning, search: FakeSearch | None = None) -> Any:
    runner, _, _ = make_runner(model, search)
    runner.tuning = tuning
    return runner


# --- policies ----------------------------------------------------------------------------------


def test_routing_rules() -> None:
    assert needs_calculation("What is the v2 RTO in seconds?")
    assert needs_calculation("How much shorter is the RTO in v2 than in v1?")
    assert needs_calculation("What is 15 * 60?")
    assert not needs_calculation("What is the RPO in the latest payments architecture?")
    assert needs_metadata("Which version of the payments architecture is the latest version of it?")
    assert not needs_metadata("Who approves a failover?")
    assert is_simple_lookup("What is the recovery time objective in v2?")
    assert not is_simple_lookup("How did the RPO change between v1 and v2?")
    assert not is_simple_lookup("What is the RTO in seconds?")
    assert is_comparison("Compare the availability targets of payments architecture v1 and v2.")
    assert not is_comparison("What is the RPO in the latest payments architecture?")


# --- graph switches ----------------------------------------------------------------------------


async def test_query_rewrite_off_skips_the_plan_call() -> None:
    model, search = GraphModel(), FakeSearch()
    result = await runner_with(model, AgentTuning(query_rewrite=False), search).run(
        make_ctx(), "What is the RPO?"
    )
    assert result.status == "answered"
    assert "plan" not in model.calls and search.calls == 1


async def test_rule_routing_skips_tool_selection_for_plain_lookups() -> None:
    # The small model says tools are needed (the Day 3 baseline did this on most lookups).
    model = GraphModel(classification={"intent": "document_question", "needs_tools": True})
    ctx = make_ctx()
    result = await runner_with(model, AgentTuning(rule_routing=True)).run(ctx, "What is the RPO?")
    assert result.status == "answered"
    assert "tools" not in model.calls
    assert "select_or_execute_tools" not in nodes_run(ctx)


async def test_rule_routing_keeps_tool_selection_for_comparisons() -> None:
    # Measured regression: skipping it lost the document_metadata call (multi-availability 0/3).
    model = GraphModel(classification={"intent": "document_question", "needs_tools": False})
    ctx = make_ctx()
    await runner_with(model, AgentTuning(rule_routing=True)).run(
        ctx, "Compare the availability targets of v1 and v2."
    )
    assert "tools" in model.calls


async def test_without_rule_routing_the_model_flag_still_decides() -> None:
    model = GraphModel(classification={"intent": "document_question", "needs_tools": True})
    await runner_with(model, AgentTuning()).run(make_ctx(), "What is the RPO?")
    assert "tools" in model.calls


async def test_rule_routing_nudges_the_model_to_use_the_calculator() -> None:
    model = GraphModel(
        classification={"intent": "document_question", "needs_tools": False},
        tool_calls=[[], [ToolCall(id="c", name="calculator", arguments={"expression": "5*60"})]],
        answers=[{"answer": "300 seconds [S1][T1].", "citations": ["S1", "T1"]}],
    )
    ctx = make_ctx()
    result = await runner_with(model, AgentTuning(rule_routing=True)).run(
        ctx, "What is the RPO in seconds?"
    )
    assert result.status == "answered"
    assert model.calls.count("tools") == 2  # first ask declined, the nudge got the call
    names = [e.name for e in ctx.ledger if e.kind == "model_call"]
    assert "select_tools_calculator" in names
    assert result.tool_references[0].output["result"] == 300


async def test_simple_lookup_drafted_on_small_model_and_verified_on_main() -> None:
    model = ModelSpy()
    result = await runner_with(model, AgentTuning(route_simple_to_small=True)).run(
        make_ctx(), "What is the RPO?"
    )
    assert result.status == "answered"
    assert ("answer", "small") in model.served and ("verify", "main") in model.served


async def test_revision_after_a_rejected_small_draft_goes_to_main() -> None:
    model = ModelSpy(
        answers=[
            {"answer": "5 min [S9].", "citations": ["S9"]},  # invalid ID: revised
            {"answer": "5 min [S1].", "citations": ["S1"]},
        ]
    )
    await runner_with(model, AgentTuning(route_simple_to_small=True)).run(
        make_ctx(), "What is the RPO?"
    )
    answers = [m for c, m in model.served if c == "answer"]
    assert answers == ["small", "main"]


async def test_comparison_questions_stay_on_main() -> None:
    model = ModelSpy()
    await runner_with(model, AgentTuning(route_simple_to_small=True)).run(
        make_ctx(), "How did the RPO change between v1 and v2?"
    )
    assert ("answer", "main") in model.served and ("answer", "small") not in model.served


async def test_evidence_cap_limits_what_is_shown_and_citable() -> None:
    seen: list[dict[str, Any]] = []

    @dataclass
    class SchemaSpy(GraphModel):
        def __call__(self, request: LLMRequest) -> LLMResponse:
            if request.messages[0].content == nodes.ANSWER_PROMPT:
                seen.append(request.json_schema or {})
            return super().__call__(request)

    search = FakeSearch([passage(CHUNK_A, "A" * 900), passage(CHUNK_B, "B" * 900)])
    result = await runner_with(SchemaSpy(), AgentTuning(max_evidence_chars=1000), search).run(
        make_ctx(), "?"
    )
    assert result.status == "answered"
    enum = seen[0]["properties"]["citations"]["items"]["enum"]
    assert enum == ["S1"]  # S2 did not fit, so it cannot be cited


async def test_insufficient_draft_text_is_replaced() -> None:
    model = GraphModel(
        answers=[
            {
                "answer": "Probably 45 minutes.",  # an unverified claim inside a refusal
                "citations": [],
                "insufficient_evidence": True,
            }
        ]
    )
    result = await runner_with(model, AgentTuning()).run(make_ctx(), "What is the Kafka retention?")
    assert result.status == "insufficient_evidence"
    assert result.answer == nodes.INSUFFICIENT_TEXT and "45" not in result.answer


# --- caches ------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_ttl_cache_expires_and_evicts() -> None:
    clock = Clock()
    cache: TTLCache[int] = TTLCache(max_entries=2, ttl_s=10, clock=clock)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)  # evicts the least recently used: b
    assert cache.get("b") is None and cache.stats.evictions == 1
    clock.now = 11
    assert cache.get("a") is None  # expired
    assert cache.stats.hits == 1 and cache.stats.misses == 2


def harness_with_cache() -> Harness:
    router = LLMRouter(
        {"main": [Route(FakeProvider(), "m")], "small": [Route(FakeProvider(), "s")]},
        max_retries=0,
    )
    return Harness(router, ToolRegistry([Calculator()]), TTLCache(100, 60))


def ctx_for(tenant: str) -> ExecutionContext:
    return ExecutionContext(
        tenant_id=tenant,
        limits=ExecutionLimits(deadline_s=10),
        allowed_tools=frozenset({"calculator"}),
    )


async def test_tool_cache_hits_within_a_tenant_and_never_across() -> None:
    harness = harness_with_cache()
    call = ToolCall(id="1", name="calculator", arguments={"expression": "15*60"})
    a = ctx_for("tenant_a")
    first = await harness.call_tool(a, call)
    again = await harness.call_tool(a, call.model_copy(update={"id": "2"}))
    assert first.output == again.output and again.attempts == 0
    events = [e for e in a.ledger if e.kind == "tool_call"]
    assert [e.data.get("cache") for e in events] == ["miss", "hit"]
    assert a.counters.tool_calls == 2  # a hit still counts against the tool budget

    b = ctx_for("tenant_b")
    await harness.call_tool(b, call)
    assert [e.data.get("cache") for e in b.ledger if e.kind == "tool_call"] == ["miss"]


async def test_tools_without_a_cache_key_are_never_cached() -> None:
    class Echo(BaseModel):
        text: str

    from app.tools.base import Tool

    class EchoTool(Tool[Echo, Echo]):
        name = "echo"
        description = "echo"
        input_model = Echo
        output_model = Echo
        runs = 0

        async def run(self, args: Echo, ctx: ExecutionContext) -> Echo:
            EchoTool.runs += 1
            return args

    router = LLMRouter(
        {"main": [Route(FakeProvider(), "m")], "small": [Route(FakeProvider(), "s")]},
        max_retries=0,
    )
    harness = Harness(router, ToolRegistry([EchoTool()]), TTLCache(10, 60))
    ctx = ExecutionContext(
        tenant_id="t", limits=ExecutionLimits(deadline_s=10), allowed_tools=frozenset({"echo"})
    )
    for i in range(2):
        await harness.call_tool(ctx, ToolCall(id=str(i), name="echo", arguments={"text": "x"}))
    assert EchoTool.runs == 2


async def test_document_search_cache_key_follows_the_corpus_version() -> None:
    from app.tools.document_search import DocumentSearch, DocumentSearchInput

    version = {"tenant_a": "1:t0"}

    class Retriever:
        def config(self) -> dict[str, object]:
            return {"mode": "hybrid"}

    async def corpus_version(tenant: str) -> str:
        return version[tenant]

    tool = DocumentSearch(Retriever(), corpus_version=corpus_version)  # type: ignore[arg-type]
    args = DocumentSearchInput(query="rpo")
    ctx = ctx_for("tenant_a")
    before = await tool.cache_key(args, ctx)
    version["tenant_a"] = "2:t1"  # a document was added, deleted, or reindexed
    assert await tool.cache_key(args, ctx) != before
    assert await DocumentSearch(Retriever()).cache_key(args, ctx) is None  # type: ignore[arg-type]


# --- usage accounting ---------------------------------------------------------------------------


def event(seq: int, kind: str, name: str, status: str = "ok", **kw: Any) -> LedgerEvent:
    return LedgerEvent(seq=seq, kind=kind, name=name, status=status, **kw)  # type: ignore[arg-type]


def test_usage_summary_counts_retries_embeddings_and_cache_hits() -> None:
    ledger = [
        event(0, "model_call", "classify", input_tokens=100, output_tokens=10,
              data={"model": "small", "attempts": 1}),
        event(1, "model_call", "answer", input_tokens=900, output_tokens=90,
              data={"model": "main", "attempts": 3}),
        event(2, "model_call", "verify", "error", error_code="provider_timeout"),
        event(3, "tool_call", "document_search",
              data={"result": {"timings_ms": {"embed": 20.0}, "embed_cached": False}}),
        event(4, "tool_call", "document_search",
              data={"result": {"timings_ms": {"embed": 0.1}, "embed_cached": True}}),
        event(5, "tool_call", "calculator", data={"cache": "hit"}),
    ]  # fmt: skip
    s = summarize(ledger, Decimal("1"), Decimal("2"))
    assert (s.model_calls, s.retries, s.failed_model_calls) == (2, 2, 1)
    assert s.model_calls_by_model == {"small": 1, "main": 1}
    assert (s.embedding_calls, s.embedding_cache_hits, s.tool_cache_hits) == (1, 1, 1)
    assert s.hosted_equivalent_usd == round((1000 * 1 + 100 * 2) / 1e6, 6)
    assert summarize(ledger).hosted_equivalent_usd is None  # no reference prices: unavailable


def test_hosted_equivalent_needs_both_prices() -> None:
    assert hosted_equivalent(10, 10, Decimal("1"), None) is None
    assert hosted_equivalent(1_000_000, 0, Decimal("0.5"), Decimal("1")) == 0.5


def test_graph_tuning_comes_from_settings() -> None:
    from app.core.config import Settings

    s = Settings(env="test", agent_rule_routing=True, agent_query_rewrite=False)
    t = AgentTuning.from_settings(s)
    assert t.rule_routing and not t.query_rewrite and t.search_top_k == 5
