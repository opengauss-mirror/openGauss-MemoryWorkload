import importlib.util
import json
from pathlib import Path

import pytest
from memory_bench_platform.integration import resolve_run_skill_bundle
from memory_bench_platform.compatibility import resolve_compatibility
from memory_bench_platform.composer import compose_run_plan
from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/memory_plugins/openclaw-ogmemory/scripts"


def load():
    spec = importlib.util.spec_from_file_location("ogmem_lifecycle", SCRIPTS / "run_lifecycle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upstream(tmp_path):
    path = tmp_path / "upstream"
    path.mkdir()
    (path / "index.js").write_text("export default {};")
    (path / "openclaw.plugin.json").write_text(json.dumps({"id": "og-memory-context-engine",
        "configSchema": {"properties": {k: {"type": "boolean"} for k in ("autoCapture", "autoRecall")}}}))
    return path


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"models": {"test": "secret"}, "agents": {
        "defaults": {"model": {"primary": "test/model"}, "workspace": "/shared"}}}))
    module = load()
    config = module.prepare_runtime(source, upstream(tmp_path), tmp_path / "isolated", "http://backend")
    state = config.with_name("phase.json")
    monkeypatch.setenv("OGMEM_PLUGIN_STATE_FILE", str(state))
    monkeypatch.setenv("MEMORY_BENCH_AGENT_LOCAL", "1")
    monkeypatch.setenv("OPENCLAW_TRANSPORT", "cli")
    monkeypatch.delenv("OG_AUTH_ACCOUNT_ID", raising=False)
    def run(action, **inputs):
        return module.run({"action": action, "inputs": inputs,
                           "runtime_context": {"run_dir": str(tmp_path / "run")}})
    return state, module, run


def entry(state):
    return json.loads(state.with_name("openclaw.json").read_text())["plugins"]["entries"]["og-memory-context-engine"]["config"]


def test_native_configuration_phases_isolation_and_finalize(runtime):
    state, module, run = runtime
    assert run("validate")["output"]["context_engine"] == "og-memory-context-engine"
    assert entry(state)["autoCapture"] is False
    run("prepare", scope_id="run:a")
    identity_a = json.loads(state.read_text())["identity"]
    assert entry(state)["authAccountId"] == identity_a["accountId"]
    assert entry(state)["autoCapture"] is True and entry(state)["autoRecall"] is False
    run("set_phase", phase="qa")
    assert entry(state)["autoCapture"] is False and entry(state)["autoRecall"] is True
    assert entry(state)["userId"] == identity_a["userId"]
    with pytest.raises(ValueError, match="another active run"):
        module.run({"action": "prepare", "inputs": {"scope_id": "run:b"},
                    "runtime_context": {"run_dir": "another"}})
    run("prepare", scope_id="run:b")
    assert json.loads(state.read_text())["identity"] != identity_a
    finalized = run("finalize")
    assert json.loads(state.read_text())["active"] is False
    assert entry(state)["autoCapture"] is False and entry(state)["autoRecall"] is False
    assert Path(finalized["artifacts"][0]["path"]).is_file()


def test_preparer_preserves_plugin_and_private_config(runtime):
    state, _, _ = runtime
    root = state.parent
    assert root.joinpath("plugin/index.js").read_text() == "export default {};"
    assert not root.joinpath("plugin/index.mjs").exists()
    assert state.with_name("openclaw.json").stat().st_mode & 0o777 == 0o600
    assert json.loads(root.joinpath("provenance.json").read_text())["phase_control"] == "native_autoCapture_autoRecall"
    config = json.loads(state.with_name("openclaw.json").read_text())
    assert config["tools"]["deny"] == ["*"]
    assert config["agents"]["defaults"]["workspace"] != "/shared"
    assert json.loads(root.parent.joinpath("source.json").read_text())["agents"]["defaults"]["workspace"] == "/shared"


def test_reject_old_plugin_and_existing_destination(tmp_path):
    module = load(); src = tmp_path / "source.json"; src.write_text('{}')
    plugin = upstream(tmp_path)
    dest = tmp_path / "runtime"
    module.prepare_runtime(src, plugin, dest, "http://backend")
    with pytest.raises(FileExistsError):
        module.prepare_runtime(src, plugin, dest, "http://backend")
    (plugin / "openclaw.plugin.json").write_text('{"id":"og-memory-context-engine","configSchema":{"properties":{}}}')
    with pytest.raises(ValueError, match="autoCapture"):
        module.prepare_runtime(src, plugin, tmp_path / "new", "http://backend")


def test_wait_uses_native_session_and_checks_index(runtime, monkeypatch):
    state, module, run = runtime; run("prepare", scope_id="run:a")
    identity = json.loads(state.read_text())["identity"]; calls = []
    def backend(base, path, body, ident, **kw):
        calls.append((path, body, ident, kw))
        if kw.get("method") == "GET":
            return {"message_count": 2, "commit_count": 1, "pending_tokens": 0}
        return {"idle": True, "reason": "idle", "drain": {"failed": 0}, "outbox": {"total": 0}}
    monkeypatch.setattr(module, "backend", backend)
    result = run("wait_ready", scope_id="run:a", session_handle={"session_id": "native-id"})
    assert result["output"]["history_archived"] is True
    assert len(calls) == 4 and calls[0][1]["sessionId"] == "native-id"
    assert all(c[2] == identity for c in calls)
    with pytest.raises(ValueError, match="scope"):
        run("wait_ready", scope_id="wrong", session_handle={"session_id": "native-id"})


@pytest.mark.parametrize("kind", ["missing", "empty", "index_failure", "unsupported"])
def test_no_false_readiness(runtime, monkeypatch, kind):
    _, module, run = runtime; run("prepare", scope_id="run:a")
    def backend(base, path, body, identity, **kw):
        if kind == "missing": return {"idle": True, "reason": "session_not_found"}
        if kw.get("method") == "GET":
            return {"message_count": 0, "commit_count": 0} if kind == "empty" else {"commit_count": 1}
        return {"idle": True, "drain": {"failed": 1 if kind == "index_failure" else 0},
                "outbox": {"total": 0, "supported": kind != "unsupported"}}
    monkeypatch.setattr(module, "backend", backend)
    with pytest.raises(RuntimeError):
        run("wait_ready", scope_id="run:a", session_handle={"session_id": "native"})


def test_short_history_is_buffered_not_reported_as_extracted(runtime, monkeypatch):
    _, module, run = runtime; run("prepare", scope_id="run:a"); calls = []
    def backend(base, path, body, identity, **kw):
        calls.append(path)
        return {"message_count": 2, "commit_count": 0, "pending_tokens": 40} if kw.get("method") == "GET" else {"idle": True}
    monkeypatch.setattr(module, "backend", backend)
    result = run("wait_ready", scope_id="run:a", session_handle={"session_id": "short"})
    assert result["output"]["history_archived"] is False
    assert result["output"]["reason"] == "buffered_without_archive"
    assert len(calls) == 2


def test_timeout_and_gateway_rejection(runtime, monkeypatch):
    _, module, run = runtime; run("prepare", scope_id="run:a")
    with pytest.raises(TimeoutError):
        run("wait_ready", scope_id="run:a", session_handle={"session_id": "native"}, timeout_seconds=0)
    monkeypatch.setenv("OPENCLAW_TRANSPORT", "http")
    with pytest.raises(ValueError, match="OGMEM_GATEWAY_CONTAINER"):
        run("validate")


def test_gateway_restart_failure_leaves_phase_unready(runtime, monkeypatch):
    import subprocess
    state, module, run = runtime
    run("prepare", scope_id="run:a")
    monkeypatch.setenv("OPENCLAW_TRANSPORT", "http")
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(state.parent / "state"))
    monkeypatch.setattr(module, "gateway_container", lambda path: "dedicated")
    def fail(*a, **kw):
        raise subprocess.CalledProcessError(1, a[0])
    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        run("set_phase", phase="qa")
    assert json.loads(state.read_text())["gateway_ready"] is False


def test_gateway_control_rejects_unowned_container(runtime, monkeypatch):
    from types import SimpleNamespace
    state, module, _ = runtime
    monkeypatch.setenv("OGMEM_GATEWAY_CONTAINER", "shared")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout='[{"Config":{"Labels":{}},"Mounts":[]}]'))
    with pytest.raises(ValueError, match="label"):
        module.gateway_container(state)


def test_plugin_resolves_and_plan_waits_on_native_capture():
    bundle = resolve_run_skill_bundle("longmemeval", "openclaw", "ogmemory", "agent_plugin")
    scenario = BenchmarkScenario.model_validate({"benchmark_id": "longmemeval", "samples": [{
        "sample_id": "a", "timeline": [
            {"event_id": "s1", "type": "conversation", "payload": {"content": "History"}},
            {"event_id": "qa", "type": "checkpoint", "evaluation": {"target": "qa_answer",
                "questions": [{"question_id": "q1", "question": "What?", "reference": "fact"}]}}]}]})
    binding = RunBinding(benchmark_id="longmemeval", agent_id="openclaw", memory_id="ogmemory",
        memory_integration="agent_plugin", memory_plugin_id="openclaw-ogmemory", run_id="test")
    compatibility = resolve_compatibility(scenario, binding, agent=bundle.agent,
        memory=bundle.memory, memory_plugin=bundle.memory_plugin)
    assert compatibility.compatible
    plan = compose_run_plan(scenario, binding, compatibility.resolved_capabilities)
    waits = [s for s in plan["steps"] if s["inputs"].get("action") == "wait_ready"]
    assert len(waits) == 1 and "session_handle" in waits[0]["inputs"]
    assert not any(s["inputs"].get("action") == "commit" for s in plan["steps"])


@pytest.mark.parametrize("failure", ["scope", "local", "phase", "hook", "missing_plugin", None])
def test_plugin_checks_native_phase_and_result(runtime, failure):
    _, _, run = runtime
    run("prepare", scope_id="run:a")
    metadata = {"purpose": "memory_ingest", "scope_id": "run:a", "local": True}
    if failure == "scope": metadata["scope_id"] = "wrong"
    if failure == "local": metadata["local"] = False
    if failure == "phase": run("set_phase", phase="qa")
    stderr = "[plugins] og-memory: mode=remote"
    if failure == "hook": stderr += "\n[og-memory] HTTP 500 from after_turn"
    if failure == "missing_plugin": stderr = ""
    def invoke():
        context = run("before_agent", agent_request={"metadata": metadata})["output"]
        return run("after_agent", check_context=context,
                   agent_result={"transport": "cli", "stderr": stderr})
    if failure:
        with pytest.raises((ValueError, RuntimeError)): invoke()
    else:
        assert invoke()["output"]["checked"] is True


@pytest.mark.parametrize("failure", [None, "stale_config", "identity", "hook", "missing_session"])
def test_http_agent_checks_retain_native_guards(runtime, monkeypatch, failure):
    import hashlib
    from types import SimpleNamespace
    state, module, run = runtime
    run("prepare", scope_id="run:a")
    monkeypatch.setenv("OPENCLAW_TRANSPORT", "http")
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(state.parent / "state"))
    monkeypatch.setattr(module, "gateway_container", lambda path: "dedicated")
    if failure == "identity":
        cfg = json.loads(state.with_name("openclaw.json").read_text())
        cfg["plugins"]["entries"][module.PLUGIN]["config"]["userId"] = "wrong"
        module.save(state.with_name("openclaw.json"), cfg)
    data = json.loads(state.read_text())
    data.update(gateway_ready=True, config_sha256=hashlib.sha256(state.with_name("openclaw.json").read_bytes()).hexdigest())
    if failure == "stale_config": data["config_sha256"] = "stale"
    module.save(state, data)
    def logs(cmd, **kw):
        assert cmd[:3] == ["docker", "logs", "--since"]
        assert float(cmd[3]) > 0
        return SimpleNamespace(stdout="[og-memory] HTTP 500 from compose" if failure == "hook" else "", stderr="")
    monkeypatch.setattr(module.subprocess, "run", logs)
    def invoke():
        request = {"system_prompt": "Reply INGEST_OK", "metadata": {"purpose": "memory_ingest", "scope_id": "run:a"}}
        check = run("before_agent", agent_request=request)["output"]
        return run("after_agent", check_context=check, agent_result={"transport": "http",
                   "output": {"session_id": "" if failure == "missing_session" else "gateway-uuid"}})
    if failure:
        with pytest.raises((ValueError, RuntimeError)): invoke()
    else:
        assert invoke()["output"]["checked"] is True


def test_real_plugin_protocol_brackets_generic_agent(runtime):
    from memory_bench_platform.integration import run_memory_plugin_task, build_run_contract
    from memory_bench_platform.protocol import StepRecord, WorkflowRuntimeContext
    from memory_bench_platform.workflow_operators import _execute_agent
    state, _, run = runtime
    run("prepare", scope_id="run:a")
    # Use the actual JSON subprocess protocol, not an in-process lifecycle mock.
    ctx = WorkflowRuntimeContext(run_id="run", run_dir=str(state.parent.parent / "run"),
        benchmark_id="locomo", agent_id="openclaw", memory_integration="agent_plugin",
        memory_plugin_id="openclaw-ogmemory",
        run_contract=build_run_contract("locomo", "openclaw", "ogmemory", "agent_plugin"))
    step = StepRecord(step_id="ingest", case_id="case", name="ingest", operator_kind="agent",
        inputs={"messages": [{"role": "user", "content": "history"}],
                "metadata": {"purpose": "memory_ingest", "scope_id": "run:a", "local": True}})
    result = _execute_agent(step, "openclaw", lambda *args: {
        "status": "ok", "transport": "cli", "stderr": "og-memory: mode=remote",
        "turns": [{"text": "INGEST_OK"}]}, ctx, run_memory_plugin_task)
    assert result["agent_answer"] == "INGEST_OK"
    actions = [json.loads(line)["action"] for line in Path(str(state)+"."+load().digest(str(state.parent.parent / "run"))+".events.jsonl").read_text().splitlines()]
    assert actions[-2:] == ["before_agent", "after_agent"]


@pytest.mark.parametrize("kind", ["success", "empty", "missing", "null", "preliminary", "wrong_id", "wrong_key", "unpaired", "success_then_null"])
def test_qa_requires_current_session_success_receipt(runtime, monkeypatch, kind):
    from types import SimpleNamespace
    state, module, run = runtime
    run("prepare", scope_id="run:a")
    run("set_phase", phase="qa")
    context = run("before_agent", agent_request={"metadata": {
        "purpose": "memory_qa", "scope_id": "run:a", "local": True}})["output"]
    context.update(transport="http", expected_session_key="agent:main:qa")
    monkeypatch.setattr(module, "gateway_container", lambda path: "dedicated")
    sid = "other" if kind == "wrong_id" else "current"
    key = "agent:main:other" if kind == "wrong_key" else "agent:main:qa"
    success = f"[og-memory] assemble return: 2 msgs, ws_injected={'false' if kind == 'empty' else 'true'}, first_3_roles=user,user"
    identity = f"[og-memory] assemble params keys: sessionId,sessionKey,messages, sessionKey={key}, sessionId={sid}"
    text = success + "\n" + identity
    if kind == "missing": text = ""
    if kind == "null": text = "[og-memory] compose returned null - check CE server logs"
    if kind == "preliminary": text = "[og-memory] compose result keys: messages\n[og-memory] assemble: identity=x\n" + identity
    if kind == "unpaired": text = success + "\n[og-memory] another call\n" + identity
    if kind == "success_then_null": text += "\n[og-memory] compose returned null - check CE server logs"
    def logs(cmd, **kwargs):
        assert cmd == ["docker", "logs", "--since", context["log_since"], "dedicated"]
        return SimpleNamespace(stdout=text, stderr="")
    monkeypatch.setattr(module.subprocess, "run", logs)
    inputs = {"check_context": context, "agent_result": {"transport": "http", "output": {"session_id": "current"}}}
    if kind in ("success", "empty"):
        result = module.after_agent(state, inputs)
        assert result["checked"] and result["recall_receipt"] == "native_assemble_return"
        assert result["session_id"] == "current"
    else:
        with pytest.raises(RuntimeError): module.after_agent(state, inputs)


@pytest.mark.parametrize("phase", ["ingest", "qa"])
def test_processing_exception_is_ingest_only(phase):
    module = load()
    text = '[og-memory] HTTP 503 from after_turn: {"status":"processing"}'
    if phase == "ingest": module.check_ogmemory_hook_errors(text, phase)
    else:
        with pytest.raises(RuntimeError): module.check_ogmemory_hook_errors(text, phase)


@pytest.mark.parametrize("metadata, expected", [
    ({"session_key": "QA:One"}, "agent:main:qa:one"),
    ({"session_key": "agent:main:QA"}, "agent:main:qa"),
    ({"session_id": "ABC"}, "agent:main:explicit:abc"),
    ({}, None),
])
def test_qa_expected_gateway_session(runtime, monkeypatch, metadata, expected):
    import hashlib
    state, module, run = runtime
    run("prepare", scope_id="run:a")
    run("set_phase", phase="qa")
    config = json.loads(state.with_name("openclaw.json").read_text())
    data = json.loads(state.read_text())
    data.update(gateway_ready=True, config_sha256=hashlib.sha256(state.with_name("openclaw.json").read_bytes()).hexdigest())
    monkeypatch.setenv("OPENCLAW_TRANSPORT", "http")
    request = {"metadata": {"purpose": "memory_qa", "scope_id": "run:a", **metadata}}
    if expected:
        assert module.before_agent(state, config, data, request)["expected_session_key"] == expected
    else:
        with pytest.raises(ValueError, match="explicit session"):
            module.before_agent(state, config, data, request)


def test_event_archive_is_per_owner_and_preserves_samples(runtime, tmp_path):
    state, module, run = runtime
    run("prepare", scope_id="old-scope")
    old_identity = json.loads(state.read_text())["identity"]["userId"]
    first = Path(run("finalize")["artifacts"][0]["path"]).read_text()
    def next_run(action, **inputs):
        return module.run({"action": action, "inputs": inputs,
                           "runtime_context": {"run_dir": str(tmp_path / "run-two")}})
    next_run("validate")
    next_run("prepare", scope_id="new-a")
    next_run("prepare", scope_id="new-b")
    second = Path(next_run("finalize")["artifacts"][0]["path"]).read_text()
    assert "old-scope" in first
    assert "old-scope" not in second and old_identity not in second
    assert "new-a" in second and "new-b" in second
    assert Path(tmp_path / "run/artifacts/memory_plugin/openclaw-ogmemory-events.jsonl").read_text() == first


@pytest.mark.parametrize("slow_call", [1, 2, 3, 4])
def test_readiness_rejects_success_after_deadline(runtime, monkeypatch, slow_call):
    _, module, run = runtime
    run("prepare", scope_id="run:a")
    clock = [0.0]
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    def backend(base, path, body, identity, **kw):
        calls.append(kw["timeout"])
        assert 0 < kw["timeout"] <= 0.5 - clock[0]
        clock[0] += 0.6 if len(calls) == slow_call else 0.01
        if kw.get("method") == "GET":
            return {"message_count": 2, "commit_count": 1}
        return {"idle": True, "outbox": {"total": 0}}
    monkeypatch.setattr(module, "backend", backend)
    with pytest.raises(TimeoutError):
        run("wait_ready", scope_id="run:a", session_handle={"session_id": "s"}, timeout_seconds=0.5)
    assert len(calls) == slow_call


def test_readiness_sleep_is_limited_to_remaining_budget(runtime, monkeypatch):
    _, module, run = runtime
    run("prepare", scope_id="run:a")
    clock, sleeps = [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "backend", lambda *a, **kw: {"idle": False})
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(TimeoutError):
        run("wait_ready", scope_id="run:a", session_handle={"session_id": "s"},
            timeout_seconds=0.5, interval_seconds=2)
    assert sleeps == [0.5]
