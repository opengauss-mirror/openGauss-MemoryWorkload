from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

import pytest

from memory_bench_platform.loader import load_all_skills
from memory_bench_platform.protocol import TraceChannelConfig, TraceRuntimeConfig
from memory_bench_platform.trace_runtime import TraceRuntime
from memory_bench_platform.trace_runtime.bundle import load_bundle
from memory_bench_platform.trace_runtime.openclaw_importer import import_openclaw_session
from memory_bench_platform.trace_runtime.runtime import _split_sse_frames, _stream_frame


SSE_FRAMES = [
    b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp-1"}}\n\n',
    b'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"id":"call-1","type":"function_call","name":"lookup","arguments":""}}\n\n',
    b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","item_id":"call-1","output_index":0,"delta":"{\\"city\\":"}\n\n',
    b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","item_id":"call-1","output_index":0,"delta":"\\"Paris\\"}"}\n\n',
    b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp-1","status":"completed"}}\n\n',
]


class _ResponsesProvider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        size = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(size))
        if payload.get("stream"):
            body = b"".join(SSE_FRAMES)
            content_type = "text/event-stream; charset=utf-8"
        else:
            body = json.dumps(
                {
                    "id": "resp-json",
                    "object": "response",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": payload["input"]}],
                        }
                    ],
                },
                separators=(",", ":"),
            ).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def _post(endpoint: str, payload: dict, session_id: str, request_id: str) -> bytes:
    request = Request(
        endpoint + "/v1/responses",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-request-id": request_id,
            "x-trace-session-id": session_id,
        },
    )
    with urlopen(request, timeout=5) as response:
        return response.read()


def test_responses_non_stream_and_sse_capture_replay_preserve_sequence(tmp_path: Path):
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ResponsesProvider)
    provider_thread = Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    bundle_path = tmp_path / "responses-bundle"
    channel = TraceChannelConfig(
        protocol="openai-responses",
        upstream_base_url=f"http://127.0.0.1:{provider.server_address[1]}",
        match_mode="ordered",
        order_scope="session",
    )
    capture = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            output_path=str(bundle_path),
            channels={"agent_chat": channel},
        )
    )
    try:
        with capture.activate() as bindings:
            captured_json = _post(
                bindings.endpoints["agent_chat"],
                {"model": "test", "input": "hello", "stream": False},
                "session-json",
                "capture-json",
            )
            captured_sse = _post(
                bindings.endpoints["agent_chat"],
                {"model": "test", "input": "tool", "stream": True},
                "session-sse",
                "capture-sse",
            )
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
    assert capture.verify_and_collect().valid is True

    loaded = load_bundle(bundle_path)
    stream_record = loaded.records["agent_chat"][1]
    assert [frame.body_bytes() for frame in stream_record.response.stream_frames] == SSE_FRAMES
    assert [frame.data["type"] for frame in stream_record.response.stream_frames] == [
        "response.created",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
        "response.completed",
    ]

    replay = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="replay-zero-delay",
            profile_id="openai-compatible@1",
            bundle_path=str(bundle_path),
            channels={"agent_chat": channel.model_copy(update={"upstream_base_url": None})},
        )
    )
    with replay.activate() as bindings:
        replayed_sse = _post(
            bindings.endpoints["agent_chat"],
            {"model": "changed", "input": "tool", "stream": True},
            "session-sse",
            "replay-sse",
        )
        replayed_json = _post(
            bindings.endpoints["agent_chat"],
            {"model": "changed", "input": "hello", "stream": False},
            "session-json",
            "replay-json",
        )
    assert replayed_sse == captured_sse
    assert replayed_json == captured_json
    assert replay.verify_and_collect().valid is True


def test_session_ordered_replay_isolated_under_concurrency(tmp_path: Path):
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ResponsesProvider)
    provider_thread = Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    bundle_path = tmp_path / "session-bundle"
    channel = TraceChannelConfig(
        protocol="openai-responses",
        upstream_base_url=f"http://127.0.0.1:{provider.server_address[1]}",
        match_mode="ordered",
        order_scope="session",
    )
    capture = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            output_path=str(bundle_path),
            channels={"agent_chat": channel},
        )
    )
    try:
        with capture.activate() as bindings:
            for session, value in (("a", "a1"), ("b", "b1"), ("a", "a2"), ("b", "b2")):
                _post(
                    bindings.endpoints["agent_chat"],
                    {"model": "test", "input": value, "stream": False},
                    session,
                    f"capture-{value}",
                )
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
    capture.verify_and_collect()

    replay = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="replay-zero-delay",
            profile_id="openai-compatible@1",
            bundle_path=str(bundle_path),
            channels={"agent_chat": channel.model_copy(update={"upstream_base_url": None})},
        )
    )
    with replay.activate() as bindings:
        endpoint = bindings.endpoints["agent_chat"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            session_b = pool.submit(_post, endpoint, {"input": "ignored"}, "b", "replay-b1")
            session_a = pool.submit(_post, endpoint, {"input": "ignored"}, "a", "replay-a1")
            first = [json.loads(session_a.result()), json.loads(session_b.result())]
        second = [
            json.loads(_post(endpoint, {"input": "ignored"}, "a", "replay-a2")),
            json.loads(_post(endpoint, {"input": "ignored"}, "b", "replay-b2")),
        ]
    assert [item["output"][0]["content"][0]["text"] for item in first] == ["a1", "b1"]
    assert [item["output"][0]["content"][0]["text"] for item in second] == ["a2", "b2"]
    assert replay.verify_and_collect().valid is True


def test_openclaw_session_importer_builds_responses_bundle(tmp_path: Path):
    session_path = tmp_path / "session.jsonl"
    session_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "id": "session-1", "timestamp": "2026-01-01T00:00:00Z"}),
                json.dumps({"type": "message", "message": {"role": "user", "content": "Find weather"}}),
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "model": "gpt-test",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "id": "call-1",
                                    "name": "weather",
                                    "arguments": {"city": "Paris"},
                                    "partialArgs": "{\"city\":\"Paris\"}",
                                }
                            ],
                            "stopReason": "toolUse",
                        },
                    }
                ),
                json.dumps({"type": "message", "message": {"role": "toolResult", "toolCallId": "call-1", "content": [{"type": "text", "text": "sunny"}]}}),
                json.dumps({"type": "message", "message": {"role": "assistant", "model": "gpt-test", "content": [{"type": "text", "text": "It is sunny."}], "stopReason": "stop"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bundle_path = tmp_path / "imported"
    manifest = import_openclaw_session(session_path, bundle_path, case_id="case-1")
    loaded = load_bundle(bundle_path)
    records = loaded.records["agent_chat"]

    assert manifest.channels["agent_chat"].protocol == "openai-responses"
    assert manifest.channels["agent_chat"].order_scope == "session"
    assert len(records) == 2
    assert all(record.scope.session_id == "session-1" for record in records)
    event_types = [
        frame.data["type"]
        for record in records
        for frame in record.response.stream_frames
        if isinstance(frame.data, dict)
    ]
    assert "response.function_call_arguments.delta" in event_types
    assert "response.output_text.delta" in event_types


def test_phase3_trace_skills_are_available():
    loaded = load_all_skills(Path(__file__).resolve().parents[1] / "skills")
    traces = {item.id: item for item in loaded["traces"]}
    assert traces["openai-responses"].capabilities["streaming"] is True
    assert traces["openclaw-session"].entry.importer == "scripts/import.py"


def test_sse_frame_redaction_rewrites_raw_frame():
    frame = _stream_frame(
        b': comment\nid: 42\nevent: response.completed\ndata: {"type":"response.completed","access_token":"secret"}\n\n',
        1,
    )

    assert frame.data["access_token"] == "[REDACTED]"
    assert b"secret" not in frame.body_bytes()
    assert b": comment\n" in frame.body_bytes()
    assert b"id: 42\n" in frame.body_bytes()


def test_plain_text_sse_frame_is_only_rewritten_when_secret_is_present():
    ordinary = _stream_frame(b"data: ordinary text\n\n", 1)
    secret = _stream_frame(b"data: Bearer abc123\n\n", 1)

    assert ordinary.body_bytes() == b"data: ordinary text\n\n"
    assert secret.body_bytes() == b"data: Bearer [REDACTED]\n\n"


def test_sse_split_accepts_mixed_line_endings():
    raw = b"data: one\r\n\ndata: two\n\r\n"

    assert _split_sse_frames(raw) == [b"data: one\r\n\n", b"data: two\n\r\n"]


def test_openclaw_session_importer_rejects_malformed_json(tmp_path: Path):
    session_path = tmp_path / "session.jsonl"
    session_path.write_text('{"type":"session","id":"s"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        import_openclaw_session(session_path, tmp_path / "bundle")
