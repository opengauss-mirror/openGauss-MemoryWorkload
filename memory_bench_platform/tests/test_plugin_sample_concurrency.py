import copy
import json
from threading import Barrier, Lock
from pathlib import Path

import pytest

from memory_bench_platform import workflow, token_usage
from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding
from memory_bench_platform.composer import compose_run_plan
from memory_bench_platform.integration import _adapter_environment, adapter_environment
from memory_bench_platform.protocol import CaseRecord, StepRecord, ExecutionSpec, WorkflowRuntimeContext, RenderedTaskInput
from test_workflow_concurrency import grade


def plugin_plan(*, extra_question=False, questions_per_checkpoint=1):
    payload = json.loads(Path("tests/golden/multi_checkpoint_scenario.json").read_text())
    original = payload["samples"][0]
    if extra_question:
        questions = original["timeline"][1]["evaluation"]["questions"]
        questions.append(dict(questions[0], question_id="q1-extra"))
    for index in range(1, questions_per_checkpoint):
        questions = original["timeline"][1]["evaluation"]["questions"]
        questions.append(dict(questions[0], question_id=f"q1-{index}"))
    payload["samples"] = [copy.deepcopy(original), copy.deepcopy(original)]
    for index, sample in enumerate(payload["samples"]):
        sample["sample_id"] = f"sample-{index}"
    binding = RunBinding(benchmark_id=payload["benchmark_id"], run_id="r", agent_id="openclaw",
                         memory_id="ogmemory", memory_plugin_id="openclaw-ogmemory",
                         memory_integration="agent_plugin")
    plan = compose_run_plan(BenchmarkScenario.model_validate(payload), binding,
                            {"memory_plugin": {"commit": {"required_after_ingest": True},
                                               "readiness": {"supported": True}}})
    cases = [CaseRecord(run_id="r", **c) for c in plan["cases"]]
    steps = [StepRecord(**s) for s in plan["steps"]]
    environments = {
        f"sample-{i}": {"OPENCLAW_GATEWAY_URL": f"http://127.0.0.1:{19000+i}",
                        "OPENCLAW_STATE_DIR": f"/tmp/sample-{i}",
                        "OPENCLAW_BIN": f"/tmp/sample-{i}/openclaw"}
        for i in range(2)
    }
    return cases, steps, environments


@pytest.mark.parametrize("concurrency", [1, 2])
def test_plugin_samples_order_and_environment(tmp_path, monkeypatch, concurrency):
    cases, steps, environments = plugin_plan()
    barrier = Barrier(2)
    seen, ready, ingested = set(), set(), set()
    owners = {c.case_id: c.source_metadata.get("sample_id") or c.reference.get("sample_id") for c in cases}
    def operator(step, **kwargs):
        sample = owners[step.case_id]
        assert _adapter_environment.get()["OPENCLAW_STATE_DIR"] == environments[sample]["OPENCLAW_STATE_DIR"]
        action = step.inputs.get("action")
        if action == "validate" and sample not in seen:
            seen.add(sample)
        if step.operator_kind == "agent":
            purpose = step.inputs["metadata"]["purpose"]
            if purpose == "memory_ingest":
                if sample in ingested:
                    assert sample in ready
                elif concurrency == 2:
                    barrier.wait(timeout=5)
                ingested.add(sample)
                ready.discard(sample)
            else:
                assert sample in ready
            return {"status": "ok", "output": {"session_handle": "session"},
                    "turns": [{"text": "Paris"}]}
        if action == "wait_ready":
            ready.add(sample)
        return {"status": "ok", "output": {"operation": {"id": step.step_id}, "ready": True}}
    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    monkeypatch.setattr(workflow, "run_llm_judge", grade)
    context = WorkflowRuntimeContext(run_id="r", run_dir=str(tmp_path), benchmark_id="locomo",
        agent_id="openclaw", memory_id="ogmemory", memory_plugin_id="openclaw-ogmemory",
        memory_integration="agent_plugin", sample_environments=environments)
    result = workflow.execute_cases(run_id="r", agent_id="openclaw", memory_id="ogmemory",
        runtime_context=context, cases=cases, steps=steps, run_dir=tmp_path,
        execution_spec=ExecutionSpec(sample_concurrency=concurrency))
    assert seen == set(environments)
    assert all(r.status == "passed" for r in result["step_results"])
    assert result["judge_results"]
    assert not _adapter_environment.get()


@pytest.mark.parametrize("qa_limit", [1, 2])
def test_plugin_qa_overlap_only_across_gateways(tmp_path, monkeypatch, qa_limit):
    from time import monotonic_ns

    cases, steps, environments = plugin_plan(extra_question=True)
    barrier = Barrier(2)
    lock = Lock()
    intervals = []
    def operator(step, **kwargs):
        if step.operator_kind != "agent":
            return {"status": "ok", "output": {"operation": {"id": step.step_id}, "ready": True}}
        if step.inputs["metadata"]["purpose"] != "memory_ingest":
            gateway = _adapter_environment.get()["OPENCLAW_GATEWAY_URL"]
            start = monotonic_ns()
            if qa_limit == 2:
                barrier.wait(timeout=5)
            with lock:
                intervals.append((gateway, start, monotonic_ns()))
        return {"status": "ok", "output": {"session_handle": "session"}, "turns": [{"text": "Paris"}]}

    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    monkeypatch.setattr(workflow, "run_llm_judge", grade)
    context = WorkflowRuntimeContext(run_id="r", run_dir=str(tmp_path), benchmark_id="locomo",
        agent_id="openclaw", memory_id="ogmemory", memory_plugin_id="openclaw-ogmemory",
        memory_integration="agent_plugin", sample_environments=environments)
    result = workflow.execute_cases(run_id="r", agent_id="openclaw", memory_id="ogmemory",
        runtime_context=context, cases=cases, steps=steps, run_dir=tmp_path,
        execution_spec=ExecutionSpec(sample_concurrency=2, qa_concurrency=qa_limit, judge_concurrency=4))
    assert len(intervals) == len(result["judge_results"]) == 6
    overlapping = [(a, b) for i, (a, start, end) in enumerate(intervals)
                   for b, other_start, other_end in intervals[:i]
                   if start < other_end and other_start < end]
    assert bool(overlapping) == (qa_limit == 2)
    assert all(a != b for a, b in overlapping)


def test_plugin_shared_environment_rejected_before_writing(tmp_path, monkeypatch):
    cases, steps, environments = plugin_plan()
    environments["sample-1"] = environments["sample-0"]
    monkeypatch.setattr(workflow, "_execute_case", lambda **kw: pytest.fail("unexpected write"))
    context = WorkflowRuntimeContext(run_id="r", run_dir=str(tmp_path), benchmark_id="locomo",
        agent_id="openclaw", memory_id="ogmemory", memory_plugin_id="openclaw-ogmemory",
        memory_integration="agent_plugin", sample_environments=environments)
    with pytest.raises(ValueError, match="distinct"):
        workflow.execute_cases(run_id="r", agent_id="openclaw", memory_id="ogmemory",
            runtime_context=context, cases=cases, steps=steps, run_dir=tmp_path,
            execution_spec=ExecutionSpec(sample_concurrency=2, qa_concurrency=2))


def test_token_session_lookup_uses_bound_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "wrong"))
    folder = tmp_path / "right" / "agents/main/sessions"
    folder.mkdir(parents=True)
    (folder / "sessions.json").write_text(json.dumps({"agent:main:test": {"sessionId": "uuid"}}))
    request = RenderedTaskInput(task_id="t", metadata={"session_key": "agent:main:test"})
    with adapter_environment({"OPENCLAW_STATE_DIR": str(tmp_path / "right")}):
        _, candidates = token_usage._session_candidates(request, {})
        assert folder / "uuid.jsonl" in candidates


def test_parallel_bound_transcripts_count_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    barrier = Barrier(2)
    def sample(index):
        state = tmp_path / f'state-{index}'
        folder = state / 'agents/main/sessions'
        folder.mkdir(parents=True)
        request = SimpleNamespace(metadata={'session_key': 'qa', 'purpose': 'memory_qa', 'sample_id': str(index)})
        def runner(*args):
            barrier.wait(timeout=5)
            (folder / 'sessions.json').write_text(json.dumps({'agent:main:qa': {'sessionId': 'same-id'}}))
            (folder / 'same-id.jsonl').write_text(json.dumps({'id': 'same-message', 'message': {
                'role': 'assistant', 'usage': {'input': 10, 'output': 3, 'cacheRead': 2, 'cacheWrite': 0}}})+'\n')
            return {'status': 'ok', 'raw': {'usage': {'input_tokens': 12, 'output_tokens': 3, 'total_tokens': 15}}}
        with adapter_environment({'OPENCLAW_STATE_DIR': str(state), 'OPENCLAW_TRANSPORT': 'http'}):
            with token_usage.scope({'run_dir': str(tmp_path), 'run_id': 'r', 'memory_backend': 'ogmemory'}):
                token_usage.capture_agent(runner, 'openclaw', request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(sample, [0, 1]))
    summary = token_usage.rebuild(tmp_path)
    rows = [json.loads(line) for line in (tmp_path / 'records/token_usage.jsonl').read_text().splitlines()]
    assert len(rows) == 2
    assert len({row['record_id'] for row in rows}) == 2
    assert {row['source'] for row in rows} == {'openclaw_session'}
    assert summary['observed_agent_qa_tokens'] == 30


@pytest.mark.parametrize('failed', [False, True])
def test_finalize_uses_every_sample_runtime(tmp_path, monkeypatch, failed):
    from memory_bench_platform import cli
    from memory_bench_platform.protocol import MemoryPluginTaskOutput
    _, _, environments = plugin_plan()
    calls = []
    def finalize(plugin, request):
        env = dict(_adapter_environment.get())
        calls.append(env)
        assert not request.runtime_context.sample_environments
        if failed and len(calls) == 1:
            raise RuntimeError('sample cleanup failed')
        return MemoryPluginTaskOutput(status='ok', state='completed')
    monkeypatch.setattr(cli, 'run_memory_plugin_task', finalize)
    context = WorkflowRuntimeContext(run_id='r', run_dir=str(tmp_path), benchmark_id='locomo',
        agent_id='openclaw', memory_id='ogmemory', memory_plugin_id='openclaw-ogmemory',
        memory_integration='agent_plugin', sample_environments=environments)
    payload, _, _, result, _, _ = cli._run_plugin_finalize(context)
    assert len(calls) == 2
    assert len({e['MEMORY_BENCH_PLUGIN_RUN_DIR'] for e in calls}) == 2
    assert {e['OPENCLAW_STATE_DIR'] for e in calls} == {e['OPENCLAW_STATE_DIR'] for e in environments.values()}
    assert result.status == ('failed' if failed else 'passed')
    assert len(payload['output']['samples']) == 2

def test_four_questions_share_one_plugin_gateway(tmp_path, monkeypatch):
    cases, steps, _ = plugin_plan(questions_per_checkpoint=4)
    cases = [c for c in cases if c.case_id.startswith("sample-0")]
    steps = [s for s in steps if s.case_id in {c.case_id for c in cases}]
    assert len(cases) == 7
    barrier = Barrier(4)
    sessions = set()
    lock = Lock()

    def operator(step, runtime_context, **kwargs):
        if step.operator_kind != "agent":
            return {"status": "ok", "output": {"operation": {"id": step.step_id}, "ready": True}}
        if step.inputs["metadata"]["purpose"] == "memory_qa":
            assert runtime_context.concurrent_plugin_qa
            with lock:
                sessions.add(step.inputs["metadata"]["session_key"])
                first_checkpoint = len(sessions) <= 4
            if first_checkpoint:
                barrier.wait(timeout=5)
        return {"status": "ok", "output": {"session_handle": "session"}, "turns": [{"text": "coffee" if step.case_id.endswith("-q2") else "tea"}]}

    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    monkeypatch.setattr(workflow, "run_llm_judge", grade)
    ctx = WorkflowRuntimeContext(run_id="r", run_dir=str(tmp_path), benchmark_id="locomo",
        agent_id="openclaw", memory_id="ogmemory", memory_plugin_id="openclaw-ogmemory",
        memory_integration="agent_plugin")
    result = workflow.execute_cases(run_id="r", agent_id="openclaw", memory_id="ogmemory",
        runtime_context=ctx, cases=cases, steps=steps, run_dir=tmp_path,
        execution_spec=ExecutionSpec(qa_concurrency=4, qa_per_sample=4))
    assert len(sessions) == len(result["judge_results"]) == 5
    assert all(r.passed for r in result["judge_results"])

def test_parallel_qa_shared_state_counts_each_transcript_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    folder = tmp_path / "state/agents/main/sessions"
    folder.mkdir(parents=True)
    (folder / "sessions.json").write_text(json.dumps({
        f"agent:main:q{i}": {"sessionId": f"sid-{i}"} for i in range(4)}))
    barrier = Barrier(4)

    def answer(i):
        request = SimpleNamespace(metadata={"session_key": f"q{i}", "purpose": "memory_qa",
                                            "sample_id": "same"})
        def runner(*args):
            barrier.wait(timeout=5)
            (folder / f"sid-{i}.jsonl").write_text(json.dumps({"id": "msg", "message": {
                "role": "assistant", "usage": {"input": 10+i, "output": 3, "cacheRead": 2,
                                               "cacheWrite": 0}}})+"\n")
            return {"status": "ok", "output": {"session_id": f"sid-{i}"},
                    "raw": {"usage": {"input_tokens": 12+i, "output_tokens": 3}}}
        with adapter_environment({"OPENCLAW_STATE_DIR": str(tmp_path / "state"), "OPENCLAW_TRANSPORT": "http"}):
            with token_usage.scope({"run_dir": str(tmp_path), "run_id": "r", "memory_backend": "ogmemory"}):
                token_usage.capture_agent(runner, "openclaw", request)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(answer, range(4)))
    assert token_usage.rebuild(tmp_path)["observed_agent_qa_tokens"] == 66
    rows = [json.loads(line) for line in (tmp_path / "records/token_usage.jsonl").read_text().splitlines()]
    assert len(rows) == len({r["record_id"] for r in rows}) == 4
    assert all(r["source"] == "openclaw_session" for r in rows)


@pytest.mark.parametrize("failure,restore,graded", [
    ("SettledHistoryFailure", True, True),
    ("SettledHistoryFailure", False, False),
    ("TimeoutError", True, False),
    ("RuntimeError", True, False),
])
def test_plugin_history_failure_and_read_only_gate(tmp_path, monkeypatch, failure, restore, graded):
    from memory_bench_platform.cli import _summarize_native_evaluation
    cases, steps, _ = plugin_plan(questions_per_checkpoint=4)
    cases = [c for c in cases if c.case_id.startswith("sample-0")]
    steps = [s for s in steps if s.case_id in {c.case_id for c in cases}]
    qa, switches, commits = [], [], []

    def operator(step, **kwargs):
        action = step.inputs.get("action")
        if action == "commit":
            commits.append(step.step_id)
            if len(commits) == 1:
                return {"status": "failed", "error": {"code": failure}}
        if step.inputs.get("recover_read_only"):
            switches.append(step.step_id)
            assert step.inputs["phase"] == "qa" and step.inputs["scope_id"]
            return {"status": "ok" if restore else "failed",
                    "output": {"read_only_verified": restore}}
        if step.operator_kind == "agent":
            if step.inputs["metadata"]["purpose"] == "memory_qa":
                assert switches and restore
                qa.append(step.step_id)
            return {"status": "ok", "output": {"session_handle": "session"}, "turns": [{"text": "tea"}]}
        return {"status": "ok", "output": {"operation": {"id": step.step_id}, "ready": True}}

    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    ctx = WorkflowRuntimeContext(run_id="r", run_dir=str(tmp_path), benchmark_id="locomo",
        agent_id="openclaw", memory_id="ogmemory", memory_plugin_id="openclaw-ogmemory",
        memory_integration="agent_plugin")
    result = workflow.execute_cases(run_id="r", agent_id="openclaw", memory_id="ogmemory",
        runtime_context=ctx, cases=cases, steps=steps, run_dir=tmp_path, execution_spec=ExecutionSpec())
    summary = _summarize_native_evaluation(cases, steps, result["judge_results"], result["step_results"])
    assert len(qa) == (5 if graded else 0)
    assert summary["case_ungraded"] == (0 if graded else 5)
    assert summary["checkpoint_ready_rate"] == 0.0
    assert len(commits) == (2 if graded else 1)
    if graded:
        assert summary["degraded_qa"]["graded"] == 5
        assert all(s.structured_output["qa_degradation"]["setup_failed"]
                   for s in result["step_results"] if s.step_id in qa)
    else:
        assert bool(switches) == (failure == "SettledHistoryFailure")


def test_jiuwen_finalize_uses_sample_plans_and_weights_missing_answers(tmp_path):
    from memory_bench_platform import cli
    import hashlib

    cases, steps, environments = plugin_plan()
    # Use different denominators so averaging two percentages cannot pass.
    qa_cases = [c for c in cases if c.reference.get("evaluation_target") == "qa_answer"]
    first, second = environments
    keep = [next(c for c in qa_cases if c.source_metadata["sample_id"] == first),
            *[c for c in qa_cases if c.source_metadata["sample_id"] == second][:2]]
    ingest = [s for s in steps if s.inputs.get("metadata", {}).get("purpose") == "memory_ingest"]
    writes = [next(s for s in ingest if s.case_id.startswith(sample)) for sample in environments]
    keep.extend(c for c in cases if c.case_id in {s.case_id for s in writes})
    plans = cli._sample_finalize_plans(keep, writes)
    assert plans == {first: {"planned_writes": 1, "planned_qa": 1},
                     second: {"planned_writes": 1, "planned_qa": 2}}
    for sample in environments:
        root = tmp_path / "artifacts/sample-runtimes" / hashlib.sha256(sample.encode()).hexdigest()
        receipts = root / "artifacts/memory_plugin/openclaw-jiuwenmemory"
        receipts.mkdir(parents=True)
        (receipts / "write.json").write_text(json.dumps({"ingest_verified": sample == first}))
        if sample == first:
            (receipts / "qa.json").write_text(json.dumps({"verified_search": True, "calls": 1, "evidence": True}))

    ctx = WorkflowRuntimeContext(run_id="r", run_dir=str(tmp_path), benchmark_id="locomo",
        agent_id="openclaw", memory_id="jiuwenmemory", memory_plugin_id="openclaw-jiuwenmemory",
        memory_integration="agent_plugin", sample_environments=environments)
    payload, _, _, result, _, _ = cli._run_plugin_finalize(ctx, sample_plans=plans)
    assert result.status == "passed"
    out = payload["output"]
    assert out["samples"][second]["output"]["recall_verification"]["coverage"] == 0
    assert out["ingest_verification"] == {"planned": 2, "verified": 1, "coverage": 0.5}
    assert out["recall_verification"] == {"planned_qa": 3, "checked_qa": 1, "verified": 1,
        "unverified": 2, "coverage": 1/3, "tool_called": 1, "tool_call_coverage": 1/3, "with_evidence": 1}
