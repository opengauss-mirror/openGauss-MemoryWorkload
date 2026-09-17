from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
import json
import os

from memory_bench_platform.integration import build_run_contract, run_json_script
from memory_bench_platform.trace_runtime import TraceBindings, TraceRuntime
from memory_bench_platform.trace_runtime.platform import (
    build_trace_environment,
    build_trace_runtime_config,
)
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
