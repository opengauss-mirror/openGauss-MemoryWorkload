import copy
import json
import time
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from memory_bench_platform import workflow
from memory_bench_platform.cli import _execution_options, build_parser
from memory_bench_platform.protocol import CaseRecord, ExecutionSpec, StepRecord
from test_workflow_concurrency import plan, grade


def samples():
    cases, steps = [], []
    for sample in ("a", "b"):
        cs, ss = plan(checkpoints=2, questions=2)
        for c in cs:
            c.case_id = sample + "-" + c.case_id
            c.depends_on_cases = [sample + "-" + d for d in c.depends_on_cases]
            c.source_metadata["sample_id"] = sample
            c.reference["sample_id"] = sample
        for s in ss:
            s.case_id = sample + "-" + s.case_id
            s.step_id = sample + "-" + s.step_id
            s.depends_on = [sample + "-" + d for d in s.depends_on]
            if s.operator_kind == "memory":
                s.inputs["scope_id"] = sample
            if s.operator_kind == "agent":
                s.inputs["metadata"]["session_key"] = s.step_id
        cases.extend(cs)
        steps.extend(ss)
    return cases, steps


def run(tmp_path, cases, steps, qa=2, **kwargs):
    return workflow.execute_cases(run_id="run", agent_id="openclaw", memory_id="mem",
        cases=cases, steps=steps, execution_spec=ExecutionSpec(sample_concurrency=2,
        qa_concurrency=qa, judge_concurrency=2), run_dir=tmp_path, **kwargs)


@pytest.mark.parametrize("qa", [1, 2])
def test_parallel_samples_keep_checkpoints_and_global_limits(tmp_path, monkeypatch, qa):
    cases, steps = samples()
    writes = Barrier(2)
    lock = Lock()
    active = {"qa": 0, "judge": 0}
    peak = dict(active)
    graded = set()
    def operator(step, **kwargs):
        sample = step.case_id[0]
        if step.inputs.get("action") == "ingest":
            if step.case_id.endswith("setup-0"):
                writes.wait(timeout=5)  # Cannot pass if samples are serialized.
            else:
                assert all(sample + "-c0-q" + str(i) in graded for i in range(2))
            return {"status": "ok", "output": {}}
        if step.inputs.get("action") == "recall":
            assert step.inputs["scope_id"] == sample
            return {"status": "ok", "output": {"evidence_text": sample}}
        with lock:
            active["qa"] += 1
            peak["qa"] = max(peak["qa"], active["qa"])
        time.sleep(.03)
        with lock:
            active["qa"] -= 1
        return {"status": "ok", "turns": [{"text": step.case_id}]}
    def judge(run_id, judge_input, **kwargs):
        with lock:
            active["judge"] += 1
            peak["judge"] = max(peak["judge"], active["judge"])
        time.sleep(.03)
        with lock:
            active["judge"] -= 1
            graded.add(judge_input.case_id)
        return grade(run_id, judge_input)
    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    monkeypatch.setattr(workflow, "run_llm_judge", judge)
    result = run(tmp_path, cases, steps, qa=qa)
    assert peak["qa"] == qa and 1 <= peak["judge"] <= 2
    assert len(result["judge_results"]) == 8
    assert all(row.passed for row in result["judge_results"])
    assert [r.step_id for r in result["step_results"]] == [s.step_id for s in steps]
    assert len(list((tmp_path / "records/case-progress").glob("*.json"))) == len(cases)


@pytest.mark.parametrize("problem", ["scope", "dependency", "session", "sample", "conflicting_sample"])
def test_isolation_rejected_before_writes(tmp_path, monkeypatch, problem):
    cases, steps = samples()
    if problem == "scope":
        steps[-2].inputs["scope_id"] = "a"
    elif problem == "dependency":
        cases[6].depends_on_cases = ["a-setup-0"]
    elif problem == "session":
        agents = [s for s in steps if s.operator_kind == "agent"]
        agents[-1].inputs["metadata"]["session_key"] = agents[0].inputs["metadata"]["session_key"]
    elif problem == "conflicting_sample":
        cases[1].reference["sample_id"] = "b"
    else:
        cases[0].source_metadata.clear()
        cases[0].reference.clear()
    monkeypatch.setattr(workflow, "_execute_step_operator",
        lambda *a, **k: pytest.fail("must reject before any writes"))
    with pytest.raises(ValueError):
        run(tmp_path, cases, steps)


def test_parallel_phase_profiling_rejected(tmp_path):
    cases, steps = samples()
    with pytest.raises(ValueError, match="profiling"):
        run(tmp_path, cases, steps, phase_sink=object())


def test_cli_defaults_and_plugin_guard():
    assert ExecutionSpec().sample_concurrency == 1
    parser = build_parser()
    args = parser.parse_args(["run", "--benchmark", "locomo", "--agent", "openclaw",
        "--sample-concurrency", "2"])
    assert _execution_options(args).sample_concurrency == 2
    args.memory_integration = "agent_plugin"
    assert _execution_options(args).sample_concurrency == 2


def test_async_completion_precedes_next_write_and_tokens_are_not_duplicated(tmp_path, monkeypatch):
    from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding
    from memory_bench_platform.composer import compose_run_plan
    from memory_bench_platform import token_usage as usage
    payload = json.loads(open("tests/golden/multi_checkpoint_scenario.json").read())
    first = payload["samples"][0]
    # Two historical sessions before the checkpoint, two independent samples.
    first["timeline"] = [first["timeline"][0], first["timeline"][2], first["timeline"][3]]
    second = copy.deepcopy(first)
    second["sample_id"] = "sample-2"
    payload["samples"].append(second)
    composed = compose_run_plan(BenchmarkScenario.model_validate(payload),
        RunBinding(benchmark_id=payload["benchmark_id"], agent_id="openclaw",
            memory_id="mem", memory_integration="backend_direct", run_id="run"),
        {"memory": {"async_ingest": True, "commit": {"required_after_ingest": True},
            "readiness": {"supported": True}}})
    cases = [CaseRecord(run_id="run", **c) for c in composed["cases"]]
    steps = [StepRecord(**s) for s in composed["steps"]]
    for s in steps:
        if s.operator_kind == "poll":
            s.inputs["interval_seconds"] = .001
    barrier = Barrier(2)
    completed = set()
    events = []
    lock = Lock()
    def operator(step, **kwargs):
        if step.operator_kind == "poll":
            scope = step.inputs["probe"]["inputs"]["scope_id"]
            operation = step.inputs["probe"]["inputs"]["operation"]
            time.sleep(.02)
            with lock:
                completed.add((scope, operation["event"]))
                events.append((scope, operation["event"], "ready"))
            return {"status": "ok", "state": "completed"}
        scope = step.inputs.get("scope_id")
        action = step.inputs.get("action")
        if action == "ingest":
            event = step.inputs["event_id"]
            if event == "session-1":
                barrier.wait(timeout=5)
            else:
                assert (scope, "session-1") in completed
            with lock:
                events.append((scope, event, "ingest"))
            return {"status": "ok", "output": {"session_id": event,
                "operation": {"event": event}}}
        if action == "flush":
            return {"status": "ok", "output": {"operation": step.inputs["operation"]}}
        if action == "recall":
            assert (scope, "session-2") in completed
            return {"status": "ok", "output": {"evidence_text": scope}}
        context = usage.current_context()
        assert context and context["case_id"] == step.case_id
        request = SimpleNamespace(metadata=step.inputs["metadata"])
        return usage.capture_agent(lambda *a: {"status": "ok", "turns": [{"text": "coffee"}],
            "raw": {"usage": {"input_tokens": 10, "output_tokens": 2,
                             "input_tokens_details": {"cached_tokens": 3}}}},
            "test-agent", request)
    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    monkeypatch.setattr(workflow, "run_llm_judge", grade)
    result = run(tmp_path, cases, steps)
    assert len(result["judge_results"]) == 2
    assert all(row.passed for row in result["judge_results"])
    summary = usage.rebuild(tmp_path)
    assert summary["observed_agent_qa_tokens"] == 24
    rows = [json.loads(line) for line in (tmp_path / "records/token_usage.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and len({r["case_id"] for r in rows}) == 2
    assert sum(r["cache_read_tokens"] for r in rows) == 6
    assert usage.rebuild(tmp_path)["observed_agent_qa_tokens"] == 24


def test_failed_sample_does_not_block_other_sample(tmp_path, monkeypatch):
    cases, steps = samples()
    def operator(step, **kwargs):
        if step.case_id == "a-setup-0":
            return {"status": "failed", "error": {"message": "test failure"}}
        return {"status": "ok", "output": {"evidence_text": "b"}, "turns": [{"text": "b"}]}
    monkeypatch.setattr(workflow, "_execute_step_operator", operator)
    monkeypatch.setattr(workflow, "run_llm_judge", grade)
    result = run(tmp_path, cases, steps)
    b = [r for r in result["judge_results"] if r.case_id.startswith("b-")]
    assert len(b) == 4 and all(r.passed for r in b)
    a = [r for r in result["judge_results"] if r.case_id.startswith("a-")]
    assert len(a) == 4 and all(r.passed is None for r in a)


@pytest.mark.parametrize("mode", [1, 2])
def test_openviking_explicit_bindings_preflight_all_scopes(tmp_path, monkeypatch, mode):
    from memory_bench_platform.protocol import WorkflowRuntimeContext
    cases, steps = samples()
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"a": {"user_id": "u1", "api_key": "key1"}}))
    context = WorkflowRuntimeContext(run_id="run", run_dir=str(tmp_path), benchmark_id="locomo",
        agent_id="openclaw", memory_id="openviking", sample_environments={
            name: {"OPENVIKING_SCOPE_USERS_FILE": str(path)} for name in ("a", "b")})
    monkeypatch.setattr(workflow, "_execute_step_operator", lambda *a, **kw: pytest.fail("unexpected write"))
    with pytest.raises(ValueError, match="identity preflight failed for sample b"):
        workflow.execute_cases(run_id="run", agent_id="openclaw", memory_id="openviking",
            runtime_context=context, cases=cases, steps=steps, run_dir=tmp_path,
            execution_spec=ExecutionSpec(sample_concurrency=mode))


def test_openviking_bindings_compare_actual_users_across_files(tmp_path):
    from memory_bench_platform.integration import validate_sample_identities
    from memory_bench_platform.protocol import WorkflowRuntimeContext
    environments = {}
    scopes = {"left": {"scope-a"}, "right": {"scope-b"}}
    for sample, (scope,) in scopes.items():
        path = tmp_path / sample
        path.write_text(json.dumps({scope: {"user_id": "shared", "api_key": sample}}))
        environments[sample] = {"OPENVIKING_SCOPE_USERS_FILE": str(path)}
    context = WorkflowRuntimeContext(run_id="r", run_dir=str(tmp_path), benchmark_id="locomo",
        agent_id="openclaw", memory_id="openviking", sample_environments=environments)
    with pytest.raises(ValueError, match="left and right share a memory identity"):
        validate_sample_identities(context, "openviking", scopes)
    (tmp_path / "right").write_text(json.dumps({"scope-b": {"user_id": "other", "api_key": "right"}}))
    validate_sample_identities(context, "openviking", scopes)
