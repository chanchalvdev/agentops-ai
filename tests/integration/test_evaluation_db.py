"""Evaluation end to end on Postgres with a scripted model: corpus ingestion for the eval tenant,
a workspace case approved by the runner, a skipped case, persisted results, scores pushed to
telemetry, and the evaluation API (202 + poll, list, compare)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agents import nodes
from app.core.config import Settings
from app.evaluation.dataset import load_dataset
from app.evaluation.runner import EvaluationRunner
from app.evaluation.store import EvaluationStore
from app.llm.provider import FakeProvider
from app.llm.schemas import LLMRequest, LLMResponse, ToolCall
from app.observability.interfaces import Score, TraceRecord
from app.services import Services

pytestmark = pytest.mark.integration

CASES = [
    {
        "id": "ans-rpo",
        "category": "answerable",
        "question": "What is the RPO?",
        "expected_status": ["answered"],
        "expected_sources": ["dr.md"],
        "required_citations": ["dr.md"],
        "must_contain": [["5 minutes"]],
    },
    {
        "id": "ws-write",
        "category": "workspace",
        "question": "Create notes.md in my workspace",
        "modes": ["graph"],
        "approval": "approve",
        "expect_approval_any": ["workspace_write"],
        "expect_files": {"notes.md": ["hello"]},
    },
    {
        "id": "loop-only",
        "category": "answerable",
        "question": "Anything",
        "modes": ["loop"],
    },
]


def scripted(request: LLMRequest) -> LLMResponse:
    system, user = request.messages[0].content, request.messages[-1].content
    workspace = "workspace" in user
    calls: list[ToolCall] = []
    if system == nodes.CLASSIFY_PROMPT:
        intent = "workspace_task" if workspace else "document_question"
        content = json.dumps({"intent": intent, "needs_tools": workspace})
    elif system == nodes.PLAN_PROMPT:
        content = json.dumps({"search_queries": ["RPO"]})
    elif system == nodes.TOOLS_PROMPT:
        content = ""
        if workspace and "<tool_result" not in user:  # first round only
            calls = [
                ToolCall(
                    id="w1",
                    name="workspace_write",
                    arguments={"path": "notes.md", "content": "hello"},
                )
            ]
    elif system == nodes.ANSWER_PROMPT:
        label = "T1" if workspace else "S1"
        text = "Created notes.md" if workspace else "The RPO is 5 minutes"
        content = json.dumps(
            {"insufficient_evidence": False, "citations": [label], "answer": f"{text} [{label}]."}
        )
    elif system == nodes.VERIFY_PROMPT:
        content = json.dumps({"supported": True})
    else:
        raise AssertionError(system[:40])
    return LLMResponse(
        content=content, tool_calls=calls, model=request.model, provider="fake", latency_ms=1
    )


@pytest.fixture
def test_settings(test_settings: Settings, tmp_path: Path) -> Settings:
    corpus, datasets, seed = tmp_path / "corpus", tmp_path / "datasets", tmp_path / "seed"
    corpus.mkdir()
    (corpus / "dr.md").write_text("# DR\n\nThe Recovery Point Objective (RPO) is 5 minutes.\n")
    datasets.mkdir()
    rows = [{**c, "dataset_version": "t1"} for c in CASES]
    (datasets / "mini.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    seed.mkdir()
    (seed / "README.md").write_text("seed\n")
    return test_settings.model_copy(
        update={
            "eval_corpus_dirs": [corpus],
            "eval_datasets_dir": datasets,
            "eval_workspace_seed": seed,
        }
    )


class Recorder:
    name = "rec"

    def __init__(self) -> None:
        self.traces: list[TraceRecord] = []
        self.scores: list[tuple[Score, str]] = []

    def accepts(self, trace: TraceRecord) -> bool:
        return True

    async def export(self, trace: TraceRecord) -> None:
        self.traces.append(trace)

    async def score(self, score: Score, source: str) -> None:
        self.scores.append((score, source))


async def test_runner_end_to_end(
    services: Services, fake_llm: FakeProvider, test_settings: Settings
) -> None:
    fake_llm._responder = scripted
    sink = Recorder()
    services.telemetry.sinks.append(sink)
    dataset = load_dataset(test_settings.eval_datasets_dir / "mini.jsonl")
    report = await EvaluationRunner(services).run(dataset, mode="graph", requested_by="tenant_a")

    by_id = {c.id: c for c in report.cases}
    assert by_id["ans-rpo"].passed, by_id["ans-rpo"].failures
    assert by_id["ws-write"].passed and by_id["ws-write"].status == "answered"
    assert by_id["loop-only"].passed is None
    assert report.aggregate["pass_rate"] == 1.0 and report.aggregate["skipped"] == 1
    root = services.workspaces.for_tenant("eval").root
    assert (root / "notes.md").read_text() == "hello"  # the runner approved the write

    stored = await EvaluationStore(services.sessions).report("tenant_a", report.run_id)
    assert [c.id for c in stored.cases] == ["ans-rpo", "ws-write", "loop-only"]
    assert stored.aggregate == report.aggregate and stored.config["prompt_version"]

    await services.telemetry.flush()
    assert {t.source for t in sink.traces} == {"eval"}
    names = {s.name for s, source in sink.scores if source == "eval"}
    assert {"eval.passed", "eval.citation_validity", "eval.workspace_effects"} <= names


async def test_evaluation_api(
    client: httpx.AsyncClient, services: Services, fake_llm: FakeProvider
) -> None:
    fake_llm._responder = scripted
    started = await client.post(
        "/api/v1/evaluations/run", json={"dataset": "mini", "case_ids": ["ans-rpo"]}
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    assert services.eval_task is not None
    await services.eval_task

    run = (await client.get(f"/api/v1/evaluations/runs/{run_id}")).json()
    assert run["aggregate"]["passed"] == 1 and run["cases"][0]["id"] == "ans-rpo"
    runs = (await client.get("/api/v1/evaluations/runs")).json()
    assert runs[0]["id"] == run_id and runs[0]["status"] == "completed"
    same = (
        await client.get("/api/v1/evaluations/compare", params={"base": run_id, "head": run_id})
    ).json()
    assert same["breaches"] == []

    other_tenant = await client.get(
        f"/api/v1/evaluations/runs/{run_id}", headers={"X-API-Key": "dev-key-tenant-b"}
    )
    assert other_tenant.status_code == 404
    missing = await client.post("/api/v1/evaluations/run", json={"dataset": "nope"})
    assert missing.status_code == 404
    traversal: Any = await client.post("/api/v1/evaluations/run", json={"dataset": "../etc"})
    assert traversal.status_code == 422


async def test_runner_seeds_fixtures_the_agent_may_not_read(services: Services) -> None:
    """Live eval bug: seeding a planted .env through the agent's jail was refused."""
    case = load_dataset(Path("datasets/golden_questions.jsonl")).cases
    secret = next(c for c in case if c.id == "adv-secret-file")
    root = EvaluationRunner(services)._seed_workspace(secret)
    assert (root / ".env").read_text().startswith("EVAL_FAKE_SECRET")
    bad = secret.model_copy(update={"workspace_files": {"../escape.txt": "x"}})
    with pytest.raises(ValueError, match="escapes"):
        EvaluationRunner(services)._seed_workspace(bad)
