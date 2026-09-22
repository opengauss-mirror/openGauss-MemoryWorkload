"""Controller boundaries; the official plugin itself is not mocked/reimplemented."""
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mem0_lifecycle", ROOT / "skills/memory_plugins/openclaw-mem0/scripts/run_lifecycle.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
native_gateway = plugin.gateway_container
native_configure = plugin.configure

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    upstream = tmp_path / "official"
    (upstream / "dist").mkdir(parents=True)
    (upstream / "package.json").write_text('{"name":"@mem0/openclaw-mem0","version":"1.2.0"}')
    (upstream / "dist/index.js").write_text("// unchanged fixture")
    (upstream / "openclaw.plugin.json").write_text('{"id":"openclaw-mem0"}')
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"plugins": {"entries": {plugin.PLUGIN: {"config": {
        "mode": "open-source", "oss": {"vectorStore": {"provider": "qdrant"}}}}}}}))
    root = tmp_path / "runtime"
    plugin.prepare_runtime(source, root, upstream, timeout=1)
    config = json.loads((root / "openclaw.json").read_text())
    for key, value in {"MEM0_PLUGIN_STATE_FILE": str(root / "phase.json"), "OPENCLAW_TRANSPORT": "http",
        "OPENCLAW_STATE_DIR": str(root / "state"), "OPENCLAW_GATEWAY_URL": "http://127.0.0.1:18789",
        "OPENCLAW_GATEWAY_TOKEN": config["gateway"]["auth"]["token"]}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(plugin, "gateway_container", lambda path: "dedicated")
    monkeypatch.setattr(plugin.time, "sleep", lambda seconds: None)
    restarts = []
    def configure(path, config, state, phase):
        config["plugins"]["entries"][plugin.PLUGIN]["config"].update(
            userId=state["user_id"], autoCapture=phase == "ingest", autoRecall=phase == "qa")
        plugin.save(path.with_name("openclaw.json"), config)
        state.update(phase=phase, gateway_ready=True,
            config_sha256=hashlib.sha256(path.with_name("openclaw.json").read_bytes()).hexdigest())
        restarts.append(phase)
    monkeypatch.setattr(plugin, "configure", configure)
    def lifecycle(action, **inputs):
        return plugin.run({"action": action, "inputs": inputs, "runtime_context": {"run_dir": str(tmp_path / "run")}})
    lifecycle("validate")
    lifecycle("prepare", scope_id="sample-a")
    return root, upstream, lifecycle, restarts

def request(phase="ingest", session="ingest-1"):
    return {"messages": [{"role": "user", "content": "Alice lives in Paris."}],
        "metadata": {"scope_id": "sample-a", "session_key": session,
            "purpose": "memory_ingest" if phase == "ingest" else "memory_qa"}}

def finish(life, check):
    return life("after_agent", check_context=check, agent_result={"status": "ok", "transport": "http",
        "output": {"session_handle": {"session_id": "actual-session", "gateway_session_key": check["session_key"]}}})

def test_package_phase_identity_receipts(runtime, monkeypatch):
    root, upstream, life, restarts = runtime
    assert not (root / "plugin").exists()
    assert (upstream / "dist/index.js").read_text() == "// unchanged fixture"
    check = life("before_agent", agent_request=request())["output"]
    with pytest.raises(ValueError, match="unfinished"):
        life("set_phase", phase="qa")
    monkeypatch.setattr(plugin, "logs", lambda *args: "openclaw-mem0: auto-captured 2 memories")
    assert finish(life, check)["output"]["memory_count"] == 2
    life("set_phase", phase="qa")
    check = life("before_agent", agent_request=request("qa", "qa-1"))["output"]
    monkeypatch.setattr(plugin, "logs", lambda *args: "openclaw-mem0: injecting 2 memories into context")
    assert finish(life, check)["output"]["checked"]
    entry = json.loads((root / "openclaw.json").read_text())["plugins"]["entries"][plugin.PLUGIN]["config"]
    assert not entry["autoCapture"] and entry["autoRecall"]
    user = entry["userId"]
    life("prepare", scope_id="sample-b")
    assert json.loads((root / "phase.json").read_text())["user_id"] != user
    life("finalize")
    assert restarts == ["ingest", "qa", "ingest", "inactive"]

@pytest.mark.parametrize("text,phase", [
    ("openclaw-mem0: capture failed: unavailable", "ingest"),
    ("openclaw-mem0: recall failed: unavailable", "qa"),
    ("openclaw-mem0: recall timed out\nopenclaw-mem0: injecting 1 memories into context", "qa"),
    ("openclaw-mem0: auto-captured 1 memories\nopenclaw-mem0: injecting 1 memories into context", "qa"),
    ("openclaw-mem0: injecting 1 memories into context\nopenclaw-mem0: injecting 2 memories into context", "qa")])
def test_failures(text, phase):
    with pytest.raises(RuntimeError): plugin.native_receipt(text, phase)

def test_late_timer_and_empty_results():
    assert plugin.native_receipt("openclaw-mem0: injecting 2 memories into context\nopenclaw-mem0: recall timed out", "qa")["memory_count"] == 2
    assert plugin.native_receipt("openclaw-mem0: initialized", "qa") is None
    assert plugin.native_receipt("", "ingest") is None

def test_missing_hook_and_stale_receipt(runtime, monkeypatch):
    _, _, life, _ = runtime
    life("set_phase", phase="qa")
    check = life("before_agent", agent_request=request("qa"))["output"]
    monkeypatch.setattr(plugin, "logs", lambda *args: "")
    with pytest.raises(RuntimeError, match="No positive native"): finish(life, check)
    with pytest.raises(ValueError, match="context mismatch"): finish(life, {**check, "nonce": "old"})
    with pytest.raises(ValueError, match="Parallel"): life("before_agent", agent_request=request("qa"))

def test_existing_session_and_package_change(runtime):
    root, upstream, life, _ = runtime
    store = root / "state/agents/main/sessions/sessions.json"
    store.parent.mkdir(parents=True)
    store.write_text('{"agent:main:ingest-1":{"sessionId":"old"}}')
    with pytest.raises(ValueError, match="already exists"): life("before_agent", agent_request=request())
    (upstream / "dist/index.js").write_text("modified")
    with pytest.raises(ValueError, match="changed"): life("validate")

def test_mount_validation(runtime, monkeypatch):
    from types import SimpleNamespace
    root, upstream, _, _ = runtime
    monkeypatch.setenv("MEM0_GATEWAY_CONTAINER", "dedicated")
    info = {"Config": {"Labels": {"memory-bench.runtime": str(root)}, "Env": [
        "OPENCLAW_CONFIG_PATH=/runtime/openclaw.json", "OPENCLAW_STATE_DIR=/runtime/state"]},
        "Mounts": [{"Source": str(root), "Destination": "/runtime", "RW": True},
                   {"Source": str(upstream), "Destination": str(upstream), "RW": False}]}
    monkeypatch.setattr(plugin.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=json.dumps([info])))
    assert native_gateway(root / "phase.json") == "dedicated"
    info["Mounts"][1]["RW"] = True
    with pytest.raises(ValueError, match="read-only"): native_gateway(root / "phase.json")
    info["Mounts"][1]["RW"] = False
    info["Config"]["Env"] = []
    with pytest.raises(ValueError, match="config/state paths"): native_gateway(root / "phase.json")

def test_restart_applies_native_switches_and_failure_stays_unready(runtime, monkeypatch):
    root, _, _, _ = runtime
    path = root / "phase.json"
    config = json.loads((root / "openclaw.json").read_text())
    state = json.loads(path.read_text())
    calls = []
    monkeypatch.setattr(plugin.subprocess, "run", lambda args, **kw: calls.append(args))
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(plugin.urllib.request, "urlopen", lambda *a, **kw: Response())
    monkeypatch.setattr(plugin, "logs", lambda *a: f"openclaw-mem0: registered (mode: open-source, user: {state['user_id']}, autoRecall: true, autoCapture: false, skills: false)")
    native_configure(path, config, state, "qa")
    assert calls == [["docker", "restart", "dedicated"]]
    assert state["gateway_ready"] and state["phase"] == "qa"
    def fail(*args, **kw): raise OSError("Docker restart failed")
    monkeypatch.setattr(plugin.subprocess, "run", fail)
    with pytest.raises(OSError): native_configure(path, config, state, "ingest")
    assert json.loads(path.read_text())["gateway_ready"] is False
