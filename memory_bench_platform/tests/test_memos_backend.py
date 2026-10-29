import importlib.util
import io
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("memos_runner", ROOT / "skills/memories/memos/scripts/run_operation.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def invoke(action="recall", inputs=None, data=None, env=None):
    calls = []
    def opener(req, timeout):
        if req.get_method() == "GET":
            return io.BytesIO(json.dumps({"code": 200, "data": {"scheduler_summary": dict(waiting=0, in_progress=0, pending=0, failed=0, cancelled=0)}}).encode())
        calls.append((req, json.loads(req.data), timeout))
        return io.BytesIO(json.dumps(data).encode())
    result = runner.run_operation({"action": action, "task_id": "task1", "inputs": {"scope_id": "run:episode", **(inputs or {})}}, environ={"MEMOS_EXCLUSIVE_INSTANCE": "1", **(env or {})}, urlopen=opener)
    return result, calls


def test_ingest_sync_scope_and_history():
    r, calls = invoke("ingest", {"content": "Alice: Paris", "occurred_at": "2023-05-08"}, {"code": 200, "message": "Memory added successfully", "data": []})
    assert r["state"] == "completed"
    req, body, _ = calls[0]
    assert req.full_url.endswith("/product/add")
    assert body["async_mode"] == "sync"
    assert body["writable_cube_ids"] == [r["operation"]["user_id"]]
    assert body["session_id"] == r["output"]["session_id"]
    assert "2023-05-08" in body["messages"][0]["content"]
    assert "Alice: Paris" in body["messages"][0]["content"]


@pytest.mark.parametrize("data", [{"text_mem": [{"memories": [{"memory": "Paris"}]}]}, {"memory_detail_list": [{"memory_value": "Paris"}]}])
def test_search_native_shapes_and_auth(data):
    r, calls = invoke(inputs={"query": "Where?", "node_limit": 7}, data={"code": 200, "message": "Search completed successfully", "data": data}, env={"MEMOS_API_KEY": "secret"})
    assert r["state"] == "completed" and r["output"]["evidence_text"] == "Paris"
    req, body, _ = calls[0]
    assert body["top_k"] == 7 and body["readable_cube_ids"] == [r["operation"]["user_id"]]
    assert req.get_header("Authorization") == "Token secret"


@pytest.mark.parametrize("data", [{}, {"text_mem": "bad"}, {"text_mem": [{}]}, {"memory_detail_list": [{}]}, None])
def test_malformed_search_not_empty_success(data):
    r, _ = invoke(inputs={"query": "Where?"}, data={"code": 200, "message": "Search completed successfully", "data": data})
    assert r["state"] == "failed"


@pytest.mark.parametrize("limit", [0, -1, 1001, None, True, 1.5, ""])
def test_invalid_limit_before_http(limit):
    r, calls = invoke(inputs={"query": "Where?", "node_limit": limit})
    assert r["state"] == "failed" and not calls


def test_empty_recall_valid_and_scope_distinct():
    response = {"code": 200, "message": "Search completed successfully", "data": {"text_mem": []}}
    r, _ = invoke(inputs={"query": "Where?"}, data=response)
    other, _ = invoke(inputs={"query": "Where?", "scope_id": "other"}, data=response)
    assert r["state"] == "completed" and r["output"]["evidence_text"] == ""
    assert r["operation"]["user_id"] != other["operation"]["user_id"]


def test_no_write_retry_or_secret_leak():
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("secret")
    r = runner.run_operation({"action": "ingest", "task_id": "x", "inputs": {"scope_id": "a", "content": "history"}}, environ={"MEMOS_API_KEY": "secret", "MEMOS_EXCLUSIVE_INSTANCE": "1"}, urlopen=fail)
    assert len(calls) == 1 and "secret" not in json.dumps(r)


def test_manifest_and_composer():
    from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding
    from memory_bench_platform.composer import compose_run_plan
    from memory_bench_platform.integration import get_memory_manifest
    scenario=BenchmarkScenario.model_validate(json.loads((ROOT/"tests/golden/multi_checkpoint_scenario.json").read_text()))
    caps=get_memory_manifest("memos").capabilities
    plan=compose_run_plan(scenario, RunBinding(benchmark_id=scenario.benchmark_id, agent_id="openclaw", memory_id="memos", memory_integration="backend_direct", run_id="test"), {"memory":caps})
    assert any(s["inputs"].get("action")=="ingest" for s in plan["steps"])
    assert not any(s["inputs"].get("action") in ("flush", "status") for s in plan["steps"])


@pytest.mark.parametrize("summary", [None, {}, {"waiting": 0, "in_progress": 0, "pending": 0, "failed": 1, "cancelled": 0}])
def test_failed_or_missing_scheduler_ack_is_failure(summary):
    def opener(req, timeout):
        result = {"code": 200, "message": "Memory added successfully", "data": []} if req.get_method() == "POST" else {"code": 200, "data": {"scheduler_summary": summary}}
        return io.BytesIO(json.dumps(result).encode())
    r=runner.run_operation({"action":"ingest", "task_id":"x", "inputs":{"scope_id":"s", "content":"history"}}, environ={"MEMOS_EXCLUSIVE_INSTANCE":"1"}, urlopen=opener)
    assert r["state"] == "failed"


def test_shared_instance_rejected_before_write():
    calls=[]
    r=runner.run_operation({"action":"ingest", "task_id":"x", "inputs":{"scope_id":"s", "content":"history"}}, environ={}, urlopen=lambda *a,**kw:calls.append(1))
    assert r["state"] == "failed" and not calls


def test_pending_scheduler_is_polled_without_repeating_write(monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    methods=[]
    def opener(req, timeout):
        methods.append(req.get_method())
        if req.get_method()=="POST":
            response={"code":200,"message":"Memory added successfully","data":[]}
        else:
            response={"code":200,"data":{"scheduler_summary":dict(waiting=0,in_progress=int(len(methods)==2),pending=0,failed=0,cancelled=0)}}
        return io.BytesIO(json.dumps(response).encode())
    r=runner.run_operation({"action":"ingest","task_id":"x","inputs":{"scope_id":"s","content":"history"}},environ={"MEMOS_EXCLUSIVE_INSTANCE":"1"},urlopen=opener)
    assert r["state"]=="completed" and methods==["POST","GET","GET"]


@pytest.mark.parametrize("value, expected", [
    ("2023-05-08", "2023-05-08T00:00:00"),
    ("2023-06-09T19:55:00", "2023-06-09T19:55:00"),
    ("2023-06-09T19:55:00+08:00", "2023-06-09T19:55:00+08:00"),
    ("2023-06-09T19:55:00Z", "2023-06-09T19:55:00+00:00"),
    ("7:55 pm on 9 June, 2023", "2023-06-09T19:55:00"),
])
def test_ingest_native_chat_time_preserves_content(value, expected):
    r, calls = invoke("ingest", {"content": "Alice: Paris\nBob: Hello", "occurred_at": value},
                      {"code": 200, "message": "Memory added successfully", "data": []})
    assert r["state"] == "completed"
    assert calls[0][1]["messages"] == [{
        "role": "user", "content": f"[Historical conversation date: {value}]\nAlice: Paris\nBob: Hello",
        "chat_time": expected,
    }]


@pytest.mark.parametrize("inputs, expected", [
    ({}, None),
    ({"occurred_at": None}, None),
    ({"timestamp": "2023-05-08"}, "2023-05-08T00:00:00"),
    ({"occurred_at": None, "timestamp": "2023-05-08"}, "2023-05-08T00:00:00"),
    ({"occurred_at": "2023-05-08", "timestamp": "2026-10-19"}, "2023-05-08T00:00:00"),
])
def test_ingest_time_fallback_and_absence(inputs, expected):
    r, calls = invoke("ingest", {"content": "history", **inputs},
                      {"code": 200, "message": "Memory added successfully", "data": []})
    assert r["state"] == "completed"
    message = calls[0][1]["messages"][0]
    if expected is None:
        assert message == {"role": "user", "content": "history"}
    else:
        assert message["chat_time"] == expected


@pytest.mark.parametrize("value", ["", "bad-date", "2023-02-30", "7:55 pm on 30 February, 2023", 0, True, [], {}])
def test_invalid_history_time_fails_before_write(value):
    r, calls = invoke("ingest", {"content": "history", "occurred_at": value, "timestamp": "2023-05-08"})
    assert r["state"] == "failed" and not calls
    assert r["error"]["code"] == "InvalidHistoricalTime"


@pytest.mark.parametrize("invalid", [{}, False, "", 0])
@pytest.mark.parametrize("field", ["text_mem", "pref_mem", "preference_detail_list"])
def test_falsey_non_array_search_results_are_failures(invalid, field):
    data = {"memory_detail_list": []} if field == "preference_detail_list" else {"text_mem": []}
    data[field] = invalid
    r, _ = invoke(inputs={"query": "Where?"}, data={
        "code": 200, "message": "Search completed successfully", "data": data})
    assert r["state"] == "failed"
    assert "evidence_text" not in r["output"]


@pytest.mark.parametrize("data", [
    {"memory_detail_list": [], "preference_detail_list": None},
    {"memory_detail_list": [], "preference_detail_list": []},
    {"text_mem": [], "pref_mem": None},
    {"text_mem": None},
])
def test_nullable_native_results_remain_supported(data):
    r, _ = invoke(inputs={"query": "Where?"}, data={
        "code": 200, "message": "Search completed successfully", "data": data})
    assert r["state"] == "completed" and r["output"]["memories"] == []


@pytest.mark.parametrize("code", [200, 301, 302, 303, 307, 308])
@pytest.mark.parametrize("redirect_path", [
    "/product/add", "/product/search", "/product/scheduler/allstatus",
])
def test_http_redirect_never_reaches_target(code, redirect_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    source_calls, target_calls = [], []
    def payload(path):
        if path == "/product/add":
            return {"code": 200, "message": "Memory added successfully", "data": []}
        if path == "/product/search":
            return {"code": 200, "message": "Search completed successfully",
                    "data": {"text_mem": []}}
        return {"code": 200, "data": {"scheduler_summary": dict(
            waiting=0, in_progress=0, pending=0, failed=0, cancelled=0)}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.do_GET()

        def do_GET(self):
            calls = source_calls if self.server is source else target_calls
            calls.append((self.command,
                          self.path, self.headers.get("Authorization")))
            if self.server is source and self.path == redirect_path and code != 200:
                self.send_response(code)
                self.send_header("Location", target_url + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(payload(self.path)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    source = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    target = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    target_url = f"http://127.0.0.1:{target.server_port}"
    threads = [Thread(target=s.serve_forever, kwargs={"poll_interval": .01})
               for s in (source, target)]
    for thread in threads:
        thread.start()
    try:
        action = "recall" if redirect_path == "/product/search" else "ingest"
        result = runner.run_operation({"action": action, "task_id": "redirect-test",
            "inputs": {"scope_id": "redirect-test", "query": "Where?", "content": "history"}},
            environ={"MEMOS_API_URL": f"http://127.0.0.1:{source.server_port}",
                     "MEMOS_API_KEY": "synthetic-redirect-secret", "MEMOS_EXCLUSIVE_INSTANCE": "1",
                     "MEMOS_HTTP_TIMEOUT_SECONDS": "5"})
        assert result["state"] == ("completed" if code == 200 else "failed")
        assert target_calls == []
        assert [c[1] for c in source_calls] == (
            ["/product/add", "/product/scheduler/allstatus"]
            if redirect_path.endswith("allstatus") or (code == 200 and action == "ingest")
            else [redirect_path])
        assert all(c[2] == "Token synthetic-redirect-secret" for c in source_calls)
        assert "synthetic-redirect-secret" not in json.dumps(result)
        assert target_url not in json.dumps(result)
    finally:
        for server in (source, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", ["http://example.test/target", "https://example.test/target"])
def test_redirect_handler_rejects_downgrade_and_same_origin(code, target):
    import urllib.error
    import urllib.request
    opener = urllib.request.build_opener(runner.NoRedirect())
    request = urllib.request.Request("https://example.test/source",
                                     headers={"Authorization": "Token synthetic-secret"})
    # Exercise the standard handler dispatch with the custom redirect policy.
    with pytest.raises(urllib.error.HTTPError) as error:
        opener.error("http", request, io.BytesIO(), code, "Redirect", {"location": target})
    assert error.value.code == code
