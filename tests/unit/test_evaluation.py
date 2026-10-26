"""Evaluation framework: dataset validation, every deterministic evaluator (pass and fail), the
aggregate report, the judge's fail-safe behaviour, and the regression gate."""

import json
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.agents.result import AgentRunResult
from app.core.errors import InvalidRequestError
from app.evaluation.dataset import EvalCase, load_dataset
from app.evaluation.evaluators import CaseRun, EvaluatorResult, evaluate
from app.evaluation.judge import JudgeScores, judge
from app.evaluation.regression import Thresholds, check, compare
from app.evaluation.report import CaseReport, DatasetInfo, RunReport, aggregate, summary
from app.llm.provider import FakeProvider, LLMRouter, Route
from app.llm.schemas import ToolCall
from tests.unit.test_agent_graph import GraphModel, make_ctx, make_runner

GOLDEN = Path("datasets/golden_questions.jsonl")


def case(**kw: Any) -> EvalCase:
    return EvalCase.model_validate(
        {"id": "c1", "category": "answerable", "question": "q", "dataset_version": "t", **kw}
    )


def by_name(results: list[EvaluatorResult]) -> dict[str, EvaluatorResult]:
    return {r.name: r for r in results}


async def answered_run() -> AgentRunResult:
    runner, _, _ = make_runner(GraphModel())
    return await runner.run(make_ctx(), "What is the RPO?")


async def calculator_run() -> AgentRunResult:
    model = GraphModel(
        classification={"intent": "calculation", "needs_tools": True},
        tool_calls=[[ToolCall(id="c", name="calculator", arguments={"expression": "5*60"})]],
        answers=[{"answer": "300 seconds [T1].", "citations": ["T1"]}],
    )
    runner, _, _ = make_runner(model)
    return await runner.run(make_ctx(), "5 minutes in seconds?")


# --- dataset ------------------------------------------------------------------------------------


def test_golden_dataset_meets_the_spec() -> None:
    data = load_dataset(GOLDEN)
    assert data.version == "golden-v1" and len(data.cases) >= 30
    counts = Counter(c.category for c in data.cases)
    for category in ("answerable", "unanswerable", "multi_document", "tool_use", "adversarial"):
        assert counts[category] >= 5, category
    corpus = {p.name for p in Path("datasets/corpus").iterdir()}
    for c in data.cases:  # every referenced source exists in the evaluation corpus
        assert set(c.expected_sources) | set(c.required_citations) <= corpus, c.id


def test_dataset_validation_is_strict(tmp_path: Path) -> None:
    base = {"id": "a1", "category": "answerable", "question": "q", "dataset_version": "v1"}
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({**base, "expected_statuss": ["answered"]}) + "\n")
    with pytest.raises(InvalidRequestError, match="bad.jsonl:1"):
        load_dataset(bad)  # a typo in a field name must not silently disable a check
    bad.write_text(json.dumps(base) + "\n" + json.dumps(base) + "\n")
    with pytest.raises(InvalidRequestError, match="duplicate"):
        load_dataset(bad)
    bad.write_text(
        json.dumps(base) + "\n" + json.dumps({**base, "id": "a2", "dataset_version": "v2"})
    )
    with pytest.raises(InvalidRequestError, match="mixed"):
        load_dataset(bad)


# --- evaluators ---------------------------------------------------------------------------------


async def test_evaluators_pass_on_a_correct_grounded_answer() -> None:
    result = await answered_run()
    c = case(
        expected_status=["answered"],
        expected_sources=["payments.md"],
        required_citations=["payments.md"],
        must_contain=[["5 min", "five minutes"]],
        must_not_contain=["15 min"],
        forbidden_tools=["save_note"],
    )
    results = by_name(evaluate(CaseRun(c, result, max_steps=20)))
    for name in (
        "expected_behavior",
        "citation_validity",
        "source_retrieval",
        "required_citations",
        "structured_output",
        "unauthorized_tools",
        "max_steps",
        "answer_content",
    ):
        assert results[name].passed is True, (name, results[name].detail)
    for name in ("tool_usage", "approval_gate", "workspace_effects"):
        assert results[name].passed is None  # nothing to check: not applicable


async def test_evaluators_explain_failures() -> None:
    result = await answered_run()
    c = case(
        expected_status=["insufficient_evidence"],
        expected_sources=["other.md"],
        required_citations=["other.md"],
        must_contain=[["300 seconds"]],
        must_not_contain=["RPO"],
        expected_tools=["calculator"],
        expect_approval_any=["save_note"],
    )
    results = by_name(evaluate(CaseRun(c, result, max_steps=2)))
    assert "status answered" in results["expected_behavior"].detail
    assert "other.md" in results["source_retrieval"].detail
    assert "other.md" in results["required_citations"].detail
    assert "missing one of" in results["answer_content"].detail
    assert "forbidden 'RPO'" in results["answer_content"].detail
    assert "calculator never succeeded" in results["tool_usage"].detail
    assert results["approval_gate"].passed is False
    assert results["max_steps"].passed is False


async def test_tool_argument_and_result_correctness() -> None:
    result = await calculator_run()
    ok = case(
        expected_tools=["calculator"],
        expected_tool_args={"calculator": {"expression": "5*60"}},
        expected_tool_results={"calculator": {"result": 300}},
    )
    assert by_name(evaluate(CaseRun(ok, result, 20)))["tool_usage"].passed is True
    wrong = case(expected_tool_results={"calculator": {"result": 3000}})
    detail = by_name(evaluate(CaseRun(wrong, result, 20)))["tool_usage"].detail
    assert "never returned {'result': 3000}" in detail and "300.0" in detail


async def test_unauthorized_tool_execution_is_caught() -> None:
    result = await calculator_run()
    c = case(forbidden_tools=["calculator"])
    assert (
        "executed: ['calculator']"
        in by_name(evaluate(CaseRun(c, result, 20)))["unauthorized_tools"].detail
    )


async def test_leaked_secrets_in_tool_output_count_as_exposed() -> None:
    result = await calculator_run()
    c = case(must_not_contain=["5*60"])  # appears only in the returned tool reference
    assert by_name(evaluate(CaseRun(c, result, 20)))["answer_content"].passed is False


async def test_workspace_effects() -> None:
    result = await answered_run()
    c = case(
        expect_files={"a.py": ["def f"]}, expect_absent=["pwned.txt"], expect_unchanged=["b.md"]
    )
    run = CaseRun(
        c,
        result,
        20,
        files_before={"b.md": "x", "a.py": None, "pwned.txt": None},
        files_after={"b.md": "x", "a.py": "def f(): ...", "pwned.txt": None},
    )
    assert by_name(evaluate(run))["workspace_effects"].passed is True
    run.files_after = {"b.md": "changed", "a.py": None, "pwned.txt": "owned"}
    detail = by_name(evaluate(run))["workspace_effects"].detail
    assert "a.py missing" in detail and "pwned.txt exists" in detail and "b.md changed" in detail


# --- judge --------------------------------------------------------------------------------------


async def test_judge_scores_are_normalized_and_invalid_output_is_none() -> None:
    result = await answered_run()
    scores = {"correctness": 5, "groundedness": 3, "relevance": 5, "completeness": 1}
    good = FakeProvider(lambda r: json.dumps({**scores, "explanation": "ok"}))
    router = LLMRouter({"main": [Route(good, "m")], "small": [Route(good, "s")]}, max_retries=0)
    out = await judge(router, case(reference_answer="5 minutes"), result)
    assert isinstance(out, JudgeScores)
    assert out.normalized() == {
        "correctness": 1.0,
        "groundedness": 0.5,
        "relevance": 1.0,
        "completeness": 0.0,
    }
    assert "5 minutes" in good.requests[0].messages[1].content  # reference answer is shown
    bad = FakeProvider(lambda r: "not json")
    router = LLMRouter({"main": [Route(bad, "m")], "small": [Route(bad, "s")]}, max_retries=0)
    assert await judge(router, case(), result) is None  # missing, never a fake zero


# --- report and regression ----------------------------------------------------------------------


def report(passes: dict[str, bool | None], sha: str = "abc", p95: float = 1000.0) -> RunReport:
    cases = [
        CaseReport(
            id=cid,
            category="adversarial" if cid.startswith("adv") else "answerable",
            passed=ok,
            status="answered" if ok is not None else "skipped",
            latency_ms=p95,
            evaluators=[EvaluatorResult(name="unauthorized_tools", passed=ok)]
            if cid.startswith("adv")
            else [],
            failures=[] if ok else ["x: y"],
        )
        for cid, ok in passes.items()
    ]
    return RunReport(
        run_id=uuid.uuid4(),
        dataset=DatasetInfo(name="golden", version="v1", sha256=sha, cases=len(cases)),
        mode="graph",
        config={"models": {"main": "m"}},
        started_at=datetime.now(UTC),
        aggregate=aggregate(cases),
        cases=cases,
    )


def test_aggregate_and_summary() -> None:
    r = report({"a1": True, "a2": False, "adv1": True, "s1": None})
    a = r.aggregate
    assert (a["total"], a["passed"], a["skipped"], a["pass_rate"]) == (3, 2, 1, 0.6667)
    assert a["by_category"]["adversarial"]["pass_rate"] == 1.0
    assert a["by_evaluator"]["unauthorized_tools"] == {
        "applicable": 1,
        "passed": 1,
        "pass_rate": 1.0,
    }
    text = summary(r)
    assert "Passed 2/3" in text and "a2 [answered]: x: y" in text


def test_regression_gate() -> None:
    t = Thresholds(min_pass_rate=0.5, max_pass_rate_drop=0.05)
    base = report({"a1": True, "a2": True, "a3": True, "adv1": True})
    same = report({"a1": True, "a2": True, "a3": True, "adv1": True})
    assert compare(base, same, t).ok and check(same, base, t) == []

    worse = report({"a1": True, "a2": False, "a3": True, "adv1": False}, p95=5000.0)
    result = compare(base, worse, t)
    joined = " | ".join(result.breaches)
    assert "pass rate dropped" in joined
    assert "evaluator unauthorized_tools dropped" in joined  # zero tolerance
    assert "p95 latency" in joined
    assert result.newly_failing == ["a2", "adv1"]
    assert "newly failing cases: ['a2', 'adv1']" in joined
    one_flip = compare(base, report({"a1": True, "a2": True, "a3": True, "adv1": False}), t)
    assert any("newly failing" in b for b in one_flip.breaches)

    crashed = report({"a1": True, "a2": True, "a3": True, "adv1": True})
    crashed.cases[0].status = "error"
    assert "cases crashed: ['a1']" in compare(base, crashed, t).breaches
    filtered = report({"a1": True, "a2": True, "a3": True, "adv1": True})
    filtered.config["case_filter"] = {"ids": ["a1"]}
    assert any("case_filter differs" in b for b in compare(base, filtered, t).breaches)

    other = report({"a1": True}, sha="different")
    assert "not comparable" in compare(base, other, t).breaches[0]
    floor = report({"a1": False, "a2": False})
    assert check(floor, None, t) == ["pass rate 0.0 below floor 0.5"]


def test_thresholds_file_is_valid() -> None:
    t = Thresholds.model_validate_json(Path("datasets/eval_thresholds.json").read_text())
    assert "unauthorized_tools" in t.zero_tolerance_evaluators
