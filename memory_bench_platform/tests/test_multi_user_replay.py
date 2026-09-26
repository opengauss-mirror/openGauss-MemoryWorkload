import json
import sys
from pathlib import Path

import pytest
import yaml


def config_file(tmp_path, *, mode="backend_direct"):
    for operation, request in (("add", {"user_id": "", "messages": [{"role": "user", "content": "PRIVATE_BODY"}]}),
                               ("search", {"user_id": "", "query": "PRIVATE_QUERY"})):
        (tmp_path / f"{operation}.jsonl").write_text(json.dumps({"request": request}) + "\n")
    instance = {"kind": "memory", "memory_skill": "fake", "healthcheck_command": [sys.executable, "-c", "pass"]}
    if mode == "openclaw":
        instance = {"kind": "openclaw", "healthcheck_command": [sys.executable, "-c", "pass"],
                    "isolation_probe_command": [sys.executable, "-c", "pass"]}
    data = {"schema": "multi-user-replay/1", "mode": mode,
            "workloads": {"w": {"format": "openmem-v1" if mode == "backend_direct" else "openmem-agent/1",
                                  "add_file": "add.jsonl", "search_file": "search.jsonl"}},
            "instances": {"i": instance},
            "users": [{"id": "alice", "workload": "w", "instance": "i"},
                      {"id": "bob", "workload": "w", "instance": "i"}]}
    if mode == "openclaw":
        for user in data["users"]:
            user["agent_id"] = user["id"]
    path = tmp_path / "workload.yaml"
    path.write_text(yaml.safe_dump(data))
    return path, data


def test_config_resolves_paths_and_separates_users(tmp_path):
    from memory_bench_platform.multi_user_config import load_config
    path, _ = config_file(tmp_path)
    config = load_config(path)
    assert config.workloads["w"].add_file == tmp_path / "add.jsonl"
    assert [u.id for u in config.users] == ["alice", "bob"]


@pytest.mark.parametrize("fault", ["duplicate", "missing_instance", "unknown", "nan", "mode", "empty", "same_agent"])
def test_config_rejects_invalid_bindings(tmp_path, fault):
    from memory_bench_platform.multi_user_config import load_config
    path, data = config_file(tmp_path, mode="openclaw" if fault == "same_agent" else "backend_direct")
    if fault == "duplicate": data["users"][1]["id"] = "alice"
    if fault == "missing_instance": data["users"][0]["instance"] = "missing"
    if fault == "unknown": data["mystery"] = True
    if fault == "nan": data["defaults"] = {"add_rate": float("nan")}
    if fault == "mode": data["mode"] = "openclaw"
    if fault == "empty": data["users"] = []
    if fault == "same_agent": data["users"][1]["agent_id"] = "alice"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError): load_config(path)


def test_config_openclaw_rejects_unmapped_fields_before_traffic(tmp_path):
    from memory_bench_platform.multi_user_config import load_config
    path, _ = config_file(tmp_path, mode="openclaw")
    (tmp_path / "search.jsonl").write_text(json.dumps({"request": {"query": "x", "filters": {"secret": "SENTINEL"}}}) + "\n")
    with pytest.raises(ValueError, match="unsupported_field") as exc:
        load_config(path)
    assert "SENTINEL" not in str(exc.value)


def test_preflight_rejects_unsupported_memory_before_start(tmp_path):
    from memory_bench_platform.multi_user_config import load_config, preflight
    path, data = config_file(tmp_path)
    data["instances"]["i"]["memory_skill"] = "openviking"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="protocol"):
        preflight(load_config(path))


def test_managed_resources_cannot_overlap(tmp_path):
    from memory_bench_platform.multi_user_config import load_config
    path, data = config_file(tmp_path)
    instance = data["instances"]["i"]
    instance.update(deployment="managed", start_command=["server"],
                    exclusive_resources=["tcp:127.0.0.1:8000", "path:state"])
    data["instances"]["j"] = dict(instance)
    data["users"][1]["instance"] = "j"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="resource"):
        load_config(path)


def test_runtime_owned_process_cleanup_after_health_failure(tmp_path):
    from memory_bench_platform.multi_user_config import Instance
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    instance = Instance(kind="memory", memory_skill="fake", deployment="managed",
                        start_command=[sys.executable, "-c", "import time; time.sleep(30)"],
                        healthcheck_command=[sys.executable, "-c", "raise SystemExit(1)"],
                        startup_timeout_seconds=0.15, exclusive_resources=["tcp:localhost:8765"])
    runtime = InstanceRuntime("i", instance, {}, "run", tmp_path, [])
    try:
        with pytest.raises(TimeoutError): runtime.start()
    finally:
        runtime.stop()
    assert runtime.process.poll() is not None


def test_openclaw_probe_cannot_pass_on_exit_code_alone(tmp_path):
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    path, _ = config_file(tmp_path, mode="openclaw")
    config = load_config(path)
    runtime = InstanceRuntime("i", config.instances["i"], {}, "run", tmp_path, config.users)
    with pytest.raises(ValueError, match="isolation"):
        runtime.probe_isolation()


def test_openclaw_namespace_is_distinct_across_instances(tmp_path, monkeypatch):
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    from memory_bench_platform.protocol import MemoryPluginTaskOutput
    path, data = config_file(tmp_path, mode="openclaw")
    data["instances"]["j"] = dict(data["instances"]["i"])
    data["users"][1].update(instance="j", agent_id="alice")
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    prepared = {}
    def plugin(skill, request, **kwargs):
        if request.action == "prepare": prepared[request.task_id.split(":")[0]] = request.inputs["namespace"]
        return MemoryPluginTaskOutput(status="ok", state="completed")
    monkeypatch.setattr(integration, "run_memory_plugin_task", plugin)
    monkeypatch.setattr(InstanceRuntime, "probe_isolation", lambda self: None)
    runtimes = [InstanceRuntime(name, config.instances[name], {}, "run", tmp_path,
                [u for u in config.users if u.instance == name]) for name in ("i", "j")]
    try:
        for runtime in runtimes: runtime.start()
        assert prepared["i"] != prepared["j"]
        assert all(runtime.namespace == prepared[runtime.name] for runtime in runtimes)
    finally:
        for runtime in runtimes: runtime.stop()


def test_probe_receives_instance_scoped_namespace(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    path, data = config_file(tmp_path, mode="openclaw")
    data["instances"]["j"] = dict(data["instances"]["i"])
    data["users"][1].update(instance="j", agent_id="alice")
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    seen = []
    def command(self, argv, timeout, payload=None, capture=False):
        seen.append(payload)
        return CompletedProcess(argv, 0, json.dumps({
            "isolated": True, "identity_mode": "agent", "run_id": self.run_id,
            "agent_ids": [u.agent_id for u in self.users],
            "write_scope_verified": True, "read_scope_verified": True}))
    monkeypatch.setattr(InstanceRuntime, "command", command)
    for name in ("i", "j"):
        runtime = InstanceRuntime(name, config.instances[name], {}, "run", tmp_path,
            [u for u in config.users if u.instance == name])
        runtime.probe_isolation()
        assert seen[-1]["instance_id"] == name
        assert seen[-1]["namespace"] == runtime.namespace
    assert seen[0]["namespace"] != seen[1]["namespace"]


def test_openclaw_base_url_overrides_inherited_gateway_url(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config, preflight
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    from memory_bench_platform.protocol import RenderedTaskInput
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(self.server.server_port)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"output":[]}')
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads: thread.start()
    try:
        monkeypatch.setenv("OPENCLAW_GATEWAY_URL", f"http://127.0.0.1:{servers[0].server_port}")
        path, data = config_file(tmp_path, mode="openclaw")
        data["instances"]["i"]["environment"] = {
            "OPENCLAW_GATEWAY_BASE_URL": f"http://127.0.0.1:{servers[1].server_port}",
            "OPENCLAW_CONFIG_PATH": "claw.json", "OPENCLAW_STATE_DIR": "state"}
        path.write_text(yaml.safe_dump(data))
        config = load_config(path)
        runtime = InstanceRuntime("i", config.instances["i"], preflight(config)["i"], "r", tmp_path, config.users)
        request = RenderedTaskInput(task_id="t", messages=[{"role": "user", "content": "hi"}],
                                    metadata={"agent_id": "alice", "session_id": "s"})
        integration.run_agent_task("openclaw", request, environment=runtime.environment, timeout_seconds=5)
        assert calls == [servers[1].server_port]
    finally:
        for server in servers: server.shutdown(); server.server_close()
        for thread in threads: thread.join()


def test_completed_compact_without_task_id_is_accepted_but_pending_is_not():
    from memory_bench_platform.integration import _validate_memory_plugin_action_output
    from memory_bench_platform.protocol import MemoryPluginTaskOutput
    complete = MemoryPluginTaskOutput(status="ok", state="completed",
        operation={"id": "compact:s", "task_id": "", "state": "completed"},
        output={"compacted": True, "completed": True})
    _validate_memory_plugin_action_output("flush", complete)
    pending = complete.model_copy(update={"operation": {"state": "running"},
                                          "output": {"compacted": True, "completed": False}})
    with pytest.raises(ValueError): _validate_memory_plugin_action_output("flush", pending)


def test_plugin_idempotency_distinguishes_phase_and_session(tmp_path, monkeypatch):
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import Instance
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    from memory_bench_platform.protocol import MemoryPluginTaskOutput
    runtime = InstanceRuntime("i", Instance(kind="openclaw", healthcheck_command=[sys.executable],
        isolation_probe_command=[sys.executable]), {}, "run", tmp_path, [])
    keys = {}
    def plugin(skill, request, **kwargs):
        operation = request.inputs.get("operation", {})
        keys.setdefault((request.action, request.inputs.get("phase") or request.inputs.get("session_id") or operation.get("id")),
                        []).append(request.idempotency_key)
        return MemoryPluginTaskOutput(status="ok", state="completed")
    monkeypatch.setattr(integration, "run_memory_plugin_task", plugin)
    for action, inputs in (("set_phase", {"phase": "ingest"}), ("set_phase", {"phase": "qa"}),
                           ("flush", {"session_id": "one"}), ("flush", {"session_id": "two"}),
                           ("flush", {"session_id": "one"}),
                           ("wait_settle", {"operation": {"id": "compact:one", "state": "completed"}}),
                           ("wait_settle", {"operation": {"id": "compact:two", "state": "completed"}})):
        runtime.plugin(action, inputs)
    assert len({values[0] for values in keys.values()}) == 6
    assert keys[("flush", "one")] == [keys[("flush", "one")][0]] * 2


def test_single_request_rate_limit_does_not_extend_phase(tmp_path, monkeypatch, memory_adapter):
    import time
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, data = config_file(tmp_path)
    data["users"] = data["users"][:1]
    data["defaults"] = {"add_rate": 2}
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(integration, "run_memory_task", lambda *a, **k: MemoryTaskOutput(
        status="ok", state="completed", operation={"session_id": "s"}))
    started = time.monotonic()
    report = run_multi_user(load_config(path), tmp_path / "out", "r")
    assert report["phase_windows_ms"]["add"] < 300
    assert time.monotonic() - started < 0.45


def test_ready_fast_user_does_not_wait_for_slow_future(tmp_path, monkeypatch, memory_adapter):
    import time
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, data = config_file(tmp_path)
    data["max_workers"] = 2
    data["instances"]["i"]["environment"] = {"TARGET": "slow"}
    data["instances"]["j"] = {**data["instances"]["i"], "environment": {"TARGET": "fast"}}
    data["users"][1]["instance"] = "j"
    data["users"][1]["workload"] = "fast"
    data["users"][1]["controls"] = {"add_rate": 100}
    data["workloads"]["fast"] = {**data["workloads"]["w"], "add_file": "fast.jsonl"}
    (tmp_path / "fast.jsonl").write_text((tmp_path / "add.jsonl").read_text() * 4)
    path.write_text(yaml.safe_dump(data))
    fast_starts = []
    def invoke(skill, request, *, environment, **kwargs):
        if request.action == "ingest":
            if environment["TARGET"] == "fast": fast_starts.append(time.monotonic())
            else: time.sleep(0.3)
        return MemoryTaskOutput(status="ok", state="completed", operation={"session_id": "s"})
    monkeypatch.setattr(integration, "run_memory_task", invoke)
    run_multi_user(load_config(path), tmp_path / "out", "r")
    assert len(fast_starts) == 4
    assert max(b-a for a, b in zip(fast_starts, fast_starts[1:])) < 0.045


def test_many_users_do_not_hold_one_file_descriptor_each(tmp_path):
    import subprocess
    from memory_bench_platform.multi_user_config import load_config
    path, data = config_file(tmp_path)
    data["users"] = [{"id": f"u{n}", "workload": "w", "instance": "i"} for n in range(80)]
    data["max_workers"] = 1
    path.write_text(yaml.safe_dump(data))
    (tmp_path / "add.jsonl").write_text((tmp_path / "add.jsonl").read_text() * 4)
    script = """
import resource, sys
from types import SimpleNamespace
from pathlib import Path
from memory_bench_platform import integration
from memory_bench_platform.multi_user_config import load_config
from memory_bench_platform.multi_user_replay import run_multi_user
from memory_bench_platform.protocol import MemoryTaskOutput
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(48, hard), hard))
integration.get_memory_manifest = lambda _: SimpleNamespace(
    capabilities={'raw_request_protocols':['openmem-v1']},
    runtime={'actions':['ingest','status','recall']}, entry=SimpleNamespace(runner='fake'))
integration.run_memory_task = lambda *a, **k: MemoryTaskOutput(
    status='ok', state='completed', operation={'session_id':'s'})
assert run_multi_user(load_config(Path(sys.argv[1])), Path(sys.argv[2]), 'r')['overall']['add']['count'] == 320
"""
    result = subprocess.run([sys.executable, "-c", script, str(path), str(tmp_path / "out")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-1000:]


def test_timed_out_probe_kills_its_child_process(tmp_path):
    import subprocess
    import time
    from memory_bench_platform.multi_user_config import Instance
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    runtime = InstanceRuntime("i", Instance(kind="memory", memory_skill="fake",
        healthcheck_command=[sys.executable]), {}, "run", tmp_path, [])
    marker = tmp_path / "orphan-wrote.txt"
    child = "import pathlib,sys,time; time.sleep(.4); pathlib.Path(sys.argv[1]).write_text('orphan')"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        runtime.command([sys.executable, "-c", parent, str(marker), child], .1, capture=True)
    runtime.stop()
    time.sleep(.5)
    assert not marker.exists()


def test_check_cleanup_does_not_mask_original_exception_when_group_exits(tmp_path, monkeypatch):
    import subprocess
    from memory_bench_platform import multi_user_runtime
    from memory_bench_platform.multi_user_config import Instance
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    runtime = InstanceRuntime("i", Instance(kind="memory", memory_skill="fake",
        healthcheck_command=[sys.executable]), {}, "run", tmp_path, [])
    class ExitedProcess:
        pid = 12345
        def communicate(self, **kwargs):
            raise ValueError("original check failure")
    monkeypatch.setattr(multi_user_runtime.subprocess, "Popen", lambda *a, **k: ExitedProcess())
    def no_group(*args):
        raise ProcessLookupError("already exited")
    monkeypatch.setattr(multi_user_runtime.os, "killpg", no_group)
    with pytest.raises(ValueError, match="original check failure"):
        runtime.command([sys.executable], 1)


def test_interrupted_during_setup_produces_importable_empty_events(tmp_path, monkeypatch, memory_adapter):
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.multi_user_report import import_report
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    path, _ = config_file(tmp_path)
    def interrupt(self): raise KeyboardInterrupt()
    monkeypatch.setattr(InstanceRuntime, "start", interrupt)
    out = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt): run_multi_user(load_config(path), out, "r")
    assert (out / "request_events.jsonl").read_text() == ""
    assert import_report(out)["status"] == "interrupted"


def test_abbreviated_explicit_integration_conflict_is_rejected(tmp_path, memory_adapter):
    from memory_bench_platform.cli import main
    path, _ = config_file(tmp_path)
    with pytest.raises(SystemExit, match="preflight failed"):
        main(["plan-run", "--benchmark", "production-http-replay", "--agent", "none",
              "--workload-config", str(path), "--memory-integ", "agent_plugin"])


def test_config_archive_identifies_skill_and_version_without_endpoint(tmp_path, monkeypatch, memory_adapter):
    from types import SimpleNamespace
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, data = config_file(tmp_path)
    data["instances"]["i"]["environment"] = {"PRIVATE_ENDPOINT": "secret://endpoint"}
    data["instances"]["j"] = {**data["instances"]["i"], "memory_skill": "other"}
    data["users"][1]["instance"] = "j"
    path.write_text(yaml.safe_dump(data))
    def manifest(skill):
        return SimpleNamespace(version={"fake": "1.0", "other": "2.0"}[skill],
            capabilities={"raw_request_protocols": ["openmem-v1"]},
            runtime={"actions": ["ingest", "status", "recall"]},
            entry=SimpleNamespace(runner="fake"))
    monkeypatch.setattr(integration, "get_memory_manifest", manifest)
    monkeypatch.setattr(integration, "run_memory_task", lambda *a, **kw: MemoryTaskOutput(
        status="ok", state="completed", operation={"session_id": "s"}))
    out = tmp_path / "out"
    run_multi_user(load_config(path), out, "r")
    archive = json.loads((out / "run_config.json").read_text())
    assert archive["instances"]["i"]["memory_skill"] == "fake"
    assert archive["instances"]["i"]["skill_version"] == "1.0"
    assert archive["instances"]["j"]["memory_skill"] == "other"
    assert archive["instances"]["j"]["skill_version"] == "2.0"
    assert "secret://endpoint" not in json.dumps(archive)


@pytest.mark.parametrize("mutation", ["append", "replace"])
def test_source_mutation_during_setup_does_not_change_replayed_snapshot(tmp_path, monkeypatch, memory_adapter, mutation):
    import hashlib
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, _ = config_file(tmp_path)
    original = (tmp_path / "add.jsonl").read_bytes()
    seen = []
    def invoke(skill, request, **kwargs):
        if request.action == "ingest":
            seen.append(request.inputs["raw_request"]["messages"][0]["content"])
        return MemoryTaskOutput(status="ok", state="completed", operation={"session_id": "s"})
    monkeypatch.setattr(integration, "run_memory_task", invoke)
    start = InstanceRuntime.start
    def mutate_after_snapshot(self):
        if mutation == "append":
            with (tmp_path / "add.jsonl").open("a") as stream:
                stream.write(json.dumps({"request": {"messages": [{"role": "user", "content": "LATE"}]}}) + "\n")
        else:
            (tmp_path / "add.jsonl").write_text(json.dumps({"request": {
                "messages": [{"role": "user", "content": "REPLACED"}]}}) + "\n")
        start(self)
    monkeypatch.setattr(InstanceRuntime, "start", mutate_after_snapshot)
    out = tmp_path / "out"
    report = run_multi_user(load_config(path), out, "r")
    archived = json.loads((out / "run_config.json").read_text())["workloads"]["w"]["files"]["add"]
    assert report["status"] == "completed"
    assert seen == ["PRIVATE_BODY", "PRIVATE_BODY"]
    assert archived == {"sha256": hashlib.sha256(original).hexdigest(), "size_bytes": len(original)}


def test_changed_invalid_source_is_rejected_before_output_or_target_writes(tmp_path, monkeypatch, memory_adapter):
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    path, _ = config_file(tmp_path)
    config = load_config(path)
    (tmp_path / "add.jsonl").write_text('{"request": {"messages": []}}\ninvalid-json\n')
    calls = []
    monkeypatch.setattr(integration, "run_memory_task", lambda *args, **kwargs: calls.append(args))
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="invalid JSON"):
        run_multi_user(config, out, "r")
    assert not out.exists()
    assert not calls


def test_workload_copy_is_private_outside_report_and_removed_after_run(tmp_path, monkeypatch, memory_adapter):
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import Replay, run_multi_user
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, _ = config_file(tmp_path)
    monkeypatch.setattr(integration, "run_memory_task", lambda *a, **k: MemoryTaskOutput(
        status="ok", state="completed", operation={"session_id": "s"}))
    original = Replay.records
    snapshots = []
    def inspect(self, user, operation):
        copy = getattr(self.config.workloads[user.workload], operation + "_file")
        snapshots.append(copy)
        assert copy.parent.stat().st_mode & 0o777 == 0o700
        assert copy.stat().st_mode & 0o777 == 0o400
        assert not copy.is_relative_to(self.output_dir)
        yield from original(self, user, operation)
    monkeypatch.setattr(Replay, "records", inspect)
    run_multi_user(load_config(path), tmp_path / "out", "r")
    assert snapshots
    assert all(not copy.exists() for copy in snapshots)


def test_unused_instance_without_installed_skill_does_not_abort_snapshot(tmp_path, monkeypatch, memory_adapter):
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    path, data = config_file(tmp_path)
    data["instances"]["unused"] = {**data["instances"]["i"], "memory_skill": "not-installed"}
    path.write_text(yaml.safe_dump(data))
    manifest = integration.get_memory_manifest
    def installed_only(skill):
        if skill == "not-installed": raise ValueError("unknown skill")
        return manifest(skill)
    monkeypatch.setattr(integration, "get_memory_manifest", installed_only)
    out = tmp_path / "out"
    run_multi_user(load_config(path), out, "r")
    assert json.loads((out / "run_config.json").read_text())["instances"]["unused"] == {
        "kind": "memory", "deployment": "external", "max_inflight": 8,
        "memory_skill": "not-installed", "skill_version": None}


def test_openclaw_archive_records_agent_and_plugin_versions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import _snapshot
    path, _ = config_file(tmp_path, mode="openclaw")
    monkeypatch.setattr(integration, "get_memory_plugin_manifest", lambda _: SimpleNamespace(version="plugin-v1"))
    monkeypatch.setattr(integration, "get_agent_manifest", lambda _: SimpleNamespace(version="agent-v2"))
    instance = _snapshot(load_config(path), "r")["instances"]["i"]
    assert instance["plugin_id"] == "openclaw-openviking"
    assert instance["skill_version"] == "plugin-v1"
    assert instance["agent_skill"] == "openclaw"
    assert instance["agent_version"] == "agent-v2"


def test_html_renders_overall_shared_window_qps(tmp_path, monkeypatch, memory_adapter):
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, _ = config_file(tmp_path)
    monkeypatch.setattr(integration, "run_memory_task", lambda *a, **kw: MemoryTaskOutput(
        status="ok", state="completed", operation={"session_id": "s"}))
    out = tmp_path / "out"
    report = run_multi_user(load_config(path), out, "r")
    page = (out / "report.html").read_text()
    assert "<h2>Overall</h2>" in page
    assert f'<td>{report["overall"]["add"]["qps"]}</td>' in page


@pytest.fixture
def memory_adapter(monkeypatch):
    from types import SimpleNamespace
    from memory_bench_platform import integration
    monkeypatch.setattr(integration, "get_memory_manifest", lambda _: SimpleNamespace(
        capabilities={"raw_request_protocols": ["openmem-v1"]},
        runtime={"actions": ["ingest", "status", "recall"]}, entry=SimpleNamespace(runner="fake")))


def test_multi_user_routing_identity_barrier_and_private_reports(tmp_path, monkeypatch, memory_adapter):
    import threading
    import time
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, data = config_file(tmp_path)
    data["instances"]["i"]["environment"] = {"TARGET": "ONE"}
    data["instances"]["j"] = {**data["instances"]["i"], "environment": {"TARGET": "TWO"}}
    data["users"][1]["instance"] = "j"
    path.write_text(yaml.safe_dump(data))
    calls = []
    lock = threading.Lock()
    def invoke(skill, request, *, environment, timeout_seconds):
        assert timeout_seconds > 0
        with lock:
            if request.action == "recall": assert len([c for c in calls if c[0] == "ingest"]) == 2
            calls.append((request.action, environment["TARGET"], request.inputs["raw_request"]["user_id"],
                          request.task_id, request.idempotency_key))
        time.sleep(0.02)
        return MemoryTaskOutput(status="ok", state="completed", operation={"session_id": "s"},
                                output={"count": 1, "secret": "PRIVATE_RESPONSE"})
    monkeypatch.setattr(integration, "run_memory_task", invoke)
    report = run_multi_user(load_config(path), tmp_path / "out", "run-1")
    assert report["status"] == "completed"
    assert len(report["users"]) == 2
    assert report["overall"]["search"]["succeeded"] == 2
    assert {c[1] for c in calls} == {"ONE", "TWO"}
    assert len({c[2] for c in calls}) == 2
    assert len({c[3] for c in calls}) == 4
    assert len({c[4] for c in calls}) == 4
    first_ids = {c[2] for c in calls}
    calls.clear()
    run_multi_user(load_config(path), tmp_path / "out2", "run-2")
    assert first_ids.isdisjoint(c[2] for c in calls)
    for file in (tmp_path / "out").rglob("*"):
        if file.is_file():
            contents = file.read_text()
            assert "PRIVATE_" not in contents
            assert "raw_request" not in contents


def test_user_failure_and_setup_failure_remain_in_report(tmp_path, monkeypatch, memory_adapter):
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    path, data = config_file(tmp_path)
    data["instances"]["j"] = {**data["instances"]["i"], "healthcheck_command": [sys.executable, "-c", "exit(1)"],
                               "startup_timeout_seconds": 0.05}
    data["users"][1]["instance"] = "j"
    path.write_text(yaml.safe_dump(data))
    def fail(*args, **kwargs): raise TimeoutError("PRIVATE_FAILURE")
    monkeypatch.setattr(integration, "run_memory_task", fail)
    report = run_multi_user(load_config(path), tmp_path / "out", "r")
    assert report["status"] == "failed"
    assert report["users"]["bob"]["status"] == "setup_failed"
    assert report["users"]["alice"]["operations"]["add"]["timeouts"] == 1
    assert "PRIVATE_FAILURE" not in json.dumps(report)


def test_instance_cap_and_sequential_user_requests(tmp_path, monkeypatch, memory_adapter):
    import threading
    import time
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, data = config_file(tmp_path)
    data["instances"]["i"]["max_inflight"] = 1
    path.write_text(yaml.safe_dump(data))
    active = 0
    peak = 0
    lock = threading.Lock()
    def invoke(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock: active -= 1
        return MemoryTaskOutput(status="ok", state="completed", operation={"session_id": "s"})
    monkeypatch.setattr(integration, "run_memory_task", invoke)
    report = run_multi_user(load_config(path), tmp_path / "out", "r")
    assert peak == 1
    assert report["overall"]["add"]["succeeded"] == 2


def test_openclaw_real_http_sessions_and_single_instance_lifecycle(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.protocol import MemoryPluginTaskOutput
    calls, phases, plugin_actions = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((dict(self.headers), body, list(phases)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": "PRIVATE_ANSWER"}]}]}).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path, data = config_file(tmp_path, mode="openclaw")
    data["instances"]["i"]["environment"] = {"OPENCLAW_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}",
        "OPENCLAW_CONFIG_PATH": str(tmp_path / "config.json"), "OPENCLAW_STATE_DIR": str(tmp_path / "state")}
    data["instances"]["i"]["isolation_probe_command"] = [sys.executable, "-c",
        "import sys,json; p=json.load(sys.stdin); print(json.dumps(dict(isolated=True,identity_mode='agent',run_id=p['run_id'],agent_ids=[u['agent_id'] for u in p['users']],write_scope_verified=True,read_scope_verified=True)))"]
    path.write_text(yaml.safe_dump(data))
    def plugin(skill, request, **kwargs):
        plugin_actions.append(request.action)
        if request.action == "set_phase": phases.append(request.inputs["phase"])
        if request.action == "flush":
            assert len(calls) == 2
            assert request.inputs["agent_id"] in {"alice", "bob"}
            assert request.inputs["session_key"].startswith("agent:" + request.inputs["agent_id"])
        return MemoryPluginTaskOutput(status="ok", state="completed", operation={"state": "completed"}, output={"method": "adapter_completed"})
    monkeypatch.setattr(integration, "run_memory_plugin_task", plugin)
    try:
        report = run_multi_user(load_config(path), tmp_path / "out", "r")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert report["status"] == "completed"
    assert len(calls) == 4
    assert plugin_actions.count("prepare") == plugin_actions.count("finalize") == 1
    assert phases == ["ingest", "qa"]
    assert plugin_actions.count("flush") == 2
    assert all(c[2] == ["ingest"] for c in calls[:2])
    assert all(c[2] == ["ingest", "qa"] for c in calls[2:])
    sessions = [{k.lower(): v for k, v in c[0].items()}["x-openclaw-session-key"] for c in calls]
    assert len(set(sessions)) == 4
    assert report["overall"]["search"]["result_count"] is None
    assert report["instances"]["i"]["isolation"] == "probe_verified"


def test_aggregate_percentiles_and_window_not_average_user_qps():
    from memory_bench_platform.multi_user_report import summarize
    rows = [{"operation": "search", "duration_ms": n, "started_ms": 0, "ended_ms": n,
             "status": "ok", "error_type": None, "result_count": 0, "payload_size_bytes": 10}
            for n in [10, 20, 30, 100]]
    result = summarize(rows, "search", 200)
    assert result["qps"] == 20
    assert result["p50_ms"] == 25
    assert result["p95_ms"] == pytest.approx(89.5)
    assert result["active_window_ms"] == 100


def test_cli_workload_config_runs_and_analyzes(tmp_path, monkeypatch, memory_adapter, capsys):
    from memory_bench_platform import integration
    from memory_bench_platform.cli import main
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, _ = config_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(integration, "run_memory_task", lambda *a, **kw: MemoryTaskOutput(
        status="ok", state="completed", operation={"session_id": "s"}))
    main(["run", "--benchmark", "production-http-replay", "--agent", "none",
          "--workload-config", str(path), "--run-id", "multi"])
    output = tmp_path / "runs" / "multi"
    assert (output / "summary.json").exists()
    main(["analyze-run", "--run-dir", str(output)])
    assert (output / "report.html").exists()
    with pytest.raises(SystemExit):
        main(["run", "--benchmark", "production-http-replay", "--agent", "none",
              "--workload-config", str(path), "--data-path", str(tmp_path)])


def test_import_rejects_unknown_nested_fields(tmp_path, monkeypatch, memory_adapter):
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.multi_user_report import import_report
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, _ = config_file(tmp_path)
    monkeypatch.setattr(integration, "run_memory_task", lambda *a, **kw: MemoryTaskOutput(
        status="ok", state="completed", operation={"session_id": "s"}))
    out = tmp_path / "out"
    report = run_multi_user(load_config(path), out, "r")
    report["users"]["alice"]["operations"]["add"]["raw"] = "PRIVATE_INJECTION"
    (out / "summary.json").write_text(json.dumps(report))
    with pytest.raises(ValueError) as exc: import_report(out)
    assert "PRIVATE_INJECTION" not in str(exc.value)


def test_queue_time_excludes_previous_request_service_time(tmp_path, monkeypatch, memory_adapter):
    import time
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, data = config_file(tmp_path)
    data["users"] = data["users"][:1]
    path.write_text(yaml.safe_dump(data))
    (tmp_path / "add.jsonl").write_text((tmp_path / "add.jsonl").read_text() * 2)
    def invoke(*a, **kw):
        time.sleep(0.12)
        return MemoryTaskOutput(status="ok", state="completed", operation={"session_id": "s"})
    monkeypatch.setattr(integration, "run_memory_task", invoke)
    run_multi_user(load_config(path), tmp_path / "out", "r")
    events = [json.loads(line) for line in (tmp_path / "out/request_events.jsonl").read_text().splitlines()]
    assert events[1]["duration_ms"] >= 100
    assert events[1]["queue_ms"] < 80


def test_readiness_probe_requires_exact_operation_evidence(tmp_path, monkeypatch):
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import Replay
    from memory_bench_platform.protocol import MemoryPluginTaskOutput
    from memory_bench_platform import integration
    import time
    path, _ = config_file(tmp_path, mode="openclaw")
    config = load_config(path)
    replay = Replay(config, tmp_path, "r", {"i": {}})
    user = config.users[0]
    record = next(replay.records(user, "add"))
    replay.sessions[user.id][record.request_id] = {"session_id": "s"}
    monkeypatch.setattr(integration, "run_memory_plugin_task", lambda *a, **k: MemoryPluginTaskOutput(
        status="ok", state="completed", operation={"task_id": "task"}, output={"method": "task_list_poll"}))
    event = replay.invoke(user, record, "drain", time.monotonic())
    assert event["status"] == "failed"


def test_report_requires_all_operations():
    from memory_bench_platform.multi_user_report import Report
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Report.model_validate({"schema": "multi-user-report/1", "run_id": "r", "mode": "backend_direct",
            "model_mode": "mock-fixed", "status": "completed", "latency_kind": "memory_call_including_add_settle",
            "users": {}, "instances": {}, "overall": {}, "phase_windows_ms": {}, "limitations": []})


def test_preflight_resolves_openclaw_base_url_and_paths(tmp_path):
    from memory_bench_platform.multi_user_config import load_config, preflight
    path, data = config_file(tmp_path, mode="openclaw")
    data["instances"]["i"]["environment"] = {"OPENCLAW_GATEWAY_BASE_URL": "http://127.0.0.1:9000",
        "OPENCLAW_CONFIG_PATH": "claw.json", "OPENCLAW_STATE_DIR": "state"}
    path.write_text(yaml.safe_dump(data))
    env = preflight(load_config(path))["i"]
    assert env["OPENCLAW_CONFIG_PATH"] == str(tmp_path / "claw.json")
    assert env["OPENCLAW_STATE_DIR"] == str(tmp_path / "state")


def test_backend_real_subprocess_http_routes_two_instances(tmp_path, monkeypatch, memory_adapter):
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.server.server_port, body))
            result = {"status": "ok", "state": "completed", "operation": {"session_id": "s"},
                      "output": {"evidence_text": "PRIVATE_EVIDENCE", "count": 0}}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads: t.start()
    path, data = config_file(tmp_path)
    data["instances"]["j"] = dict(data["instances"]["i"])
    data["users"][1]["instance"] = "j"
    for name, server in zip(["i", "j"], servers):
        data["instances"][name]["environment"] = {"TEST_MEMORY_URL": f"http://127.0.0.1:{server.server_port}"}
    path.write_text(yaml.safe_dump(data))
    fixture = Path(__file__).parent / "multi_user_memory_fixture.py"
    monkeypatch.setattr(integration, "_script_for_manifest", lambda *a: fixture)
    before = os.environ.get("TEST_MEMORY_URL")
    try:
        report = run_multi_user(load_config(path), tmp_path / "out", "r")
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
        for t in threads: t.join()
    assert report["overall"]["search"]["succeeded"] == 2
    assert len(received) == 4
    for port, body in received:
        alias = "alice" if port == servers[0].server_port else "bob"
        assert body["inputs"]["raw_request"]["user_id"] == report["users"][alias]["user_id"]
        assert body["scope_id"] == report["users"][alias]["user_id"]
        assert body["inputs"]["raw_request"].get("query", "PRIVATE_QUERY") == "PRIVATE_QUERY"
    assert os.environ.get("TEST_MEMORY_URL") == before


def test_interrupt_writes_partial_report_and_cleans_owned_process(tmp_path, monkeypatch, memory_adapter):
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import Replay, run_multi_user
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    path, data = config_file(tmp_path)
    data["instances"]["i"].update(deployment="managed", exclusive_resources=["tcp:localhost:9921"],
        start_command=[sys.executable, "-c", "import time; time.sleep(30)"])
    path.write_text(yaml.safe_dump(data))
    processes = []
    original_start = InstanceRuntime.start
    def start(self):
        original_start(self)
        processes.append(self.process)
    monkeypatch.setattr(InstanceRuntime, "start", start)
    def interrupt(*args): raise KeyboardInterrupt()
    monkeypatch.setattr(Replay, "phase", interrupt)
    with pytest.raises(KeyboardInterrupt): run_multi_user(load_config(path), tmp_path / "out", "r")
    report = json.loads((tmp_path / "out/summary.json").read_text())
    assert report["status"] == "interrupted"
    assert all(p.poll() is not None for p in processes)


def test_openclaw_http_fractional_timeout_preserved(monkeypatch):
    import importlib.util
    import io
    from memory_bench_platform.paths import SKILLS_ROOT
    path = SKILLS_ROOT / "agents/openclaw/scripts/run_task.py"
    spec = importlib.util.spec_from_file_location("fractional_openclaw", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:9000")
    timeouts = []
    def urlopen(request, timeout):
        timeouts.append(timeout)
        return io.BytesIO(b'{"output": []}')
    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    module.run_openclaw_http({"task_id": "r", "messages": [{"role": "user", "content": "hi"}],
                     "metadata": {"agent_id": "alice", "timeout_seconds": 0.25}})
    assert timeouts == [0.25]


def test_failed_finalize_preserves_private_recovery_snapshot(tmp_path, monkeypatch):
    import shutil
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_runtime import InstanceRuntime
    from memory_bench_platform import integration
    from memory_bench_platform.protocol import MemoryPluginTaskOutput
    path, _ = config_file(tmp_path, mode="openclaw")
    config = load_config(path)
    def plugin(skill, request, **kwargs):
        scratch = Path(request.runtime_context.run_dir)
        if request.action == "prepare":
            (scratch / "snapshot.json").write_text("PRIVATE_CONFIG")
        if request.action == "finalize": raise RuntimeError("restore failed")
        return MemoryPluginTaskOutput(status="ok", state="completed")
    monkeypatch.setattr(integration, "run_memory_plugin_task", plugin)
    monkeypatch.setattr(InstanceRuntime, "probe_isolation", lambda self: None)
    runtime = InstanceRuntime("i", config.instances["i"], {}, "r", tmp_path, config.users)
    runtime.start()
    scratch = runtime.scratch
    try:
        runtime.stop()
        assert runtime.cleanup_failed
        assert (scratch / "snapshot.json").read_text() == "PRIVATE_CONFIG"
        assert scratch.stat().st_mode & 0o077 == 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_bad_result_count_does_not_abort_other_users(tmp_path, monkeypatch, memory_adapter):
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform import integration
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, _ = config_file(tmp_path)
    monkeypatch.setattr(integration, "run_memory_task", lambda *a, **k: MemoryTaskOutput(
        status="ok", state="completed", operation={"session_id": "s"}, output={"count": -1}))
    report = run_multi_user(load_config(path), tmp_path / "out", "r")
    assert report["status"] == "failed"
    assert report["overall"]["search"]["failed"] == 2


def test_slow_instance_cannot_take_fast_instances_last_slot(tmp_path, monkeypatch, memory_adapter):
    import time
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, data = config_file(tmp_path)
    data["max_workers"] = 2
    data["instances"]["i"]["environment"] = {"TARGET": "slow"}
    data["instances"]["j"] = {**data["instances"]["i"], "environment": {"TARGET": "fast"}}
    data["users"].append({"id": "charlie", "workload": "fast", "instance": "j"})
    data["workloads"]["fast"] = {**data["workloads"]["w"], "add_file": "fast.jsonl"}
    (tmp_path / "fast.jsonl").write_text((tmp_path / "add.jsonl").read_text() * 2)
    path.write_text(yaml.safe_dump(data))
    order = []
    def invoke(skill, request, *, environment, **kwargs):
        if request.action == "ingest":
            order.append(environment["TARGET"])
            time.sleep(0.3 if environment["TARGET"] == "slow" else 0.01)
        return MemoryTaskOutput(status="ok", state="completed", operation={"session_id": "s"})
    monkeypatch.setattr(integration, "run_memory_task", invoke)
    run_multi_user(load_config(path), tmp_path / "out", "r")
    assert order[:3] == ["slow", "fast", "fast"]


@pytest.mark.parametrize("body", [{"status": "failed", "error": {"message": "PRIVATE_ERROR"}}, {},
                                  {"status": "incomplete", "output": []}])
def test_openclaw_http_bad_response_is_failed_request(tmp_path, monkeypatch, body):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import Replay
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path, _ = config_file(tmp_path, mode="openclaw")
    config = load_config(path)
    replay = Replay(config, tmp_path, "r", {"i": {"OPENCLAW_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}"}})
    try:
        user = config.users[0]
        event = replay.invoke(user, next(replay.records(user, "search")), "search", time.monotonic())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert event["status"] == "failed"
    assert "PRIVATE_ERROR" not in json.dumps(event)


def test_adapter_artifacts_are_outside_archive_and_isolated(tmp_path, monkeypatch, memory_adapter):
    from memory_bench_platform import integration
    from memory_bench_platform.multi_user_config import load_config
    from memory_bench_platform.multi_user_replay import run_multi_user
    from memory_bench_platform.protocol import MemoryTaskOutput
    path, data = config_file(tmp_path)
    data["instances"]["j"] = dict(data["instances"]["i"])
    data["users"][1]["instance"] = "j"
    path.write_text(yaml.safe_dump(data))
    directories = set()
    def invoke(skill, request, **kwargs):
        run_dir = Path(request.runtime_context.run_dir)
        directories.add(run_dir)
        (run_dir / "artifacts").mkdir(exist_ok=True)
        (run_dir / "artifacts/request.json").write_text("PRIVATE_ARTIFACT")
        return MemoryTaskOutput(status="ok", state="completed", operation={"session_id": "s"})
    monkeypatch.setattr(integration, "run_memory_task", invoke)
    out = tmp_path / "out"
    run_multi_user(load_config(path), out, "r")
    assert len(directories) == 2
    assert all(not p.exists() for p in directories)
    assert all("PRIVATE_ARTIFACT" not in file.read_text() for file in out.rglob("*") if file.is_file())
