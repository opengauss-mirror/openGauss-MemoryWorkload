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
