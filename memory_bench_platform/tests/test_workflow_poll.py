import importlib.util
from pathlib import Path

from memory_bench_platform.protocol import (
    CaseRecord,
    ExecutionSpec,
    MemoryTaskOutput,
    StepRecord,
    WorkflowRuntimeContext,
)
from memory_bench_platform.workflow import execute_cases


def _case() -> CaseRecord:
    return CaseRecord(
        case_id="case-1",
        run_id="run-1",
        title="memory workflow",
        goal="wait for memory ingest",
        capability="memory/store-retrieve",
        reference={"expected_answer": "unused"},
    )


def _runtime_context(tmp_path: Path) -> WorkflowRuntimeContext:
    return WorkflowRuntimeContext(
        run_id="run-1",
        run_dir=str(tmp_path),
        benchmark_id="ovtest-memory",
        agent_id="generic-cli",
        memory_id="openviking",
    )


def _memory_steps(*, interval_seconds: float = 0) -> list[StepRecord]:
    return [
        StepRecord(
            step_id="ingest",
            case_id="case-1",
            name="ingest",
            operator_kind="memory",
            gate_policy="hard",
            inputs={"action": "ingest", "content": "private fact"},
        ),
        StepRecord(
            step_id="poll-ingest",
            case_id="case-1",
            name="poll ingest",
            operator_kind="poll",
            depends_on=["ingest"],
            timeout_seconds=5,
            gate_policy="hard",
            inputs={
                "interval_seconds": interval_seconds,
                "probe": {
                    "operator_kind": "memory",
                    "action": "status",
                    "inputs": {
                        "operation": {"$ref": "steps.ingest.output.operation"},
                    },
                },
                "success_when": {"path": "state", "equals": "completed"},
                "failure_when": {"path": "state", "equals": "failed"},
            },
        ),
    ]


def test_poll_memory_probe_runs_until_completed_and_records_evidence(monkeypatch, tmp_path: Path):
    status_requests = []

    def fake_run_memory_task(skill_id, request):
        assert skill_id == "openviking"
        if request.action == "ingest":
            return MemoryTaskOutput(
                status="ok",
                state="accepted",
                operation={"task_id": "task-1"},
                output={"resource_id": "resource-1"},
                metrics=[{"name": "memory_backend_ms", "value": 4, "unit": "ms"}],
                artifacts=[{"kind": "memory_operation", "path": "artifacts/task-1.json"}],
            )
        status_requests.append(request)
        state = "running" if len(status_requests) == 1 else "completed"
        return MemoryTaskOutput(
            status="ok",
            state=state,
            operation=request.inputs["operation"],
            output={},
            metrics=[{"name": "status_probe_ms", "value": len(status_requests), "unit": "ms"}],
        )

    monkeypatch.setattr("memory_bench_platform.workflow.run_memory_task", fake_run_memory_task)

    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        memory_id="openviking",
        runtime_context=_runtime_context(tmp_path),
        cases=[_case()],
        steps=_memory_steps(),
        execution_spec=ExecutionSpec(case_mode="single_path", fail_fast=True),
        run_dir=tmp_path,
    )

    assert len(status_requests) == 2
    assert status_requests[0].inputs["operation"] == {"task_id": "task-1"}
    poll_result = output["step_results"][1]
    assert poll_result.status == "passed"
    assert poll_result.structured_output["output"]["poll_count"] == 2
    assert poll_result.structured_output["output"]["last_probe"]["state"] == "completed"
    assert len([trace for trace in output["traces"] if trace.event_type == "poll_probe"]) == 2
    assert {metric.name for metric in output["metrics"]} >= {"memory_backend_ms", "poll_count"}
    assert len([metric for metric in output["metrics"] if metric.name == "status_probe_ms"]) == 2
    assert any(artifact.kind == "memory_operation" for artifact in output["artifacts"])


def test_poll_times_out_using_step_timeout(monkeypatch, tmp_path: Path):
    clock = {"now": 0.0}

    def fake_run_memory_task(skill_id, request):
        del skill_id
        if request.action == "ingest":
            return MemoryTaskOutput(status="ok", state="accepted", operation={"task_id": "task-1"})
        return MemoryTaskOutput(status="ok", state="running", operation=request.inputs["operation"])

    def fake_sleep(seconds: float):
        clock["now"] += seconds

    monkeypatch.setattr("memory_bench_platform.workflow.run_memory_task", fake_run_memory_task)
    monkeypatch.setattr("memory_bench_platform.workflow._sleep", fake_sleep)
    monkeypatch.setattr("memory_bench_platform.workflow._monotonic", lambda: clock["now"])

    steps = _memory_steps(interval_seconds=0.5)
    steps[1].timeout_seconds = 1
    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        memory_id="openviking",
        runtime_context=_runtime_context(tmp_path),
        cases=[_case()],
        steps=steps,
        execution_spec=ExecutionSpec(case_mode="single_path", fail_fast=True),
        run_dir=tmp_path,
    )

    poll_result = output["step_results"][1]
    assert poll_result.status == "failed"
    assert poll_result.structured_output["output"]["poll_count"] == 3
    assert "timed out" in poll_result.gate_detail


def test_ovtest_memory_native_workflow_renders_recall_evidence_into_agent(monkeypatch, tmp_path: Path):
    builder_path = Path("skills/benchmarks/ovtest-memory/scripts/build_tasks.py")
    spec = importlib.util.spec_from_file_location("ovtest_memory_builder", builder_path)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    payload = builder.build_cases()
    cases = [CaseRecord(run_id="run-1", **item) for item in payload["cases"]]
    steps = [StepRecord(**item) for item in payload["steps"]]
    status_calls = {"count": 0}
    captured = {}

    def fake_run_memory_task(skill_id, request):
        assert skill_id == "openviking"
        if request.action == "ingest":
            return MemoryTaskOutput(status="ok", state="accepted", operation={"task_id": "task-1"})
        if request.action == "status":
            status_calls["count"] += 1
            state = "running" if status_calls["count"] == 1 else "completed"
            return MemoryTaskOutput(status="ok", state=state, operation=request.inputs["operation"])
        return MemoryTaskOutput(
            status="ok",
            state="completed",
            output={
                "count": 1,
                "memories": [{"content": "For systems programming I prefer Go over Python."}],
                "evidence_text": "For systems programming I prefer Go over Python.",
            },
        )

    def fake_run_agent_task(skill_id, rendered_input):
        captured["skill_id"] = skill_id
        captured["question"] = rendered_input.messages[-1]["content"]
        return {
            "status": "ok",
            "turns": [{"text": "For systems programming I prefer Go over Python."}],
            "metrics": [],
            "artifacts": [],
        }

    monkeypatch.setattr("memory_bench_platform.workflow.run_memory_task", fake_run_memory_task)
    monkeypatch.setattr("memory_bench_platform.workflow.run_agent_task", fake_run_agent_task)
    monkeypatch.setattr("memory_bench_platform.workflow._sleep", lambda seconds: None)

    output = execute_cases(
        run_id="run-1",
        agent_id="generic-cli",
        memory_id="openviking",
        runtime_context=_runtime_context(tmp_path),
        cases=cases,
        steps=steps,
        execution_spec=ExecutionSpec(**payload["execution_spec"]),
        run_dir=tmp_path,
    )

    assert status_calls["count"] == 2
    assert "For systems programming I prefer Go over Python." in captured["question"]
    assert output["judge_results"][0].passed is True
    assert output["step_results"][-1].step_id == "agent-answer"
