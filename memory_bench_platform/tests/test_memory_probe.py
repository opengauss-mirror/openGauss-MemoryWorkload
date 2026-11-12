from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
from threading import Event, Thread
from urllib.parse import urlsplit

import pytest

from memory_bench_platform.memory_probe import probe


def fake_backend(*, leak=False, fail_flush=False, pending=False):
    requests = []
    def invoke(request):
        requests.append(request)
        output = {}
        operation = {"session_id": "test-session", "scope_id": request.inputs["scope_id"]}
        state = "completed"
        status = "ok"
        if request.action == "ingest":
            output = {"session_id": "test-session"}
            state = "accepted"
        elif request.action == "flush":
            assert request.inputs["session_id"] == "test-session"
            if fail_flush:
                state, status = "failed", "failed"
            operation["compact_completed"] = True
        elif request.action == "status":
            assert request.inputs["operation"]["compact_completed"]
            state = "accepted" if pending else "completed"
        elif request.action == "recall":
            empty = request.inputs["scope_id"].endswith(":empty")
            output = {"evidence_text": "Hangzhou" if leak or not empty else ""}
        return {"status": status, "state": state, "operation": operation, "output": output}
    return invoke, requests


def test_agent_free_lifecycle_and_isolation(tmp_path):
    invoke, requests = fake_backend()
    report = probe("ogmemory", tmp_path / "probe", invoke=invoke)
    assert report["status"] == "passed"
    assert [r.action for r in requests] == ["ingest", "flush", "status", "recall", "recall"]
    assert all(r.runtime_context.agent_id == "none" for r in requests)
    assert requests[-1].inputs["scope_id"] != requests[-2].inputs["scope_id"]
    assert (tmp_path / "probe/probe.json").exists()


def test_probe_waits_for_written_fact_to_be_searchable(tmp_path, monkeypatch):
    invoke, requests = fake_backend()
    recalls = 0

    def delayed_recall(request):
        nonlocal recalls
        result = invoke(request)
        if request.action == "recall" and not request.inputs["scope_id"].endswith(":empty"):
            recalls += 1
            if recalls == 1:
                result["output"] = {"evidence_text": ""}
        return result

    monkeypatch.setattr("memory_bench_platform.memory_probe.time.sleep", lambda _: None)
    report = probe("mem0", tmp_path / "probe", invoke=delayed_recall, ready_timeout=1)
    assert report["status"] == "passed"
    assert [r.action for r in requests] == ["ingest", "recall", "recall", "recall"]


@pytest.mark.parametrize("options", [{"leak": True}, {"fail_flush": True}, {"pending": True}])
def test_failures_are_recorded_without_write_retries(tmp_path, options):
    invoke, requests = fake_backend(**options)
    report = probe("ogmemory", tmp_path / "probe", invoke=invoke, ready_timeout=0.01)
    assert report["status"] == "failed"
    assert sum(r.action == "ingest" for r in requests) == 1
    assert sum(r.action == "flush" for r in requests) == 1
    if options.get("fail_flush") or options.get("pending"):
        assert not any(r.action == "recall" for r in requests)
    assert (tmp_path / "probe/probe.json").exists()


def test_refuses_to_overwrite_probe_records(tmp_path):
    invoke, requests = fake_backend()
    with pytest.raises(FileExistsError):
        probe("ogmemory", tmp_path, invoke=invoke)
    assert not requests


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -float("inf"), 0, -1])
def test_invalid_timeout_rejected_before_side_effects(tmp_path, timeout):
    invoke, requests = fake_backend()
    target = tmp_path / "probe"
    with pytest.raises(ValueError, match="finite and positive"):
        probe("ogmemory", target, ready_timeout=timeout, invoke=invoke)
    assert not requests and not target.exists()


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf", "0", "-1"])
def test_cli_rejects_invalid_timeout(monkeypatch, tmp_path, timeout):
    from memory_bench_platform.memory_probe import main
    monkeypatch.setattr("sys.argv", ["probe", "--memory-backend", "ogmemory",
                        "--output", str(tmp_path / "probe"), "--ready-timeout=" + timeout])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert not (tmp_path / "probe").exists()


@pytest.mark.parametrize("receipt_status", ["SUCCEEDED", "UNKNOWN"])
def test_ogmemory_probe_saves_receipt_check_after_http_read_timeout(monkeypatch, tmp_path, receipt_status):
    compact_started = Event()
    release_compact = Event()
    calls = []
    ingest_account = None

    class Backend(BaseHTTPRequestHandler):
        def reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            nonlocal ingest_account
            path = self.path
            calls.append(path)
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if path.endswith("/messages"):
                ingest_account = payload["accountId"]
                self.reply({"ok": True, "message_id": "m1"})
            elif path == "/api/v1/compact":
                compact_started.set()
                # No HTTP response until the probe has finished. Its client must time out.
                release_compact.wait(5)
            elif path.endswith("/wait_until_idle"):
                self.reply({"idle": True, "outbox": {"total": 0, "supported": True}})
            elif path == "/api/v1/compose":
                messages = [{"_ogmem": True, "content": "Hangzhou"}] if payload["accountId"] == ingest_account else []
                self.reply({"messages": messages})
            else:
                self.send_error(404)

        def do_GET(self):
            path = urlsplit(self.path).path
            calls.append(path)
            if path.endswith("/context"):
                self.reply({"session_id": path.split("/")[4], "archives": [{"archive_id": "a"}]})
            elif path.endswith("/write-requests/compaction-a"):
                self.reply({"request_id": "compaction-a", "archive_id": "a", "request_status": receipt_status,
                            "source_status": "ARCHIVED", "semantic_status": "COMPLETE"})
            else:
                self.send_error(404)

    run_subprocess = subprocess.run
    budgets = {}

    def record_budget(*args, **kwargs):
        action = json.loads(kwargs["input"])["action"]
        budgets.setdefault(action, []).append(kwargs["timeout"])
        return run_subprocess(*args, **kwargs)

    monkeypatch.setattr("memory_bench_platform.memory_probe.subprocess.run", record_budget)
    monkeypatch.setenv("OGMEM_FLUSH_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with ThreadingHTTPServer(("127.0.0.1", 0), Backend) as backend:
        Thread(target=backend.serve_forever, daemon=True).start()
        monkeypatch.setenv("OGMEM_API_URL", f"http://127.0.0.1:{backend.server_port}")
        try:
            report = probe("ogmemory", tmp_path / "probe")
        finally:
            release_compact.set()
            backend.shutdown()

    assert compact_started.is_set()
    assert budgets["ingest"] == [90]
    assert budgets["flush"] == [30.2]
    assert calls.count("/api/v1/compact") == 1
    assert any(path.endswith("/write-requests/compaction-a") for path in calls)
    assert json.loads((tmp_path / "probe" / "probe.json").read_text(encoding="utf-8")) == report
    flush = report["steps"][1]["result"]
    assert flush["operation"]["compact_timeout"] is True
    assert flush["operation"]["timeout_status"]["request_status"] == receipt_status
    if receipt_status == "SUCCEEDED":
        assert report["status"] == "passed"
        assert flush["output"]["backend_result"]["confirmed_after_timeout"] is True
    else:
        assert report["status"] == "failed"
        assert flush["error"]["code"] == "CompactTimeout"
