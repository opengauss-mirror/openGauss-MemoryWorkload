import json
from pathlib import Path

import pytest

from memory_bench_platform import workflow
from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding
from memory_bench_platform.cli import _summarize_native_evaluation
from memory_bench_platform.composer import compose_run_plan
from memory_bench_platform.protocol import CaseRecord, ExecutionSpec, StepRecord


def execute(tmp_path, cases, steps, backend="mem0"):
    return workflow.execute_cases(run_id="r", agent_id="generic-cli", memory_id=backend,
        cases=cases, steps=steps, execution_spec=ExecutionSpec(), run_dir=tmp_path)


@pytest.mark.parametrize("kind", ["ref", "template"])
@pytest.mark.parametrize("has_output", [False, True])
@pytest.mark.parametrize("explicit_dependency", [False, True])
@pytest.mark.parametrize("action", ["ingest", "flush", "poll"])
def test_preparation_skips_failed_input_references(monkeypatch, tmp_path, kind, has_output, explicit_dependency, action):
    seen = []

    def operator(step, **kwargs):
        seen.append(step.step_id)
        if step.step_id == "first":
            return {"status": "failed", "output": {"content": "FAILED"} if has_output else {},
                    "error_message": "original preparation failure"}
        return {"status": "ok", "output": {"evidence_text": "Paris"}, "turns": [{"text": "Paris"}]}

    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    cases = [CaseRecord(case_id="setup", run_id="r", title="setup", goal="history", labels=["phase:setup"],
                        capability="memory/ingest", judge_mode="none"),
             CaseRecord(case_id="qa", run_id="r", title="qa", goal="question", depends_on_cases=["setup"],
                        capability="memory/question-answering", reference={"evaluation_target": "qa_answer", "expected_answer": "Paris"})]
    def ref(step):
        return {"$ref": f"steps.{step}.output.content"} if kind == "ref" else {"$template": "{{steps." + step + ".output.content}}"}
    steps = [StepRecord(step_id=sid, case_id="setup", name=sid, operator_kind="memory", gate_policy="hard",
                        depends_on=deps, inputs={"action": "ingest", "nested": [{"content": content}]})
             for sid, deps, content in [("first", [], "source"), ("dependent", ["first"] if explicit_dependency else [], ref("first")),
                                        ("transitive", ["dependent"] if explicit_dependency else [], ref("dependent")),
                                        ("independent", ["transitive"], "safe")]]
    if action == "flush":
        steps[1].inputs["action"] = "flush"
    elif action == "poll":
        steps[1].operator_kind = "poll"
        steps[1].timeout_seconds = 1
        steps[1].inputs = {
            "probe": {"operator_kind": "memory", "action": "status",
                      "inputs": {"operation": ref("first")}},
            "success_when": {"path": "state", "equals": "completed"},
        }
    steps += [StepRecord(step_id="recall", case_id="qa", name="recall", operator_kind="memory",
                          gate_policy="hard", inputs={"action": "recall"}),
              StepRecord(step_id="answer", case_id="qa", name="answer", operator_kind="agent",
                          gate_policy="hard", depends_on=["recall"], inputs={})]
    result = execute(tmp_path, cases, steps)
    by_id = {r.step_id: r for r in result["step_results"]}
    assert seen == ["first", "independent", "recall", "answer"]
    assert by_id["dependent"].status == by_id["transitive"].status == "skipped"
    assert by_id["first"].gate_detail == "original preparation failure"
    assert by_id["answer"].structured_output["qa_degradation"] == {"setup_failed": ["setup"]}
    assert result["judge_results"][0].passed is True


@pytest.mark.parametrize("backend", ["mem0", "hindsight", "ogmemory"])
@pytest.mark.parametrize("first_fails", [True, False])
def test_composed_checkpoints_preserve_history_status(monkeypatch, tmp_path, backend, first_fails):
    scenario = BenchmarkScenario.model_validate(json.loads(
        Path("tests/golden/multi_checkpoint_scenario.json").read_text()))
    plan = compose_run_plan(scenario, RunBinding(benchmark_id=scenario.benchmark_id,
        agent_id="generic-cli", memory_id=backend, run_id="r"),
        {"memory": {"async_ingest": False, "commit": {"required_after_ingest": False}, "readiness": {"supported": False}}})
    cases = [CaseRecord(**c, run_id="r") for c in plan["cases"]]
    steps = [StepRecord(**s) for s in plan["steps"]]
    for case in cases:
        if case.judge_mode != "none":
            case.judge_mode = "builtin"

    def operator(step, **kwargs):
        if first_fails and "stage-1" in step.case_id and step.inputs.get("action") == "ingest":
            return {"status": "failed", "error_message": "history not stored"}
        return {"status": "ok", "output": {"evidence_text": "Paris"}, "turns": [{"text": "Paris"}]}

    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    result = execute(tmp_path, cases, steps, backend)
    summary = _summarize_native_evaluation(cases, steps, result["judge_results"], result["step_results"])
    assert summary["checkpoint_ready_rate"] == (0.0 if first_fails else 1.0)
    assert summary["qa_paths"]["setup_failed"]["graded"] == (2 if first_fails else 0)
    assert summary["qa_paths"]["normal"]["graded"] == (0 if first_fails else 2)
    assert all(r.status == "passed" for r in result["step_results"] if "stage-2" in r.step_id)
    answers = [r for r in result["step_results"] if "agent-answer" in r.step_id]
    assert len(answers) == 2
    assert all(bool(r.structured_output.get("qa_degradation")) == first_fails for r in answers)
