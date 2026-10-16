from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
import json
import os
from urllib.request import Request, urlopen

from memory_bench_platform.integration import build_run_contract, run_json_script
from memory_bench_platform.trace_runtime import TraceBindings, TraceRuntime
from memory_bench_platform.trace_runtime.platform import (
    build_trace_environment,
    build_trace_runtime_config,
)
from memory_bench_platform.cli import build_parser
from skills.agents.openclaw.scripts.run_task import build_openclaw_http_request


def test_run_json_script_merges_trace_environment_without_global_mutation(tmp_path: Path):
    script = tmp_path / "runner.py"
    script.write_text(
        "import json, os; print(json.dumps({'trace': os.environ.get('TRACE_AGENT_CHAT_BASE_URL')}))",
        encoding="utf-8",
    )
    result = run_json_script(
        script,
        environment={"TRACE_AGENT_CHAT_BASE_URL": "http://trace.local"},
    )
    assert result == {"trace": "http://trace.local"}
    assert "TRACE_AGENT_CHAT_BASE_URL" not in os.environ


def test_openclaw_http_gateway_is_routed_through_trace_endpoint(monkeypatch):
    contract = build_run_contract("locomo", "openclaw")
    dependency = contract["model_dependencies"]["agent_chat"]
    assert dependency["base_url_env"] == "OPENCLAW_GATEWAY_URL"

    environment = build_trace_environment(
        contract,
        TraceBindings(
            endpoints={"agent_chat": "http://127.0.0.1:18087"},
            environment={},
        ),
        run_id="trace-run",
    )
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", environment["OPENCLAW_GATEWAY_URL"])
    url, _headers, _body = build_openclaw_http_request(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"agent_id": "main"},
        }
    )

    assert url == "http://127.0.0.1:18087/v1/responses"


def test_openclaw_http_runner_traffic_is_captured_through_injected_gateway(
    tmp_path: Path,
):
    class GatewayHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("content-length", "0"))
            self.rfile.read(size)
            body = json.dumps(
                {
                    "id": "response-1",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "captured"}],
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    gateway_thread = Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    contract = build_run_contract("locomo", "openclaw")
    trace_config = build_trace_runtime_config(
        SimpleNamespace(
            trace_mode="capture",
            trace_deployment="managed",
            trace_channel=["agent_chat=openai-responses:ordered:session"],
            trace_endpoint=[],
            trace_upstream=[],
            trace_redact_json_pointer=[],
            trace_bundle=None,
            trace_output=str(tmp_path / "bundle"),
            trace_profile="openai-compatible@1",
            trace_copies=1,
            trace_delay_scale=1.0,
        ),
        contract,
        environ={
            "OPENCLAW_GATEWAY_URL": f"http://127.0.0.1:{gateway.server_address[1]}"
        },
    )
    assert trace_config is not None
    runtime = TraceRuntime.prepare(trace_config)
    runner = (
        Path(__file__).resolve().parents[1]
        / "skills/agents/openclaw/scripts/run_task.py"
    )

    try:
        with runtime.activate() as bindings:
            result = run_json_script(
                runner,
                stdin_payload={
                    "task_id": "task-1",
                    "messages": [{"role": "user", "content": "hello"}],
                    "metadata": {
                        "agent_id": "main",
                        "session_id": "session-1",
                        "request_id": "request-1",
                    },
                },
                environment={
                    **build_trace_environment(
                        contract, bindings, run_id="trace-run"
                    ),
                    "OPENCLAW_TRANSPORT": "http",
                },
            )
        summary = runtime.verify_and_collect()
    finally:
        gateway.shutdown()
        gateway.server_close()
        gateway_thread.join(timeout=5)

    assert result["turns"] == [{"text": "captured"}]
    assert summary.valid is True
    assert summary.channels["agent_chat"].loaded == 1


def test_mem0_auto_discovered_channel_captures_and_replays_without_session_id(tmp_path: Path):
    class Mem0Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", "0")))
            body = (b'[{"id":"memory-1"}]' if self.path == "/memories"
                    else b'[{"id":"memory-1","memory":"hello"}]')
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Mem0Handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    contract = build_run_contract("production-mem0-sample", "generic-cli", "mem0", "backend_direct")
    bundle = tmp_path / "bundle"

    def config(mode: str):
        argv = ["run", "--benchmark", "production-mem0-sample", "--agent", "generic-cli",
                "--memory-backend", "mem0", "--trace-mode", mode]
        argv += (["--trace-output", str(bundle)] if mode == "capture"
                 else ["--trace-bundle", str(bundle)])
        return build_trace_runtime_config(build_parser().parse_args(argv), contract,
                                          environ={"MEM0_API_URL": f"http://127.0.0.1:{server.server_address[1]}"})

    requests = [("/memories", {"user_id": "mbp-user", "messages": [{"role": "user", "content": "hello"}],
                               "metadata": {"source_session_id": "write-only-session"}}),
                ("/search", {"query": "hello", "filters": {"user_id": "mbp-user"}})]

    def send(endpoint: str):
        responses = []
        for path, body in requests:
            payload = json.dumps(body).encode()
            request = Request(endpoint + path, data=payload,
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request) as response:
                responses.append(json.load(response))
        return responses

    try:
        capture_config = config("capture")
        assert capture_config is not None
        capture = TraceRuntime.prepare(capture_config)
        with capture.activate() as bindings:
            captured = send(bindings.endpoints["mem0_http"])
        assert capture.verify_and_collect().valid

        replay_config = config("replay-zero-delay")
        assert replay_config is not None
        replay = TraceRuntime.prepare(replay_config)
        with replay.activate() as bindings:
            replayed = send(bindings.endpoints["mem0_http"])
        summary = replay.verify_and_collect()
        assert replayed == captured
        assert summary.valid
        assert summary.channels["mem0_http"].matched == 2
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
