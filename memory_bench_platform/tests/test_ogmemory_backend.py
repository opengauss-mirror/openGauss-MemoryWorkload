import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ogmemory_runner", ROOT / "skills/memories/ogmemory/scripts/run_operation.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def invoke(action, inputs, responses, scope="run:sample", env=None):
    calls = []
    def open_(req, timeout):
        calls.append((req.full_url, json.loads(req.data), timeout))
        return io.BytesIO(json.dumps(responses.pop(0)).encode())
    result = runner.run_operation({"action": action, "inputs": {"scope_id": scope, **inputs}, "task_id": "step"}, environ=env or {}, urlopen=open_)
    return result, calls


def test_lifecycle_extracts_with_compact_and_preserves_date():
    ingested,calls = invoke("ingest", {"content": "Caroline: I moved to Paris.", "occurred_at": "1:56 pm on 8 May, 2023"}, [{"ok":True,"message_id":"m1"}])
    assert ingested["state"] == "accepted"
    assert calls[0][1]["created_at"] == "2023-05-08T13:56:00+00:00"
    flushed,calls = invoke("flush", {"operation": ingested["operation"]}, [{"ok":True,"compacted":True}])
    assert calls[0][0].endswith("/compact")
    assert flushed["operation"]["compact_completed"] is True
    assert flushed["operation"]["task_id"].startswith("compact-")
    ready,_ = invoke("status", {"operation":flushed["operation"]}, [{"idle":True,"reason":"idle","drain":{"failed":0}}, {"idle":True,"reason":"idle","outbox":{"total":0,"supported":True}}])
    assert ready["state"] == "completed"


@pytest.mark.parametrize("response", [{"ok":False,"error":"extraction failed"}, {"ok":True,"compacted":False,"reason":"empty_buffer"}])
def test_failed_or_empty_compact_is_not_success(response):
    result,_ = invoke("flush", {"session_id":"s"}, [response])
    assert result["state"] == "failed"


@pytest.mark.parametrize("responses", [
    [{"idle":True,"reason":"session_not_found"}],
    [{"idle":True,"drain":{"failed":1}}],
    [{"idle":True,"drain":{"failed":0}}, {"idle":True,"outbox":{"supported":False}}],
    [{"idle":True,"drain":{"failed":0}}, {"idle":True}],
    [{"idle":True,"drain":{"failed":0}}, {"idle":True,"outbox":{"supported":True}}],
])
def test_readiness_does_not_hide_missing_session_or_index_failures(responses):
    result,_ = invoke("status", {"operation":{"scope_id":"run:sample","session_id":"s","compact_completed":True}}, responses)
    assert result["state"] == "failed"


def test_pending_index_remains_accepted():
    result,_ = invoke("status", {"operation":{"scope_id":"run:sample","session_id":"s","compact_completed":True}}, [{"idle":True,"drain":{"failed":0}}, {"idle":False,"outbox":{"total":1,"supported":True}}])
    assert result["state"] == "accepted"


def test_ingest_requires_a_buffered_message_acknowledgement():
    result,_ = invoke("ingest", {"content":"history"}, [{"ok":True}])
    assert result["state"] == "failed"


def test_user_and_agent_isolation_and_read_only_recall():
    first,c1 = invoke("recall", {"query":"Where?"}, [{"messages":[{"role":"user", "content":"Paris", "_ogmem":True}]}])
    second,c2 = invoke("recall", {"query":"Where?"}, [{"messages":[]}], scope="other:sample")
    assert c1[0][1]["userId"] != c2[0][1]["userId"]
    assert c1[0][1]["accountId"] != c2[0][1]["accountId"]
    assert c1[0][1]["agentId"] != c2[0][1]["agentId"]
    assert c1[0][0].endswith("/compose")
    assert "Paris" in first["output"]["evidence_text"]
    assert second["output"]["evidence_text"] == ""


def test_cross_scope_operation_rejected_before_http():
    result,calls = invoke("flush", {"operation":{"scope_id":"other","session_id":"s"}}, [])
    assert result["state"] == "failed" and calls == []


def test_recall_uses_compose_budget_instead_of_query_limit():
    result,calls = invoke("recall", {"query":"Where?", "node_limit":3}, [{"messages":[]}], env={"OGMEM_COMPOSE_TOKEN_BUDGET":"8192"})
    assert result["status"] == "ok" and calls[0][1]["tokenBudget"] == 8192
    assert "top_k" not in calls[0][1]


def test_errors_redact_credentials_without_retry():
    count = []
    def broken(req, timeout):
        count.append(req)
        raise RuntimeError("failed secret-value")
    result = runner.run_operation({"action":"flush","inputs":{"scope_id":"s","session_id":"x"}}, environ={"OGMEM_API_KEY":"secret-value"}, urlopen=broken)
    assert len(count) == 1
    assert "secret-value" not in result["error"]["message"]


def test_flush_timeout_in_composed_plan():
    from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding
    from memory_bench_platform.composer import compose_run_plan
    from memory_bench_platform.integration import get_memory_manifest
    scenario = BenchmarkScenario.model_validate(json.loads((ROOT / "tests/golden/multi_checkpoint_scenario.json").read_text()))
    binding = RunBinding(benchmark_id=scenario.benchmark_id, agent_id="openclaw", memory_id="ogmemory", memory_integration="backend_direct", run_id="test")
    plan = compose_run_plan(scenario, binding, {"memory":get_memory_manifest("ogmemory").capabilities})
    flushes = [s for s in plan["steps"] if s["inputs"].get("action") == "flush"]
    assert flushes and all(s["timeout_seconds"] == 1800 for s in flushes)


def test_compose_retains_injected_context_without_echoing_question():
    response = {"messages": [
        {"role": "user", "content": "profile", "_ogmem": True},
        {"role": "user", "content": "Where?"},
        {"role": "user", "content": "summary", "_ogmem": True},
        {"role": "user", "content": "evidence", "_ogmem": True},
    ], "systemPromptAddition": ""}
    result, calls = invoke("recall", {"query": "Where?"}, [response])
    assert result["output"]["evidence_text"] == "profile\n\nsummary\n\nevidence"
    assert calls[0][0].endswith("/compose")
    assert calls[0][1]["messages"] == []
    assert calls[0][1]["prompt"] == "Where?"
    assert calls[0][1]["sessionId"].startswith("mbp-qa-")
    assert calls[0][1]["tokenBudget"] == 128000


def test_compose_missing_messages_is_failure_but_empty_evidence_is_valid():
    failed, _ = invoke("recall", {"query": "Where?"}, [{}])
    empty, _ = invoke("recall", {"query": "Where?"}, [{"messages": []}])
    assert failed["state"] == "failed"
    assert empty["state"] == "completed" and empty["output"]["evidence_text"] == ""


@pytest.mark.parametrize("receipt_state", ["SUCCEEDED", "UNKNOWN", "DEGRADED", None])
@pytest.mark.parametrize("http_504", [True, False])
def test_timed_out_compact_checks_scoped_receipt_without_resubmitting(receipt_state, http_504):
    calls = []

    def open_(req, timeout):
        path = urlsplit(req.full_url).path
        calls.append((req.get_method(), path, timeout))
        if path == "/api/v1/compact":
            if not http_504:
                raise TimeoutError("timed out")
            raise HTTPError(req.full_url, 504, "deadline", {}, io.BytesIO(b"deadline"))
        if path.endswith("/wait_until_idle"):
            body = {"idle": True, "reason": "idle", "outbox": {"total": 0}}
        elif path.endswith("/context"):
            assert "accountId=memory-bench-" in req.full_url
            body = {"session_id": "s", "archives": [{"archive_id": "a"}] if receipt_state else []}
        else:
            body = {"request_id": "compaction-a", "archive_id": "a", "request_status": receipt_state,
                    "source_status": "ARCHIVED", "semantic_status": "COMPLETE"}
        return io.BytesIO(json.dumps(body).encode())

    operation = {"scope_id": "scope", "session_id": "s", "type": "memory_ingest"}
    result = runner.run_operation(
        {"action": "flush", "inputs": {"scope_id": "scope", "operation": operation}},
        environ={}, urlopen=open_,
    )
    assert calls[0] == ("POST", "/api/v1/compact", 1800)
    assert [path for _, path, _ in calls].count("/api/v1/compact") == 1
    assert result["operation"]["compact_timeout"] is True
    if receipt_state == "SUCCEEDED":
        assert result["operation"]["compact_completed"] is True
        assert result["output"]["backend_result"]["confirmed_after_timeout"] is True
    else:
        assert result["status"] == "failed"
        assert result["error"]["code"] == "CompactTimeout"


@pytest.mark.parametrize("failure_code,expected_answer", [("CompactTimeout", True), ("RuntimeError", False)])
@pytest.mark.parametrize("empty_checkpoints", [False, True])
def test_workflow_ogmemory_timeout_keeps_diagnostic_qa(
    monkeypatch, tmp_path, failure_code, expected_answer, empty_checkpoints,
):
    from memory_bench_platform import workflow
    from memory_bench_platform.cli import _apply_native_run_validity, _summarize_native_evaluation
    from memory_bench_platform.protocol import CaseRecord, ExecutionSpec, MemoryTaskOutput, StepRecord, WorkflowRuntimeContext

    actions = []

    def run_memory(skill_id, request):
        actions.append(request.action)
        if request.action == "flush":
            return MemoryTaskOutput(status="failed", state="failed", error={"code": failure_code})
        return MemoryTaskOutput(status="ok", state="completed", output={"evidence_text": "Paris"})

    monkeypatch.setattr(workflow, "run_memory_task", run_memory)
    monkeypatch.setattr(workflow, "run_agent_task", lambda *_args, **_kwargs: {
        "status": "ok", "turns": [{"text": "Paris"}],
    })

    def setup(case_id, depends_on):
        return CaseRecord(case_id=case_id, run_id="r", title=case_id, goal="history",
                          labels=["phase:setup"], capability="memory/ingest",
                          judge_mode="none", depends_on_cases=depends_on)

    def qa(case_id, depends_on):
        return CaseRecord(case_id=case_id, run_id="r", title=case_id, goal="question",
                          capability="memory/question-answering", depends_on_cases=depends_on,
                          reference={"evaluation_target": "qa_answer", "expected_answer": "Paris"})

    last_checkpoint = 4 if empty_checkpoints else 2
    cases = [setup("setup1", []), qa("qa1", ["setup1"])]
    for index in range(2, last_checkpoint + 1):
        cases.extend([
            setup(f"setup{index}", [f"setup{index - 1}", f"qa{index - 1}"]),
            qa(f"qa{index}", [f"setup{index}"]),
        ])
    steps = [
        StepRecord(step_id="flush", case_id="setup1", name="flush", operator_kind="memory",
                   gate_policy="hard", inputs={"action": "flush"}),
        StepRecord(step_id="later-write", case_id=f"setup{last_checkpoint}", name="ingest", operator_kind="memory",
                   gate_policy="hard", inputs={"action": "ingest"}),
    ]
    for case_id in (f"qa{index}" for index in range(1, last_checkpoint + 1)):
        steps.extend([
            StepRecord(step_id=case_id + "-recall", case_id=case_id, name="recall",
                       operator_kind="memory", gate_policy="hard", inputs={"action": "recall"}),
            StepRecord(step_id=case_id + "-answer", case_id=case_id, name="answer",
                       operator_kind="agent", gate_policy="hard", depends_on=[case_id + "-recall"],
                       inputs={"question": "Where?"}),
        ])
    context = WorkflowRuntimeContext(run_id="r", run_dir=str(tmp_path), benchmark_id="locomo",
                                     agent_id="generic-cli", memory_id="ogmemory",
                                     memory_integration="backend_direct")
    result = workflow.execute_cases(run_id="r", agent_id="generic-cli", memory_id="ogmemory",
                                    runtime_context=context, cases=cases, steps=steps,
                                    execution_spec=ExecutionSpec(default_retry_limit=1), run_dir=tmp_path)
    assert actions == ["flush"] + (["recall"] * last_checkpoint if expected_answer else [])
    assert [row.passed for row in result["judge_results"]] == [True if expected_answer else None] * last_checkpoint
    summary = _apply_native_run_validity(_summarize_native_evaluation(
        cases, steps, result["judge_results"], result["step_results"],
    ), {"minimum_checkpoint_ready_rate": 0, "maximum_runtime_failure_rate": 1})
    assert summary["benchmark_score"] is None
    if expected_answer:
        assert summary["degraded_qa"] == {"graded": last_checkpoint, "correct": last_checkpoint, "accuracy": 1.0}
        assert summary["checkpoint_ready_rate"] == 0.0
        assert all(row["degraded_reason"] == "memory_compact_timeout" for row in summary["checkpoints"])
        assert "memory_compact_timeout" in summary["run_validity"]["reasons"]
