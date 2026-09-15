from __future__ import annotations

import http.client
import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from memory_bench_platform.protocol import TraceChannelConfig, TraceRuntimeConfig
from memory_bench_platform.trace_runtime import TraceRuntime


class _ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        size = int(self.headers.get("content-length", "0"))
        request_body = self.rfile.read(size)
        payload = json.loads(request_body)
        if self.path == "/v1/chat/completions":
            response = {
                "id": "chat-1",
                "choices": [{"message": {"content": "captured"}}],
                "access_token": "response-token-must-not-be-recorded",
            }
        else:
            response = {
                "object": "list",
                "data": [
                    {"index": index, "embedding": [float(len(text)), 1.0]}
                    for index, text in enumerate(payload["input"])
                ],
            }
        response_body = json.dumps(response, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format, *args):
        return


def _post(url: str, payload: dict, *, request_id: str) -> tuple[int, bytes]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": "Bearer must-not-be-recorded",
            "x-request-id": request_id,
            "x-trace-session-id": "session-1",
        },
    )
    with urlopen(request, timeout=5) as response:
        return response.status, response.read()


def test_chat_and_embedding_capture_then_replay(tmp_path: Path):
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    provider_thread = Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    upstream = f"http://127.0.0.1:{provider.server_address[1]}"
    bundle_path = tmp_path / "bundle"
    channels = {
        "memory_extract": TraceChannelConfig(
            protocol="openai-chat-completions",
            upstream_base_url=upstream,
            match_mode="fingerprint",
            order_scope="fingerprint",
        ),
        "embedding": TraceChannelConfig(
            protocol="openai-embeddings",
            upstream_base_url=upstream,
            match_mode="strict",
            order_scope="fingerprint",
        ),
    }
    capture = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            output_path=str(bundle_path),
            channels=channels,
        )
    )

    try:
        with capture.activate() as bindings:
            chat_status, chat_body = _post(
                bindings.endpoints["memory_extract"] + "/v1/chat/completions",
                {"model": "chat", "messages": [{"role": "user", "content": "hello"}]},
                request_id="request-chat",
            )
            embedding_status, embedding_body = _post(
                bindings.endpoints["embedding"]
                + "/v1/embeddings?api_key=query-token-must-not-be-recorded",
                {"model": "embed", "input": ["hello", "world"]},
                request_id="request-embedding",
            )
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)

    assert chat_status == embedding_status == 200
    assert capture.verify_and_collect().valid is True

    replay = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="replay-zero-delay",
            profile_id="openai-compatible@1",
            bundle_path=str(bundle_path),
            channels={
                name: config.model_copy(update={"upstream_base_url": None})
                for name, config in channels.items()
            },
        )
    )
    with replay.activate() as bindings:
        replay_chat_status, replay_chat_body = _post(
            bindings.endpoints["memory_extract"] + "/v1/chat/completions",
            {"model": "chat", "messages": [{"role": "user", "content": "hello"}]},
            request_id="replay-chat",
        )
        replay_embedding_status, replay_embedding_body = _post(
            bindings.endpoints["embedding"]
            + "/v1/embeddings?api_key=different-query-token",
            {"model": "embed", "input": ["hello", "world"]},
            request_id="replay-embedding",
        )

    summary = replay.verify_and_collect()
    assert replay_chat_status == replay_embedding_status == 200
    assert json.loads(chat_body)["choices"] == json.loads(replay_chat_body)["choices"]
    assert json.loads(replay_chat_body)["access_token"] == "[REDACTED]"
    assert replay_embedding_body == embedding_body
    assert summary.valid is True
    assert summary.channels["memory_extract"].matched == 1
    assert summary.channels["embedding"].remaining == 0

    records_text = (bundle_path / "memory_extract" / "records.jsonl").read_text(encoding="utf-8")
    assert "must-not-be-recorded" not in records_text
    assert "response-token-must-not-be-recorded" not in records_text
    embedding_records = (bundle_path / "embedding" / "records.jsonl").read_text(
        encoding="utf-8"
    )
    assert "query-token-must-not-be-recorded" not in embedding_records


def test_replay_can_derive_channels_from_bundle(tmp_path: Path):
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    provider_thread = Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    bundle_path = tmp_path / "bundle"
    capture = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            output_path=str(bundle_path),
            channels={
                "embedding": TraceChannelConfig(
                    protocol="openai-embeddings",
                    upstream_base_url=f"http://127.0.0.1:{provider.server_address[1]}",
                    match_mode="strict",
                    order_scope="fingerprint",
                )
            },
        )
    )
    try:
        with capture.activate() as bindings:
            _post(
                bindings.endpoints["embedding"] + "/v1/embeddings",
                {"model": "embed", "input": ["hello"]},
                request_id="capture-embedding",
            )
        capture.verify_and_collect()
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)

    replay = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="replay-zero-delay",
            profile_id="openai-compatible@1",
            bundle_path=str(bundle_path),
            channels={},
        )
    )
    with replay.activate() as bindings:
        status, _body = _post(
            bindings.endpoints["embedding"] + "/v1/embeddings",
            {"model": "embed", "input": ["hello"]},
            request_id="replay-embedding",
        )

    assert status == 200
    assert replay.verify_and_collect().valid is True


def test_external_runtime_verifies_managed_server_identity_and_counters(tmp_path: Path):
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    provider_thread = Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    bundle_path = tmp_path / "bundle"
    channel = TraceChannelConfig(
        protocol="openai-embeddings",
        upstream_base_url=f"http://127.0.0.1:{provider.server_address[1]}",
        match_mode="strict",
        order_scope="fingerprint",
    )
    capture = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            output_path=str(bundle_path),
            channels={"embedding": channel},
        )
    )
    try:
        with capture.activate() as bindings:
            _post(
                bindings.endpoints["embedding"] + "/v1/embeddings",
                {"model": "embed", "input": ["hello"]},
                request_id="capture-embedding",
            )
        capture.verify_and_collect()
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)

    managed = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="replay-zero-delay",
            profile_id="openai-compatible@1",
            bundle_path=str(bundle_path),
            channels={},
        )
    )
    managed.start()
    managed_endpoint = managed.bindings().endpoints["embedding"]
    external = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="replay-zero-delay",
            deployment="external",
            profile_id="openai-compatible@1",
            bundle_path=str(bundle_path),
            channels={
                "embedding": channel.model_copy(
                    update={"upstream_base_url": None, "listen_url": managed_endpoint + "/v1"}
                )
            },
        )
    )
    try:
        external.start()
        status, _body = _post(
            external.bindings().endpoints["embedding"] + "/embeddings",
            {"model": "embed", "input": ["hello"]},
            request_id="external-replay",
        )
        external.stop()
        summary = external.verify_and_collect()
    finally:
        managed.stop()

    assert status == 200
    assert summary.valid is True
    assert summary.channels["embedding"].matched == 1


def test_mock_fixed_embedding_is_counted_without_bundle():
    runtime = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="mock-fixed",
            profile_id="openai-compatible@1",
            channels={
                "embedding": TraceChannelConfig(
                    protocol="openai-embeddings",
                    match_mode="strict",
                    order_scope="fingerprint",
                )
            },
        )
    )
    with runtime.activate() as bindings:
        status, body = _post(
            bindings.endpoints["embedding"] + "/v1/embeddings",
            {"model": "embed", "input": ["one", "two"]},
            request_id="mock-1",
        )

    assert status == 200
    assert len(json.loads(body)["data"]) == 2
    summary = runtime.verify_and_collect()
    assert summary.valid is True
    assert summary.channels["embedding"].matched == 1


def test_trace_runtime_rejects_oversized_request_body():
    runtime = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="mock-fixed",
            profile_id="openai-compatible@1",
            channels={
                "embedding": TraceChannelConfig(
                    protocol="openai-embeddings",
                    match_mode="strict",
                    order_scope="fingerprint",
                    max_body_bytes=8,
                )
            },
        )
    )
    with runtime.activate() as bindings:
        request = Request(
            bindings.endpoints["embedding"] + "/v1/embeddings",
            data=b'{"input":["too large"]}',
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            urlopen(request, timeout=5)
        except HTTPError as exc:
            assert exc.code == 413
            assert json.loads(exc.read())["error"]["type"] == "trace_request_too_large"
        else:
            raise AssertionError("oversized request was accepted")


def test_trace_runtime_accepts_chunked_request_body():
    runtime = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="mock-fixed",
            profile_id="openai-compatible@1",
            channels={
                "embedding": TraceChannelConfig(
                    protocol="openai-embeddings",
                    match_mode="strict",
                    order_scope="fingerprint",
                )
            },
        )
    )
    with runtime.activate() as bindings:
        endpoint = bindings.endpoints["embedding"]
        connection = http.client.HTTPConnection(
            endpoint.removeprefix("http://"), timeout=5
        )
        connection.request(
            "POST",
            "/v1/embeddings",
            body=iter([b'{"input":["one"]}']),
            headers={"content-type": "application/json"},
            encode_chunked=True,
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

    assert response.status == 200
    assert len(payload["data"]) == 1


def test_capture_returns_502_and_invalidates_run_when_upstream_is_unavailable(
    tmp_path: Path,
):
    reserved = socket.socket()
    reserved.bind(("127.0.0.1", 0))
    unavailable_port = reserved.getsockname()[1]
    reserved.close()
    runtime = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            output_path=str(tmp_path / "bundle"),
            channels={
                "embedding": TraceChannelConfig(
                    protocol="openai-embeddings",
                    upstream_base_url=f"http://127.0.0.1:{unavailable_port}",
                    match_mode="strict",
                    order_scope="fingerprint",
                    request_timeout_seconds=1,
                )
            },
        )
    )
    with runtime.activate() as bindings:
        request = Request(
            bindings.endpoints["embedding"] + "/v1/embeddings",
            data=b'{"input":["one"]}',
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            urlopen(request, timeout=5)
        except HTTPError as exc:
            assert exc.code == 502
            assert json.loads(exc.read())["error"]["type"] == "trace_upstream_error"
        else:
            raise AssertionError("unavailable upstream did not return 502")

    summary = runtime.verify_and_collect()
    assert summary.valid is False
    assert summary.channels["embedding"].errors == 1


def test_capture_does_not_record_success_when_client_disconnects(tmp_path: Path):
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    provider_thread = Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    runtime = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            output_path=str(tmp_path / "bundle"),
            channels={
                "embedding": TraceChannelConfig(
                    protocol="openai-embeddings",
                    upstream_base_url=f"http://127.0.0.1:{provider.server_address[1]}",
                    match_mode="strict",
                    order_scope="fingerprint",
                )
            },
        )
    )

    class DisconnectedHandler:
        path = "/v1/embeddings"

        def _write_response(self, status, headers, body):
            raise BrokenPipeError("client disconnected")

    try:
        with pytest.raises(BrokenPipeError):
            runtime._capture(
                DisconnectedHandler(),
                "embedding",
                {"content-type": "application/json", "x-request-id": "disconnect"},
                b'{"input":["one"]}',
            )
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)

    summary = runtime.verify_and_collect()
    assert summary.valid is False
    assert summary.channels["embedding"].loaded == 0
    assert summary.channels["embedding"].errors == 1


def test_replay_response_failure_updates_error_and_active_counters(tmp_path: Path):
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    provider_thread = Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    bundle_path = tmp_path / "bundle"
    channel = TraceChannelConfig(
        protocol="openai-embeddings",
        upstream_base_url=f"http://127.0.0.1:{provider.server_address[1]}",
        match_mode="strict",
        order_scope="fingerprint",
    )
    capture = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            output_path=str(bundle_path),
            channels={"embedding": channel},
        )
    )
    try:
        with capture.activate() as bindings:
            _post(
                bindings.endpoints["embedding"] + "/v1/embeddings",
                {"model": "embed", "input": ["one"]},
                request_id="capture",
            )
        capture.verify_and_collect()
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)

    replay = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="replay-zero-delay",
            profile_id="openai-compatible@1",
            bundle_path=str(bundle_path),
            channels={
                "embedding": channel.model_copy(update={"upstream_base_url": None})
            },
        )
    )

    class DisconnectedHandler:
        path = "/v1/embeddings"

        def _write_response(self, status, headers, body):
            raise BrokenPipeError("client disconnected")

    with pytest.raises(BrokenPipeError):
        replay._replay(
            DisconnectedHandler(),
            "embedding",
            {"content-type": "application/json", "x-request-id": "replay"},
            b'{"model":"embed","input":["one"]}',
        )

    snapshot = replay.verify_and_collect().channels["embedding"]
    assert snapshot.matched == 1
    assert snapshot.errors == 1
    assert snapshot.active == 0


def test_trace_runtime_stop_waits_for_active_capture_request(tmp_path: Path):
    entered = Event()
    release = Event()

    class BlockingProvider(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers["content-length"])
            self.rfile.read(size)
            entered.set()
            release.wait(timeout=5)
            body = b'{"data":[{"embedding":[1.0]}]}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    provider = ThreadingHTTPServer(("127.0.0.1", 0), BlockingProvider)
    provider_thread = Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    runtime = TraceRuntime.prepare(
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            output_path=str(tmp_path / "bundle"),
            drain_timeout_seconds=5,
            channels={
                "embedding": TraceChannelConfig(
                    protocol="openai-embeddings",
                    upstream_base_url=f"http://127.0.0.1:{provider.server_address[1]}",
                    match_mode="strict",
                    order_scope="fingerprint",
                )
            },
        )
    )
    runtime.start()
    endpoint = runtime.bindings().endpoints["embedding"] + "/v1/embeddings"
    request_thread = Thread(
        target=_post,
        args=(endpoint, {"model": "embed", "input": ["one"]}),
        kwargs={"request_id": "capture"},
    )
    request_thread.start()
    assert entered.wait(timeout=5)

    stop_thread = Thread(target=runtime.stop)
    stop_thread.start()
    time.sleep(0.1)
    assert stop_thread.is_alive()
    release.set()
    request_thread.join(timeout=5)
    stop_thread.join(timeout=5)
    provider.shutdown()
    provider.server_close()
    provider_thread.join(timeout=5)

    assert not request_thread.is_alive()
    assert not stop_thread.is_alive()
    assert runtime.verify_and_collect().valid is True
