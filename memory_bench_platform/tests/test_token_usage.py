import importlib.util
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_bench_platform import token_usage as usage
from memory_bench_platform.judges import run_llm_judge
from memory_bench_platform.protocol import JudgeInput


def ctx(tmp_path, **extra):
    return dict(run_dir=str(tmp_path), run_id="test-run", memory_backend="openviking", **extra)


def records(tmp_path):
    usage.rebuild(tmp_path)
    return [json.loads(s) for s in (tmp_path / "records/token_usage.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("raw,profile,expected", [
    ({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
      "prompt_tokens_details": {"cached_tokens": 8},
      "completion_tokens_details": {"reasoning_tokens": 3}}, "openai-compatible-v1", 14),
    ({"input_tokens": 10, "output_tokens": 4}, "openai-compatible-v1", 14),
    ({"input": 10, "output": 4, "cacheRead": 8, "cacheWrite": 2}, "openclaw-v1", 24),
    ({"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 8,
      "cache_creation_input_tokens": 2}, "anthropic-v1", 24),
    ({"input_tokens": 10, "output_tokens": 4, "cache_read": 8}, "ogmemory-counter-v1", 14),
    ({"input_tokens": 1359, "output_tokens": 13, "total_tokens": 4956}, "openclaw-responses-v1", 4956),
    ({"input_tokens": 10, "output_tokens": 4}, "openclaw-responses-v1", None),
    ({"prompt_tokens": 0, "completion_tokens": 0}, "openai-compatible-v1", 0),
    ({}, "openai-compatible-v1", None),
    ({"total_tokens": 30}, "openai-compatible-v1", None),
    ({"input": 10, "output": 4}, "openclaw-v1", None),
])
def test_normalization(raw, profile, expected):
    assert usage.normalize_usage(raw, profile)["total_tokens"] == expected


def test_embedding_and_total_disagreement():
    assert usage.normalize_usage({"total_tokens": 9}, model_kind="embedding")["total_tokens"] == 9
    result = usage.normalize_usage({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 30})
    assert result["reported_total_tokens"] == 30
    assert result["total_tokens"] == 14
    assert result["issues"] == ["reported_total_mismatch"]


def test_shared_session_increment_and_response_overlap(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "state"))
    transcript = tmp_path / "session.jsonl"
    old = {"id": "old", "message": {"role": "assistant", "usage": {
        "input": 999, "output": 999, "cacheRead": 0, "cacheWrite": 0}}}
    transcript.write_text(json.dumps(old) + "\n")
    request = SimpleNamespace(metadata={"session_jsonl": str(transcript), "purpose": "memory_qa"})
    def runner(agent, req):
        with transcript.open("a") as f:
            f.write(json.dumps({"id": str(transcript.stat().st_size), "message": {
                "role": "assistant", "model": "model-a", "usage": {
                    "input": 10, "output": 5, "cacheRead": 2, "cacheWrite": 0}}}) + "\n")
        return {"status": "ok", "raw": {"result": {"meta": {"agentMeta": {
            "model": "model-a", "usage": {"input": 10, "output": 5, "cacheRead": 2, "cacheWrite": 0}}}}}}
    with usage.scope(ctx(tmp_path)):
        usage.capture_agent(runner, "openclaw", request)
        usage.capture_agent(runner, "openclaw", request)
    rows = records(tmp_path)
    assert len(rows) == 2
    assert sum(r["total_tokens"] for r in rows) == 34
    assert {r["source"] for r in rows} == {"openclaw_session"}
    assert usage.rebuild(tmp_path)["observed_agent_qa_tokens"] == 34


def test_agent_http_fallback_failures_and_disable(tmp_path, monkeypatch):
    request = SimpleNamespace(metadata={"question_id": "q1"})
    count = []
    def runner(agent, req):
        count.append(1)
        return {"status": "failed", "raw": {"model": "http-model", "usage": {
            "input_tokens": 8, "output_tokens": 3}}}
    with usage.scope(ctx(tmp_path)):
        result = usage.capture_agent(runner, "http-agent", request)
    rows = records(tmp_path)
    assert len(rows) == 1 and rows[0]["total_tokens"] == 11
    assert rows[0]["outcome"] == "failed" and rows[0]["phase"] == "qa"
    monkeypatch.setenv("MEMORY_BENCH_TOKEN_USAGE", "0")
    with usage.scope(ctx(tmp_path)):
        assert usage.capture_agent(runner, "http-agent", request) == result
    assert len(count) == 2 and len(records(tmp_path)) == 1


def test_agent_exception_is_unknown_and_propagates(tmp_path):
    def runner(*args):
        raise TimeoutError("failed")
    with usage.scope(ctx(tmp_path)), pytest.raises(TimeoutError):
        usage.capture_agent(runner, "http-agent", SimpleNamespace(metadata={}))
    row, = records(tmp_path)
    assert row["total_tokens"] is None and row["outcome"] == "failed"


@pytest.mark.parametrize("metadata,key", [
    ({"session_id": "requested"}, "agent:main:explicit:requested"),
    ({"session_id": "ignored", "session_key": "custom"}, "agent:main:custom"),
])
def test_failed_openclaw_http_reads_gateway_uuid_transcript(tmp_path, monkeypatch, metadata, key):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCLAW_TRANSPORT", "http")
    folder = tmp_path / "state/agents/main/sessions"
    folder.mkdir(parents=True)
    gateway_uuid = "11111111-2222-3333-4444-555555555555"
    request = SimpleNamespace(metadata={"agent_id": "main", "purpose": "memory_qa", **metadata})

    def runner(*args):
        (folder / "sessions.json").write_text(json.dumps({key: {"sessionId": gateway_uuid}}))
        (folder / f"{gateway_uuid}.jsonl").write_text(json.dumps({
            "id": "failed-attempt", "message": {"role": "assistant", "model": "model-a",
            "usage": {"input": 12, "output": 3, "cacheRead": 2, "cacheWrite": 0}},
        }) + "\n")
        raise RuntimeError("response lost after transcript")

    with usage.scope(ctx(tmp_path)), pytest.raises(RuntimeError, match="response lost"):
        usage.capture_agent(runner, "openclaw", request)
    rows = records(tmp_path)
    transcript, = [row for row in rows if row["source"] == "openclaw_session"]
    assert transcript["outcome"] == "failed"
    assert transcript["session_id"] == gateway_uuid
    assert transcript["total_tokens"] == 17
    assert transcript["cache_read_tokens"] == 2
    assert usage.rebuild(tmp_path)["observed_agent_qa_tokens"] == 17


class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(self.payload).encode()


def test_judge_success_parse_failure_and_transport_error_are_not_counted(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_TEST_KEY", "test")
    monkeypatch.setenv("TOKEN_TEST_URL", "https://example.invalid/v1")
    monkeypatch.setenv("TOKEN_TEST_MODEL", "judge")
    inp = JudgeInput(case_id="c", reference={"question": "Q", "expected_answer": "A", "expected_step_id": "s"},
                     step_results=[{"step_id": "s", "structured_output": {"agent_answer": "A"}}])
    config = {"profile": "locomo_qa@1", "env": {
        "api_key": "TOKEN_TEST_KEY", "base_url": "TOKEN_TEST_URL", "model": "TOKEN_TEST_MODEL"}}
    calls = []
    def opener(request, timeout):
        calls.append(json.loads(request.data))
        return Response({"usage": {"prompt_tokens": 20, "completion_tokens": 5},
                         "choices": [{"message": {"content": "not a grade"}}]})
    def fail(*args, **kwargs): raise TimeoutError()
    def success(*args, **kwargs):
        return Response({"usage": {"prompt_tokens": 20, "completion_tokens": 5},
                         "choices": [{"message": {"content": '{"is_correct":"CORRECT"}'}}]})
    with usage.scope(ctx(tmp_path, case_id="c")):
        assert run_llm_judge("test-run", inp, runtime_config=config, urlopen=success).passed is True
        assert run_llm_judge("test-run", inp, runtime_config=config, urlopen=opener).label == "judge-error"
        run_llm_judge("test-run", inp, runtime_config=config, urlopen=opener)
        run_llm_judge("test-run", inp, runtime_config=config, urlopen=fail)
    rows = records(tmp_path)
    assert rows == []
    assert not list((tmp_path / "records/token-usage/shards").glob("*.jsonl"))
    assert len(calls) == 2 and calls[0] == calls[1]


def test_task_snapshots_deduplicated_and_failed_usage_retained(tmp_path):
    with usage.scope(ctx(tmp_path)):
        for status, n in [("running", 4), ("completed", 8), ("failed", 8)]:
            usage.collect_ov_task({"output": {"task_id": "t1", "backend_status": status, "task_result": {
                "token_usage": {"llm": {"prompt_tokens": n, "completion_tokens": 2},
                                "embedding": {"total_tokens": 3}}}}})
    rows = records(tmp_path)
    assert len(rows) == 2 and sum(r["total_tokens"] for r in rows) == 13
    assert all(r["outcome"] == "failed" for r in rows)
    summary = usage.rebuild(tmp_path)
    assert summary["observed_memory_llm_tokens"] == 10
    assert summary["observed_memory_embedding_tokens"] == 3
    assert summary["complete_system_llm_tokens"] is None


@pytest.mark.parametrize("generation,after,expected", [("one", 15, 5), ("two", 15, None), ("one", 5, None)])
def test_counter_reset_and_restart(tmp_path, generation, after, expected):
    before = {"exclusive": True, "generation": "one", "timestamp": "t", "stats": {
        "llm": {"input_tokens": 10, "output_tokens": 0}, "embedding": {"total_tokens": 2}}}
    end = {"exclusive": True, "generation": generation, "stats": {
        "llm": {"input_tokens": after, "output_tokens": 0}, "embedding": {"total_tokens": 2}}}
    usage.record_counter_window(ctx(tmp_path), before, end)
    llm = next(r for r in records(tmp_path) if r["model_kind"] == "llm")
    assert llm["total_tokens"] == expected


def test_counter_not_polled_without_exclusivity(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_BENCH_OGMEMORY_EXCLUSIVE", raising=False)
    monkeypatch.setattr(usage.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("unexpected request"))
    assert usage.start_counters(dict(ctx(tmp_path), memory_backend="ogmemory")) is None


def test_parallel_shards_rebuild_and_corrupt_tail(tmp_path):
    def write(i):
        with usage.scope(ctx(tmp_path, case_id=str(i))):
            usage.record({"input_tokens": 2, "output_tokens": 3}, component="agent", phase="qa")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(80)))
    shard = next((tmp_path / "records/token-usage/shards").glob("*.jsonl"))
    with shard.open("a") as f: f.write('{"unfinished"')
    summary = usage.rebuild(tmp_path)
    assert summary["observed_agent_qa_tokens"] == 400
    assert summary["selected_observations"] == 80
    assert summary["diagnostic_count"] == 1
    previous = (tmp_path / "records/token_usage.jsonl").read_bytes()
    assert usage.rebuild(tmp_path) == summary
    assert (tmp_path / "records/token_usage.jsonl").read_bytes() == previous


def test_collection_io_failure_does_not_change_answer(tmp_path, monkeypatch):
    def fail(*a, **kw): raise OSError("no disk")
    monkeypatch.setattr(usage, "_append", fail)
    result = {"status": "ok", "agent_answer": "answer"}
    with usage.scope(ctx(tmp_path)):
        assert usage.capture_agent(lambda *args: result, "other", SimpleNamespace(metadata={})) == result


def test_started_attempt_survives_interruption(tmp_path):
    with usage.scope(ctx(tmp_path)):
        usage.record(record_id="pending", component="agent", outcome="started", revision=0)
    row, = records(tmp_path)
    assert row["outcome"] == "started" and row["total_tokens"] is None


def test_wrapped_cli_response_without_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "absent-state"))
    result = {"status": "ok", "raw": {"result": {"meta": {"agentMeta": {
        "model": "cli-model", "usage": {"input": 10, "output": 5, "cacheRead": 3, "cacheWrite": 2}}}}}}
    with usage.scope(ctx(tmp_path)):
        usage.capture_agent(lambda *a: result, "openclaw", SimpleNamespace(metadata={}))
    row, = records(tmp_path)
    assert row["total_tokens"] == 20 and row["model"] == "cli-model"


def test_valid_json_with_invalid_record_is_diagnosed(tmp_path):
    with usage.scope(ctx(tmp_path)):
        usage.record({"input_tokens": 3, "output_tokens": 2})
    shard = next((tmp_path / "records/token-usage/shards").glob("*.jsonl"))
    with shard.open("a") as f:
        f.write(json.dumps({"schema_version": "token-usage/1", "record_type": "usage"}) + "\n")
    result = usage.rebuild(tmp_path)
    assert result["selected_observations"] == 1 and result["diagnostic_count"] == 1


def test_workflow_retry_keeps_all_costs_and_accounting_does_not_change_results(tmp_path, monkeypatch):
    from memory_bench_platform.protocol import CaseRecord, StepRecord, ExecutionSpec
    from memory_bench_platform.workflow import execute_cases
    case = CaseRecord(case_id="c", run_id="r", title="retry", goal="answer", capability="qa",
                      reference={"expected_answer": "A"})
    step = StepRecord(step_id="s", case_id="c", name="answer", operator_kind="agent", retry_limit=1,
                      inputs={"question": "Q", "metadata": {"question_id": "q1"}})
    outcomes = []
    for enabled in ("1", "0"):
        monkeypatch.setenv("MEMORY_BENCH_TOKEN_USAGE", enabled)
        seen = []
        def agent(*args):
            seen.append(1)
            return {"status": "failed" if len(seen) == 1 else "ok", "turns": [{"text": "A"}],
                    "raw": {"usage": {"input_tokens": 10, "output_tokens": 2}}}
        monkeypatch.setattr("memory_bench_platform.workflow.run_agent_task", agent)
        output = execute_cases(run_id="r", agent_id="generic-cli", cases=[case], steps=[step],
                               execution_spec=ExecutionSpec(fail_fast=True), run_dir=tmp_path / enabled)
        assert len(seen) == 2
        outcomes.append((output["step_results"][0].status, output["judge_results"][0].passed))
    assert outcomes == [("passed", True), ("passed", True)]
    rows = records(tmp_path / "1")
    assert len(rows) == 2 and {r["attempt"] for r in rows} == {1, 2}
    assert sum(r["total_tokens"] for r in rows) == 24
    assert not (tmp_path / "0/records/token-usage").exists()


def test_after_agent_failure_keeps_usage(tmp_path):
    from memory_bench_platform.protocol import MemoryPluginTaskOutput, StepRecord, WorkflowRuntimeContext
    from memory_bench_platform.workflow_operators import _execute_agent
    runtime = WorkflowRuntimeContext(run_id="r", run_dir=str(tmp_path), benchmark_id="locomo", agent_id="other",
        memory_integration="agent_plugin", memory_plugin_id="p",
        run_contract={"memory_plugin_runtime": {"actions": ["after_agent"]}})
    step = StepRecord(step_id="s", case_id="c", name="answer", operator_kind="agent", inputs={"question": "Q"})
    with usage.scope(usage.context(runtime)):
        result = _execute_agent(step, "other", lambda *a: {
            "status": "ok", "turns": [{"text": "A"}], "raw": {"usage": {"input_tokens": 3, "output_tokens": 1}}},
            runtime, lambda *a: MemoryPluginTaskOutput(status="failed", state="failed", error={"message": "missing receipt"}))
    assert result["status"] == "failed" and result["agent_answer"] == "A"
    assert records(tmp_path)[0]["total_tokens"] == 4


def test_ov_failed_task_exposes_usage_without_forwarding_error(tmp_path, monkeypatch):
    path = Path("skills/memory_plugins/openclaw-openviking/scripts/run_lifecycle.py")
    spec = importlib.util.spec_from_file_location("ov_usage_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_base_url", lambda: "http://example.invalid")
    monkeypatch.setattr(module, "_task_headers", lambda *args: {"X-API-Key": "test"})
    monkeypatch.setattr(module.time, "sleep", lambda *args: None)
    monkeypatch.setattr(module, "_task_status", lambda *a, **kw: {"status": "failed", "result": {
        "error": "private message", "token_usage": {"llm": {"prompt_tokens": 6, "completion_tokens": 2}}}})
    response = module.run({"action": "wait_ready", "inputs": {"task_id": "t", "grace_seconds": 0}})
    assert response["status"] == "failed" and "private message" not in json.dumps(response)
    with usage.scope(ctx(tmp_path)):
        usage.collect_ov_task(response)
    assert usage.rebuild(tmp_path)["observed_memory_llm_tokens"] == 8


def test_report_reference_and_offline_cli(tmp_path, capsys):
    from memory_bench_platform.cli import main
    from memory_bench_platform.reporter import write_summary
    with usage.scope(ctx(tmp_path)):
        usage.record({"input_tokens": 7, "output_tokens": 2}, component="agent", phase="qa")
    write_summary(tmp_path, {"status": "failed"})
    summary = json.loads((tmp_path / "reports/summary.json").read_text())
    assert summary["status"] == "failed"
    assert summary["resource_summary"]["token_usage"]["observed_agent_qa_tokens"] == 9
    main(["token-summary", "--run-dir", str(tmp_path)])
    assert json.loads(capsys.readouterr().out)["observed_agent_qa_tokens"] == 9


def test_openclaw_http_fallback_includes_cache_without_double_counting(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "no-transcript"))
    result = {"status": "ok", "raw": {"model": "kimi-k2", "usage": {
        "input_tokens": 1359, "output_tokens": 13, "total_tokens": 4956}}}
    with usage.scope(ctx(tmp_path)):
        usage.capture_agent(lambda *a: result, "openclaw", SimpleNamespace(metadata={"purpose": "memory_qa"}))
    row, = records(tmp_path)
    assert row["input_tokens"] == 4943 and row["total_tokens"] == 4956
    assert row["cache_read_tokens"] is None and row["cache_write_tokens"] is None
    assert row["normalization_profile"] == "openclaw-responses-v1"


def test_transcript_aliases_and_duplicate_shards_are_counted_once(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "state"))
    transcript = tmp_path / "physical.jsonl"
    transcript.touch()
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(transcript)
    def runner(*args):
        transcript.write_text(json.dumps({"id": "one-message", "message": {
            "role": "assistant", "usage": {"input": 10, "output": 2, "cacheRead": 3, "cacheWrite": 0}}}) + "\n")
        return {"status": "ok", "artifacts": [{"kind": "openclaw_session_jsonl", "path": str(alias)}]}
    with usage.scope(ctx(tmp_path)):
        usage.capture_agent(runner, "openclaw", SimpleNamespace(metadata={"session_jsonl": str(transcript)}))
    shard = next((tmp_path / "records/token-usage/shards").glob("*.jsonl"))
    (shard.parent / "duplicate-copy.jsonl").write_bytes(shard.read_bytes())
    row, = records(tmp_path)
    assert row["total_tokens"] == 15


def test_ov_total_includes_cache_and_does_not_add_combined_total(tmp_path):
    native = {"llm": {"prompt_tokens": 9446, "completion_tokens": 1681, "total_tokens": 11127,
                      "cached_tokens": 5120, "reasoning_tokens": 0},
              "embedding": {"total_tokens": 928}, "total": {"total_tokens": 12055, "cached_tokens": 5120}}
    with usage.scope(ctx(tmp_path)):
        usage.collect_ov_task({"output": {"task_id": "real-shape", "task_result": {"token_usage": native}}})
    rows = records(tmp_path)
    assert len(rows) == 2
    llm = next(r for r in rows if r["model_kind"] == "llm")
    assert llm["cache_read_tokens"] == 5120 and llm["total_tokens"] == 11127
    result = usage.rebuild(tmp_path)
    assert result["observed_memory_llm_tokens"] == 11127
    assert result["observed_memory_embedding_tokens"] == 928


@pytest.mark.parametrize("integration", ["backend_direct", "agent_plugin"])
def test_system_categories_cache_and_mode_totals(tmp_path, integration):
    run_context = ctx(tmp_path, memory_integration=integration)
    usage.initialize(run_context)
    with usage.scope(run_context):
        usage.record({"input": 10, "output": 3, "cacheRead": 6, "cacheWrite": 2},
                     profile="openclaw-v1", component="agent", phase="qa")
        if integration == "agent_plugin":
            usage.record({"input": 5, "output": 1, "cacheRead": 4, "cacheWrite": 0},
                         profile="openclaw-v1", component="agent", phase="ingest")
        usage.collect_ov_task({"output": {"task_id": "task", "task_result": {"token_usage": {
            "llm": {"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 80},
            "embedding": {"total_tokens": 20}}}}})
        # Even a caller explicitly recording evaluator usage cannot re-enable it.
        usage.record({"input_tokens": 999, "output_tokens": 1}, component="evaluator", phase="judge")
    report = usage.rebuild(tmp_path)
    costs = report["cost_categories"]
    assert costs["agent_qa"]["input_tokens"] == 18
    assert costs["agent_qa"]["cache_read_tokens"] == 6
    assert costs["memory_llm"]["input_tokens"] == 100
    assert costs["memory_llm"]["cache_read_tokens"] == 80
    assert costs["memory_embedding"]["total_tokens"] == 20
    assert costs["agent_ingest"]["status"] == ("observed" if integration == "agent_plugin" else "not_applicable")
    assert report["observed_system_llm_tokens"] == (141 if integration == "agent_plugin" else 131)
    assert "observed_judge_llm_tokens" not in report and "observed_experiment_llm_tokens" not in report
    assert "CacheRead" in (tmp_path / "reports/token_summary.md").read_text()


def test_cache_field_missing_is_not_complete_or_zero(tmp_path):
    with usage.scope(ctx(tmp_path)):
        for raw in [{"input_tokens": 10, "output_tokens": 2, "cached_tokens": 0},
                    {"input_tokens": 20, "output_tokens": 3}]:
            usage.record(raw, component="agent", phase="qa")
    group = usage.rebuild(tmp_path)["cost_categories"]["agent_qa"]
    assert group["input_tokens"] == 30 and group["total_tokens"] == 35
    assert group["cache_read_tokens"] is None
    assert group["observed_cache_read_tokens"] == 0
    assert group["field_coverage"]["cache_read_tokens"] == {"known": 1, "missing": 1, "fraction": .5}


def test_unobserved_mem0_and_old_judge_shards(tmp_path):
    run_context = dict(ctx(tmp_path), memory_backend="mem0", memory_integration="backend_direct")
    usage.initialize(run_context)
    with usage.scope(run_context):
        usage.record({"input_tokens": 10, "output_tokens": 2}, component="agent", phase="qa")
    shard = next((tmp_path / "records/token-usage/shards").glob("*.jsonl"))
    old_judge = json.loads(shard.read_text())
    old_judge.update(component="evaluator", phase="judge", record_id="old-judge")
    with shard.open("a") as f:
        f.write(json.dumps(old_judge) + "\n")
    original = shard.read_bytes()
    report = usage.rebuild(tmp_path)
    assert report["excluded_legacy_judge_observations"] == 1
    assert report["observed_system_llm_tokens"] == 12
    assert report["complete_system_llm_tokens"] is None
    assert report["cost_categories"]["memory_llm"]["input_tokens"] is None
    assert report["cost_categories"]["memory_llm"]["status"] == "unobserved"
    assert shard.read_bytes() == original
    assert len(records(tmp_path)) == 1


def test_conflicting_openai_aliases_use_pair_matching_reported_total():
    raw = {
        "input_tokens": 100, "output_tokens": 0,
        "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125,
        "prompt_tokens_details": {"cached_tokens": 80},
    }
    result = usage.normalize_usage(raw)
    assert (result["input_tokens"], result["output_tokens"], result["total_tokens"]) == (100, 25, 125)
    assert result["cache_read_tokens"] == 80
    assert result["issues"] == ["conflicting_usage_aliases"]
    assert result["raw_usage"] == raw


def test_conflicting_openai_aliases_can_select_modern_pair():
    result = usage.normalize_usage({
        "input_tokens": 90, "output_tokens": 10,
        "prompt_tokens": 90, "completion_tokens": 0, "total_tokens": 100,
    })
    assert result["total_tokens"] == 100
    assert result["issues"] == ["conflicting_usage_aliases"]


def test_unresolved_openai_alias_conflict_preserves_mismatch_diagnostic():
    result = usage.normalize_usage({
        "input_tokens": 90, "output_tokens": 0,
        "prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 200,
    })
    assert result["total_tokens"] == 90
    assert result["issues"] == ["conflicting_usage_aliases", "reported_total_mismatch"]


@pytest.mark.parametrize("failure", ["lookup", "read"])
def test_agent_response_usage_survives_transcript_failure(tmp_path, monkeypatch, failure):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    request = SimpleNamespace(metadata={"session_jsonl": str(transcript), "purpose": "memory_qa"})
    result = {"status": "ok", "raw": {"usage": {
        "input": 100, "output": 20, "cacheRead": 0, "cacheWrite": 0}}}
    if failure == "lookup":
        original = usage._session_candidates
        def candidates(req, response):
            if response:
                raise PermissionError("unreadable session index")
            return original(req, response)
        monkeypatch.setattr(usage, "_session_candidates", candidates)
    else:
        original = Path.open
        def open_file(path, *args, **kwargs):
            if path == transcript and args and args[0] == "rb":
                raise PermissionError("unreadable transcript")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", open_file)
    with usage.scope(ctx(tmp_path)):
        assert usage.capture_agent(lambda *args: result, "openclaw", request) == result
    rows = records(tmp_path)
    assert len(rows) == 1
    assert rows[0]["source"] == "agent_response"
    assert rows[0]["outcome"] == "succeeded"
    assert rows[0]["total_tokens"] == 120


def test_unreadable_transcript_does_not_block_other_transcripts(tmp_path, monkeypatch):
    unreadable, readable = tmp_path / "bad.jsonl", tmp_path / "good.jsonl"
    unreadable.write_text("")
    readable.write_text(json.dumps({"id": "good", "message": {
        "role": "assistant", "usage": {"input": 10, "output": 5,
        "cacheRead": 0, "cacheWrite": 0}}}) + "\n")
    monkeypatch.setattr(usage, "_session_candidates", lambda *args: (tmp_path, [unreadable, readable]))
    original = Path.open
    def open_file(path, *args, **kwargs):
        if path == unreadable and args and args[0] == "rb":
            raise PermissionError("unreadable transcript")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_file)
    with usage.scope(ctx(tmp_path)):
        usage._collect_agent(SimpleNamespace(metadata={"purpose": "memory_qa"}),
                             {"raw": {"usage": {"input": 10, "output": 5,
                              "cacheRead": 0, "cacheWrite": 0}}}, {}, False, "attempt", "openclaw")
    rows = records(tmp_path)
    assert len(rows) == 1
    assert rows[0]["source"] == "openclaw_session"
    assert rows[0]["total_tokens"] == 15


@pytest.mark.parametrize("api_key", [None, "counter-test-secret"])
def test_counter_snapshot_passes_optional_api_key(tmp_path, monkeypatch, caplog, api_key):
    monkeypatch.setenv("MEMORY_BENCH_OGMEMORY_EXCLUSIVE", "1")
    monkeypatch.setenv("MEMORY_BENCH_OGMEMORY_CONTAINER", "dedicated")
    monkeypatch.setenv("OGMEM_API_URL", "http://counter.test/")
    if api_key is None:
        monkeypatch.delenv("OGMEM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OGMEM_API_KEY", api_key)
    monkeypatch.setattr(usage.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(stdout="container generation true"))
    calls = []
    def opener(request, timeout):
        assert request.full_url == "http://counter.test/api/v1/token_stats"
        assert request.get_header("X-api-key") == api_key
        assert timeout == 3
        calls.append(request)
        return io.BytesIO(b'{"llm":{"input_tokens":100}}')
    monkeypatch.setattr(usage.urllib.request, "urlopen", opener)
    for _ in range(2):  # Both start and end snapshots use the same request path.
        snapshot = usage.start_counters(dict(ctx(tmp_path), memory_backend="ogmemory"))
        assert snapshot["stats"]["llm"]["input_tokens"] == 100
        assert "counter-test-secret" not in json.dumps(snapshot)
    assert len(calls) == 2
    def failed_opener(*args, **kwargs):
        raise OSError("counter-test-secret")
    monkeypatch.setattr(usage.urllib.request, "urlopen", failed_opener)
    assert usage.start_counters(dict(ctx(tmp_path), memory_backend="ogmemory")) is None
    assert "counter-test-secret" not in caplog.text
    for path in (tmp_path / "records/token-usage/shards").glob("*.jsonl"):
        assert "counter-test-secret" not in path.read_text()


@pytest.mark.parametrize("bad_availability", [[], {}])
def test_rebuild_skips_invalid_availability_and_keeps_valid_usage(tmp_path, bad_availability):
    with usage.scope(ctx(tmp_path)):
        usage.record({"prompt_tokens": 10, "completion_tokens": 2},
                     component="agent", phase="qa", record_id="first")
        usage.record({"prompt_tokens": 20, "completion_tokens": 3},
                     component="agent", phase="qa", record_id="second")
    shard = next((tmp_path / "records/token-usage/shards").glob("*.jsonl"))
    first, second = shard.read_text().splitlines()
    bad = json.loads(first)
    bad.update(record_id="bad", availability=bad_availability)
    shard.write_text(first + "\n" + json.dumps(bad) + "\n" + second + "\n")
    summary = usage.rebuild(tmp_path)
    assert summary["observed_agent_qa_tokens"] == 35
    assert summary["selected_observations"] == 2
    assert summary["diagnostic_count"] == 1
    assert usage.rebuild(tmp_path) == summary
