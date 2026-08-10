from pathlib import Path
from types import SimpleNamespace
import urllib.error

from memory_bench_platform.protocol import (
    CaseRecord,
    ExecutionSpec,
    JudgeResult,
    StepRecord,
    WorkflowRuntimeContext,
)
from memory_bench_platform.workflow import execute_cases


def test_workflow_retries_failed_step(monkeypatch, tmp_path: Path):
    attempts = {"count": 0}

    def fake_run_agent_task(skill_id: str, rendered_input):
        del skill_id, rendered_input
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ValueError("temporary failure")
        return {"status": "ok", "turns": [{"text": "expected"}]}

    monkeypatch.setattr("memory_bench_platform.workflow.run_agent_task", fake_run_agent_task)

    case = CaseRecord(
        case_id="case-1",
        run_id="run-1",
        title="retry case",
        goal="answer question",
        capability="memory/question-answering",
        reference={"expected_answer": "expected"},
    )
    step = StepRecord(
        step_id="step-1",
        case_id="case-1",
        name="agent_query",
        operator_kind="agent",
        retry_limit=1,
        gate_policy="hard",
        inputs={"question": "hello", "metadata": {}},
    )
    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[case],
        steps=[step],
        execution_spec=ExecutionSpec(fail_fast=True),
        run_dir=tmp_path,
    )
    assert attempts["count"] == 2
    assert output["step_results"][0].attempt == 2
    assert output["judge_results"][0].passed is True


def test_workflow_skips_step_when_dependency_failed(monkeypatch, tmp_path: Path):
    def fake_run_agent_task(skill_id: str, rendered_input):
        del skill_id, rendered_input
        raise ValueError("permanent failure")

    monkeypatch.setattr("memory_bench_platform.workflow.run_agent_task", fake_run_agent_task)

    case = CaseRecord(
        case_id="case-1",
        run_id="run-1",
        title="dep case",
        goal="answer question",
        capability="memory/question-answering",
        reference={"expected_answer": "expected"},
    )
    first = StepRecord(
        step_id="step-1",
        case_id="case-1",
        name="first",
        operator_kind="agent",
        gate_policy="hard",
        inputs={"question": "hello", "metadata": {}},
    )
    second = StepRecord(
        step_id="step-2",
        case_id="case-1",
        name="second",
        operator_kind="agent",
        depends_on=["step-1"],
        gate_policy="hard",
        inputs={"question": "world", "metadata": {}},
    )
    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[case],
        steps=[first, second],
        execution_spec=ExecutionSpec(fail_fast=True),
        run_dir=tmp_path,
    )
    assert output["step_results"][0].status == "failed"
    assert output["judge_results"][0].passed is None
    assert output["judge_results"][0].label == "runtime-error"


def test_workflow_supports_http_operator(monkeypatch, tmp_path: Path):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"healthy":true}'

    def fake_urlopen(request, timeout):
        del request, timeout
        return FakeResponse()

    monkeypatch.setattr("memory_bench_platform.workflow.urllib.request.urlopen", fake_urlopen)

    case = CaseRecord(
        case_id="case-http",
        run_id="run-1",
        title="health case",
        goal="check health",
        capability="service/health-check",
        reference={"expected_answer": '"healthy":true'},
    )
    step = StepRecord(
        step_id="step-http",
        case_id="case-http",
        name="http_health",
        operator_kind="http",
        gate_policy="hard",
        inputs={"method": "GET", "url": "http://example.test/health"},
    )
    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[case],
        steps=[step],
        execution_spec=ExecutionSpec(fail_fast=True),
        run_dir=tmp_path,
    )
    assert output["step_results"][0].status == "passed"
    assert output["judge_results"][0].passed is True


def test_workflow_captures_http_operator_failure(monkeypatch, tmp_path: Path):
    def fake_urlopen(request, timeout):
        del request, timeout
        raise urllib.error.HTTPError("http://example.test/health", 502, "Bad Gateway", hdrs=None, fp=None)

    monkeypatch.setattr("memory_bench_platform.workflow.urllib.request.urlopen", fake_urlopen)

    case = CaseRecord(
        case_id="case-http-fail",
        run_id="run-1",
        title="health fail",
        goal="check health",
        capability="service/health-check",
        reference={"expected_answer": '"healthy":true'},
    )
    step = StepRecord(
        step_id="step-http-fail",
        case_id="case-http-fail",
        name="http_health",
        operator_kind="http",
        gate_policy="hard",
        inputs={"method": "GET", "url": "http://example.test/health"},
    )
    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[case],
        steps=[step],
        execution_spec=ExecutionSpec(fail_fast=True),
        run_dir=tmp_path,
    )
    assert output["step_results"][0].status == "failed"
    assert output["judge_results"][0].passed is None
    assert output["judge_results"][0].label == "runtime-error"


def test_builtin_judge_can_target_expected_step(monkeypatch, tmp_path: Path):
    def fake_run_agent_task(skill_id: str, rendered_input):
        del skill_id
        content = rendered_input.messages[0]["content"]
        return {"status": "ok", "turns": [{"text": content}]}

    monkeypatch.setattr("memory_bench_platform.workflow.run_agent_task", fake_run_agent_task)

    case = CaseRecord(
        case_id="case-step-target",
        run_id="run-1",
        title="targeted judge",
        goal="use final step",
        capability="memory/question-answering",
        reference={"expected_answer": "second answer", "expected_step_id": "step-2"},
    )
    first = StepRecord(
        step_id="step-1",
        case_id="case-step-target",
        name="first",
        operator_kind="agent",
        gate_policy="hard",
        inputs={"question": "first answer", "metadata": {}},
    )
    second = StepRecord(
        step_id="step-2",
        case_id="case-step-target",
        name="second",
        operator_kind="agent",
        depends_on=["step-1"],
        gate_policy="hard",
        inputs={"question": "second answer", "metadata": {}},
    )
    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[case],
        steps=[first, second],
        execution_spec=ExecutionSpec(fail_fast=True),
        run_dir=tmp_path,
    )
    assert output["judge_results"][0].passed is True


def test_workflow_uses_external_llm_judge(monkeypatch, tmp_path: Path):
    def fake_run_agent_task(skill_id: str, rendered_input):
        del skill_id, rendered_input
        return {"status": "ok", "turns": [{"text": "May 7, 2023"}]}

    captured = {}

    def fake_llm_judge(run_id, judge_input, *, runtime_config):
        captured["run_id"] = run_id
        captured["question"] = judge_input.reference["question"]
        captured["runtime_config"] = runtime_config
        return JudgeResult(
            judge_id="case-llm",
            run_id=run_id,
            case_id=judge_input.case_id,
            score=1.0,
            label="correct",
            passed=True,
            rationale="Equivalent date.",
        )

    monkeypatch.setattr("memory_bench_platform.workflow.run_agent_task", fake_run_agent_task)
    monkeypatch.setattr("memory_bench_platform.workflow.run_llm_judge", fake_llm_judge)
    case = CaseRecord(
        case_id="case-llm",
        run_id="run-1",
        title="semantic judge",
        goal="answer",
        capability="memory/question-answering",
        reference={
            "question": "When?",
            "expected_answer": "7 May 2023",
            "expected_step_id": "answer-step",
        },
        judge_mode="external",
    )
    step = StepRecord(
        step_id="answer-step",
        case_id="case-llm",
        name="answer",
        operator_kind="agent",
        gate_policy="hard",
        inputs={"question": "When?", "metadata": {}},
    )
    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[case],
        steps=[step],
        execution_spec=ExecutionSpec(fail_fast=True),
        runtime_context=WorkflowRuntimeContext(
            run_id="run-1",
            run_dir=str(tmp_path),
            benchmark_id="locomo",
            agent_id="generic-cli",
            run_contract={"judge_runtime": {"mode": "external", "type": "llm"}},
        ),
        run_dir=tmp_path,
    )

    assert output["judge_results"][0].passed is True
    assert output["judge_results"][0].label == "correct"
    assert captured == {
        "run_id": "run-1",
        "question": "When?",
        "runtime_config": {"mode": "external", "type": "llm"},
    }


def test_agent_operator_passes_full_rendered_input_contract(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_run_agent_task(skill_id: str, rendered_input):
        captured["skill_id"] = skill_id
        captured["task_id"] = rendered_input.task_id
        captured["system_prompt"] = rendered_input.system_prompt
        captured["messages"] = rendered_input.messages
        captured["attachments"] = rendered_input.attachments
        captured["metadata"] = rendered_input.metadata
        return {"status": "ok", "turns": [{"text": "ok"}]}

    monkeypatch.setattr("memory_bench_platform.workflow.run_agent_task", fake_run_agent_task)

    case = CaseRecord(
        case_id="case-rendered-input",
        run_id="run-1",
        title="rendered input",
        goal="pass full rendered task input",
        capability="memory/question-answering",
        reference={"expected_answer": "ok"},
    )
    step = StepRecord(
        step_id="step-rendered-input",
        case_id="case-rendered-input",
        name="agent_query",
        operator_kind="agent",
        gate_policy="hard",
        inputs={
            "system_prompt": "Use the provided history to answer.",
            "messages": [
                {"role": "user", "content": "history turn 1"},
                {"role": "assistant", "content": "history turn 2"},
                {"role": "user", "content": "final question"},
            ],
            "attachments": ["artifacts/history.json"],
            "metadata": {"question_id": "q-1", "agent_id": "memory-eval"},
        },
    )
    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[case],
        steps=[step],
        execution_spec=ExecutionSpec(fail_fast=True),
        run_dir=tmp_path,
    )

    assert captured["skill_id"] == "generic-cli"
    assert captured["task_id"] == "step-rendered-input"
    assert captured["system_prompt"] == "Use the provided history to answer."
    assert captured["messages"][0]["content"] == "history turn 1"
    assert captured["messages"][-1]["content"] == "final question"
    assert captured["attachments"] == ["artifacts/history.json"]
    assert captured["metadata"]["question_id"] == "q-1"
    assert output["step_results"][0].structured_output["output"] == {"text": "ok"}
    assert output["judge_results"][0].passed is True


def test_workflow_applies_default_timeout_to_operator(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs["timeout"]
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("memory_bench_platform.workflow.subprocess.run", fake_run)
    case = CaseRecord(
        case_id="case-timeout",
        run_id="run-1",
        title="timeout",
        goal="use default timeout",
        capability="workflow/bash",
        reference={"expected_answer": "ok"},
    )
    step = StepRecord(
        step_id="bash-timeout",
        case_id="case-timeout",
        name="bash",
        operator_kind="bash",
        inputs={"cmd": ["echo", "ok"]},
    )

    execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[case],
        steps=[step],
        execution_spec=ExecutionSpec(default_timeout_seconds=7),
        run_dir=tmp_path,
    )

    assert captured == {"cmd": ["echo", "ok"], "timeout": 7}


def test_workflow_records_input_resolution_failure_without_running_operator(monkeypatch, tmp_path: Path):
    called = {"agent": False}

    def fake_run_agent_task(skill_id, rendered_input):
        del skill_id, rendered_input
        called["agent"] = True
        return {"status": "ok", "turns": [{"text": "unexpected"}]}

    monkeypatch.setattr("memory_bench_platform.workflow.run_agent_task", fake_run_agent_task)
    case = CaseRecord(
        case_id="case-resolution",
        run_id="run-1",
        title="resolution",
        goal="fail missing output path",
        capability="workflow/input-resolution",
        reference={"expected_answer": "unused"},
    )
    first = StepRecord(
        step_id="wait-first",
        case_id="case-resolution",
        name="wait",
        operator_kind="wait",
        inputs={"seconds": 0},
    )
    second = StepRecord(
        step_id="agent-second",
        case_id="case-resolution",
        name="agent",
        operator_kind="agent",
        depends_on=["wait-first"],
        gate_policy="hard",
        inputs={"question": {"$ref": "steps.wait-first.output.missing"}},
    )

    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[case],
        steps=[first, second],
        execution_spec=ExecutionSpec(fail_fast=True),
        run_dir=tmp_path,
    )

    assert called["agent"] is False
    assert output["step_results"][1].status == "failed"
    assert "missing reference path" in output["step_results"][1].gate_detail


def test_workflow_runs_qa_after_successful_setup_case(monkeypatch, tmp_path: Path):
    def fake_run_agent_task(skill_id, rendered_input):
        del skill_id
        return {"status": "ok", "turns": [{"text": rendered_input.messages[-1]["content"]}]}

    monkeypatch.setattr("memory_bench_platform.workflow.run_agent_task", fake_run_agent_task)
    setup = CaseRecord(
        case_id="sample-setup",
        run_id="run-1",
        title="setup",
        goal="prepare memory",
        capability="memory/ingest",
        judge_mode="none",
    )
    qa = CaseRecord(
        case_id="sample-q1",
        run_id="run-1",
        title="question",
        goal="answer",
        capability="memory/question-answering",
        depends_on_cases=["sample-setup"],
        reference={"expected_answer": "expected"},
    )
    steps = [
        StepRecord(
            step_id="setup-step",
            case_id="sample-setup",
            name="setup",
            operator_kind="wait",
            gate_policy="hard",
            inputs={"seconds": 0},
        ),
        StepRecord(
            step_id="qa-step",
            case_id="sample-q1",
            name="answer",
            operator_kind="agent",
            gate_policy="hard",
            inputs={"question": "expected"},
        ),
    ]

    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[setup, qa],
        steps=steps,
        execution_spec=ExecutionSpec(fail_fast=True),
        run_dir=tmp_path,
    )

    assert [item.step_id for item in output["step_results"]] == ["setup-step", "qa-step"]
    assert [item.case_id for item in output["judge_results"]] == ["sample-q1"]
    assert output["judge_results"][0].passed is True


def test_workflow_skips_qa_when_setup_case_fails(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run_agent_task(skill_id, rendered_input):
        del skill_id
        calls.append(rendered_input.task_id)
        if rendered_input.task_id == "setup-step":
            raise ValueError("setup failed")
        return {"status": "ok", "turns": [{"text": "unexpected"}]}

    monkeypatch.setattr("memory_bench_platform.workflow.run_agent_task", fake_run_agent_task)
    setup = CaseRecord(
        case_id="sample-setup",
        run_id="run-1",
        title="setup",
        goal="prepare memory",
        capability="memory/ingest",
        judge_mode="none",
    )
    qa = CaseRecord(
        case_id="sample-q1",
        run_id="run-1",
        title="question",
        goal="answer",
        capability="memory/question-answering",
        depends_on_cases=["sample-setup"],
        reference={"expected_answer": "expected"},
    )
    steps = [
        StepRecord(
            step_id="setup-step",
            case_id="sample-setup",
            name="setup",
            operator_kind="agent",
            gate_policy="hard",
            inputs={"question": "prepare"},
        ),
        StepRecord(
            step_id="qa-step",
            case_id="sample-q1",
            name="answer",
            operator_kind="agent",
            gate_policy="hard",
            inputs={"question": "expected"},
        ),
    ]

    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        cases=[setup, qa],
        steps=steps,
        execution_spec=ExecutionSpec(fail_fast=True),
        run_dir=tmp_path,
    )

    qa_result = next(item for item in output["step_results"] if item.step_id == "qa-step")
    assert calls == ["setup-step"]
    assert qa_result.status == "skipped"
    assert "sample-setup" in qa_result.gate_detail
