import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from memory_bench_platform import workflow
from memory_bench_platform.cli import _apply_native_run_validity, _summarize_native_evaluation
from memory_bench_platform.protocol import (
    CaseRecord, ExecutionSpec, MemoryTaskOutput, StepRecord, WorkflowRuntimeContext,
)


@pytest.mark.parametrize("backend", ["ogmemory", "hindsight", "mem0"])
@pytest.mark.parametrize("failure", [None, "ingest", "flush", "status", "recall", "exception",
                                     "empty", "both", "invalid", "invalid_setup", "compact_timeout", "source_only"])
def test_memory_failure_does_not_prevent_qa(monkeypatch, tmp_path, backend, failure):
    actions, prompts = [], []

    def memory(_skill, request):
        actions.append(request.action)
        if failure == "exception" and request.action == "recall":
            raise RuntimeError("recall transport failed")
        if failure == "compact_timeout" and request.action == "flush":
            return MemoryTaskOutput(status="failed", state="failed",
                                    error={"code": "CompactTimeout", "message": "backend still running"})
        if failure == "source_only" and request.action == "flush":
            return MemoryTaskOutput(status="failed", state="failed",
                                    output={"recovery_mode": "source_only"},
                                    error={"code": "ExtractionDegraded", "message": "source preserved"})
        if request.action == failure or (failure == "both" and request.action in {"flush", "recall"}):
            return MemoryTaskOutput(status="failed", state="failed",
                                    output={"evidence_text": "DO NOT USE FAILED OUTPUT"},
                                    error={"code": "BackendFailure", "message": "original detail"})
        evidence = "" if failure == "empty" else "Paris"
        return MemoryTaskOutput(status="ok", state="completed", output={"evidence_text": evidence})

    def agent(_skill, request, **kwargs):
        prompts.append(request)
        return {"status": "ok", "turns": [{"text": "Paris"}]}

    monkeypatch.setattr(workflow, "run_memory_task", memory)
    monkeypatch.setattr(workflow, "run_agent_task", agent)
    cases = [
        CaseRecord(case_id="setup", run_id="r", title="setup", goal="history",
                   labels=["phase:setup"], capability="memory/ingest", judge_mode="none"),
        CaseRecord(case_id="qa", run_id="r", title="qa", goal="question",
                   capability="memory/question-answering", depends_on_cases=["setup"],
                   reference={"evaluation_target": "qa_answer", "expected_answer": "Paris"}),
    ]
    steps = [
        StepRecord(step_id="ingest", case_id="setup", name="ingest", operator_kind="memory",
                   gate_policy="hard", inputs={"action": "ingest"}),
        StepRecord(step_id="flush", case_id="setup", name="flush", operator_kind="memory",
                   gate_policy="hard", depends_on=["ingest"], inputs={"action": "flush"}),
        StepRecord(step_id="wait-ready", case_id="setup", name="ready", operator_kind="poll",
                   gate_policy="hard", depends_on=["flush"], timeout_seconds=1,
                   inputs={"interval_seconds": 0,
                           "probe": {"operator_kind": "memory", "action": "status", "inputs": {}},
                           "success_when": {"path": "state", "equals": "completed"},
                           "failure_when": {"path": "state", "equals": "failed"}}),
        StepRecord(step_id="next-ingest", case_id="setup", name="next", operator_kind="memory",
                   gate_policy="hard", depends_on=["wait-ready"], inputs={"action": "ingest"}),
        StepRecord(step_id="recall", case_id="qa", name="recall", operator_kind="memory",
                   gate_policy="hard", inputs={"action": "recall"}),
        StepRecord(step_id="answer", case_id="qa", name="answer", operator_kind="agent",
                   gate_policy="hard", depends_on=["recall"],
                   inputs={"messages": [{"role": "user", "content": {
                       "$template": "Evidence: {{steps.recall.output.evidence_text}}"}}]}),
    ]
    context = WorkflowRuntimeContext(run_id="r", run_dir=str(tmp_path), benchmark_id="locomo",
                                     agent_id="generic-cli", memory_id=backend,
                                     memory_integration="backend_direct")
    if failure == "invalid":
        steps[-2].inputs["query"] = {"$ref": "case.reference.missing"}
    if failure == "invalid_setup":
        steps[0].inputs["content"] = {"$ref": "case.reference.missing"}
    class Sink:
        session = SimpleNamespace(config=SimpleNamespace(phases=["recall"], mode="best_effort"))

        def run_phase(self, phase, targets, operation, **kwargs):
            return operation()

    result = workflow.execute_cases(run_id="r", agent_id="generic-cli", memory_id=backend,
                                    runtime_context=context, cases=cases, steps=steps,
                                    execution_spec=ExecutionSpec(default_retry_limit=0), run_dir=tmp_path,
                                    phase_sink=Sink())
    by_id = {row.step_id: row for row in result["step_results"]}
    if failure == "invalid_setup":
        assert not actions and not prompts
        assert by_id["ingest"].status == "failed"
        assert by_id["recall"].status == "skipped"
        assert result["judge_results"][0].passed is None
        return
    if failure == "invalid":
        assert "recall" not in actions and not prompts
        assert by_id["recall"].status == "failed"
        assert result["judge_results"][0].passed is None
        return
    event, = map(json.loads, (tmp_path / "profiling_client_events.jsonl").read_text().splitlines())
    recall = by_id["recall"]
    assert event["operation"] == "search"
    assert event["status"] == ("failed" if failure in {"recall", "exception", "both"} else "ok")
    assert datetime.fromisoformat(event["started_utc"]) == recall.started_at.astimezone(timezone.utc)
    assert datetime.fromisoformat(event["ended_utc"]) == recall.ended_at.astimezone(timezone.utc)
    assert actions.count("ingest") == 2
    assert "recall" in actions
    assert by_id["answer"].status == "passed"
    assert result["judge_results"][0].passed is True
    assert "DO NOT USE FAILED OUTPUT" not in str(prompts)
    if failure == "ingest":
        assert by_id["flush"].status == "skipped"
    if failure in {"recall", "exception", "both"}:
        assert by_id["recall"].status == "failed"
        assert "Paris" not in str(prompts)
        assert by_id["answer"].structured_output["qa_degradation"]["recall_failed"] == "recall"
    if failure == "recall":
        assert by_id["recall"].structured_output["raw"]["error"]["message"] == "original detail"
    if failure == "exception":
        assert (tmp_path / by_id["recall"].stderr_ref).read_text() == "recall transport failed"
    if failure is None:
        assert "Paris" in str(prompts)
    summary = _apply_native_run_validity(_summarize_native_evaluation(
        cases, steps, result["judge_results"], result["step_results"]))
    degraded = failure not in {None, "empty"}
    assert summary["qa_paths"]["degraded"]["graded"] == int(degraded)
    assert summary["qa_paths"]["normal"]["graded"] == int(not degraded)
    assert summary["qa_paths"]["recall_empty"]["cases"] == int(failure == "empty")
    assert summary["run_validity"]["valid"] is not degraded
    assert summary["raw_benchmark_score"] == 1.0
