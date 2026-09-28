from threading import Barrier, Lock
import json
import time

import pytest

from memory_bench_platform import workflow
from memory_bench_platform.cli import build_parser, _execution_options
from memory_bench_platform.integration import adapter_environment, _adapter_environment
from memory_bench_platform.protocol import CaseRecord, ExecutionSpec, JudgeResult, StepRecord, WorkflowRuntimeContext


def plan(integration="backend_direct", checkpoints=1, questions=4):
    cases, steps, previous = [], [], []
    for checkpoint in range(checkpoints):
        setup = f"setup-{checkpoint}"
        cases.append(CaseRecord(case_id=setup, run_id="run", title=setup, goal="write",
            capability="memory/setup", judge_mode="none", depends_on_cases=previous))
        steps.append(StepRecord(step_id=setup, case_id=setup, name="write", operator_kind="memory",
            inputs={"action": "ingest"}))
        previous = []
        for number in range(questions):
            qid = f"c{checkpoint}-q{number}"
            previous.append(qid)
            cases.append(CaseRecord(case_id=qid, run_id="run", title=qid, goal="answer",
                capability="memory/question-answering", judge_mode="external", depends_on_cases=[setup],
                reference={"sample_id": "sample", "checkpoint_id": f"c{checkpoint}", "evaluation_target": "qa_answer"}))
            dependencies = []
            if integration == "backend_direct":
                dependencies = [qid + "-recall"]
                steps.append(StepRecord(step_id=dependencies[0], case_id=qid, name="recall", operator_kind="memory",
                    inputs={"action": "recall"}))
            steps.append(StepRecord(step_id=qid + "-answer", case_id=qid, name="answer", operator_kind="agent",
                depends_on=dependencies, inputs={"metadata": {"session_key": qid, "purpose": "memory_qa"}}))
    for step in steps:
        step.gate_policy = "hard"
    return cases, steps


def execute(tmp_path, cases, steps, integration="backend_direct", qa=1, judge=1):
    return workflow.execute_cases(run_id="run", agent_id="openclaw", memory_id="mem",
        cases=cases, steps=steps, execution_spec=ExecutionSpec(qa_concurrency=qa, judge_concurrency=judge),
        run_dir=tmp_path, runtime_context=WorkflowRuntimeContext(run_id="run", run_dir=str(tmp_path),
            benchmark_id="locomo", agent_id="openclaw", memory_id="mem", memory_integration=integration,
            memory_plugin_id="plugin" if integration == "agent_plugin" else None))


def grade(run_id, judge_input, **kwargs):
    return JudgeResult(judge_id=judge_input.case_id, run_id=run_id, case_id=judge_input.case_id,
        passed=True, score=1.0, label="correct", rationale="test")


@pytest.mark.parametrize("integration,qa", [("backend_direct", 2), ("agent_plugin", 1)])
def test_checkpoint_parallelism_limits_context_and_archival(tmp_path, monkeypatch, integration, qa):
    cases, steps = plan(integration, checkpoints=2)
    lock, qa_barrier, judge_barrier = Lock(), Barrier(qa), Barrier(2)
    active = {"qa": 0, "judge": 0}
    peak = dict(active)
    graded, recalled = set(), set()
    def operator(step, **kwargs):
        assert _adapter_environment.get()["BENCH_TEST_CONTEXT"] == "scoped"
        if step.inputs.get("action") == "ingest":
            if step.case_id == "setup-1":
                assert len(graded) == 4  # no next-checkpoint writes before all Judges finish
            return {"status": "ok", "output": {}}
        if step.inputs.get("action") == "recall":
            with lock: recalled.add(step.case_id)
            return {"status": "ok", "output": {"evidence_text": step.case_id}}
        if integration == "backend_direct": assert step.case_id in recalled
        with lock:
            active["qa"] += 1
            peak["qa"] = max(peak["qa"], active["qa"])
        qa_barrier.wait(timeout=5)
        time.sleep(0.01)
        with lock: active["qa"] -= 1
        return {"status": "ok", "turns": [{"text": step.case_id}]}
    def judge(run_id, judge_input, **kwargs):
        assert _adapter_environment.get()["BENCH_TEST_CONTEXT"] == "scoped"
        with lock:
            active["judge"] += 1
            peak["judge"] = max(peak["judge"], active["judge"])
        judge_barrier.wait(timeout=5)
        # Answers are already archived while grading is in flight.
        snapshots = [json.loads(p.read_text()) for p in (tmp_path / "records/case-progress").glob("*.json")]
        own = next(row for row in snapshots if row["case_id"] == judge_input.case_id)
        assert own["judge_pending"] and own["step_results"]
        with lock:
            active["judge"] -= 1
            graded.add(judge_input.case_id)
        return grade(run_id, judge_input)
    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    monkeypatch.setattr(workflow, "run_llm_judge", judge)
    with adapter_environment({"BENCH_TEST_CONTEXT": "scoped"}):
        result = execute(tmp_path, cases, steps, integration, qa=qa, judge=2)
    assert peak == {"qa": qa, "judge": 2}
    assert all(r.passed for r in result["judge_results"])
    assert [r.case_id for r in result["judge_results"]] == [c.case_id for c in cases if c.judge_mode != "none"]
    assert [r.step_id for r in result["step_results"]] == [s.step_id for s in steps]
    rows = [json.loads(p.read_text()) for p in (tmp_path / "records/case-progress").glob("*.json")]
    assert len(rows) == len(cases) and all(not row["judge_pending"] for row in rows)


def test_default_execution_remains_strictly_serial(tmp_path, monkeypatch):
    cases, steps = plan(questions=2)
    events = []
    def operator(step, **kwargs):
        events.append(step.step_id)
        return {"status": "ok", "turns": [{"text": "answer"}]}
    def judge(run_id, ji, **kwargs):
        events.append(ji.case_id + "-judge")
        return grade(run_id, ji)
    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    monkeypatch.setattr(workflow, "run_llm_judge", judge)
    execute(tmp_path, cases, steps)
    assert events == ["setup-0", "c0-q0-recall", "c0-q0-answer", "c0-q0-judge",
                      "c0-q1-recall", "c0-q1-answer", "c0-q1-judge"]


def test_parallel_failures_preserve_other_results(tmp_path, monkeypatch):
    cases, steps = plan(questions=3)
    def operator(step, **kwargs):
        if step.step_id == "c0-q0-recall": raise RuntimeError("recall failed")
        return {"status": "ok", "turns": [{"text": step.step_id}]}
    def judge(run_id, ji, **kwargs):
        if ji.case_id == "c0-q1": raise ValueError("judge unavailable")
        return grade(run_id, ji)
    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    monkeypatch.setattr(workflow, "run_llm_judge", judge)
    result = execute(tmp_path, cases, steps, qa=2, judge=2)
    assert [r.label for r in result["judge_results"]] == ["runtime-error", "judge-error", "correct"]
    assert len(list((tmp_path / "records/case-progress").glob("*.json"))) == 4
    assert sum(t.event_type == "case_judge_started" for t in result["traces"]) == 3
    assert sum(t.event_type == "case_judge_finished" for t in result["traces"]) == 3
    assert any(r.step_id == "c0-q1-answer" and r.status == "passed" for r in result["step_results"])


def test_failed_setup_skips_all_qa(tmp_path, monkeypatch):
    cases, steps = plan(questions=3)
    def operator(step, **kwargs):
        assert step.step_id == "setup-0"
        return {"status": "failed", "error_message": "write failed"}
    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    monkeypatch.setattr(workflow, "run_llm_judge", lambda *a, **k: pytest.fail("must not grade"))
    result = execute(tmp_path, cases, steps, qa=2, judge=2)
    assert len(result["judge_results"]) == 3
    assert all(r.passed is None for r in result["judge_results"])
    assert all(r.status == "skipped" for r in result["step_results"][1:])


def test_plugin_rejects_concurrent_qa_before_execution(tmp_path, monkeypatch):
    cases, steps = plan("agent_plugin")
    monkeypatch.setattr(workflow, "_execute_step_operator", lambda *a, **k: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="qa_concurrency=1"):
        execute(tmp_path, cases, steps, "agent_plugin", qa=2)


def test_duplicate_qa_sessions_rejected(tmp_path):
    cases, steps = plan(questions=2)
    steps[-1].inputs["metadata"]["session_key"] = steps[-3].inputs["metadata"]["session_key"].upper()
    with pytest.raises(ValueError, match="unique session"):
        execute(tmp_path, cases, steps, qa=2)


@pytest.mark.parametrize("flag", ["--qa-concurrency", "--judge-concurrency"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "1.5"])
def test_cli_rejects_invalid_concurrency(flag, value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--benchmark", "locomo", "--agent", "openclaw", flag, value])


def test_cli_overrides_scenario_and_preserves_defaults():
    parser = build_parser()
    args = parser.parse_args(["run", "--benchmark", "locomo", "--agent", "openclaw", "--judge-concurrency", "4"])
    assert _execution_options(args, {"qa_concurrency": 2}).qa_concurrency == 2
    assert _execution_options(args).judge_concurrency == 4
    assert _execution_options(args).qa_concurrency == 1
    args.memory_integration = "agent_plugin"
    assert _execution_options(args).judge_concurrency == 4
    with pytest.raises(ValueError, match="qa-concurrency 1"):
        _execution_options(args, {"qa_concurrency": 2})


def test_plan_cli_reports_requested_concurrency(capsys):
    from memory_bench_platform.cli import main
    main(["plan-run", "--benchmark", "locomo", "--agent", "openclaw", "--memory-backend", "ogmemory",
          "--qa-concurrency", "4", "--judge-concurrency", "3"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_spec"]["qa_concurrency"] == 4
    assert payload["execution_spec"]["judge_concurrency"] == 3
