from __future__ import annotations

import base64
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from threading import BoundedSemaphore, Condition, Lock, Thread
import time
from types import TracebackType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
import uuid

from memory_bench_platform.protocol import (
    TraceChannelConfig,
    TraceCounterSnapshot,
    TraceRuntimeConfig,
    TraceRuntimeSummary,
)

from .bundle import BundleWriter, LoadedBundle, load_bundle
from .matching import ReplayMatcher, matching_key
from .protocol import (
    ProviderRequest,
    ProviderResponse,
    TraceMatching,
    TraceRecord,
    TraceScope,
    TraceStreamFrame,
    TraceTiming,
)
from .redaction import redact_bytes, redact_headers, redact_json


HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass(frozen=True)
class TraceBindings:
    endpoints: dict[str, str]
    environment: dict[str, str]


class _CaptureCounters:
    def __init__(self):
        self.matched = 0
        self.errors = 0
        self.active = 0
        self.peak_active = 0
        self._lock = Lock()

    def begin(self) -> None:
        with self._lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)

    def finish(self, *, error: bool) -> None:
        with self._lock:
            self.active -= 1
            if error:
                self.errors += 1
            else:
                self.matched += 1

    def snapshot(self) -> TraceCounterSnapshot:
        with self._lock:
            return TraceCounterSnapshot(
                loaded=self.matched,
                matched=self.matched,
                remaining=0,
                errors=self.errors,
                active=self.active,
                peak_active=self.peak_active,
            )


def _parse_body(raw: bytes, content_type: str | None) -> Any:
    if not raw:
        return None
    if content_type and "json" in content_type.lower():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scope_from_headers(
    headers: dict[str, str], request_id: str, body: Any = None
) -> TraceScope:
    metadata = body.get("metadata", {}) if isinstance(body, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}

    def value(header: str, field: str) -> str | None:
        return headers.get(header) or (
            str(metadata[field]) if metadata.get(field) is not None else None
        )

    return TraceScope(
        run_id=value("x-trace-run-id", "run_id"),
        case_id=value("x-trace-case-id", "case_id"),
        step_id=value("x-trace-step-id", "step_id"),
        user_id=value("x-trace-user-id", "user_id"),
        session_id=value("x-trace-session-id", "session_id"),
        request_id=request_id,
    )


def _upstream_url(base_url: str, request_path: str) -> str:
    base = urlsplit(base_url)
    path = request_path
    if base.path and base.path != "/" and not request_path.startswith(base.path.rstrip("/") + "/"):
        path = base.path.rstrip("/") + "/" + request_path.lstrip("/")
    return urlunsplit((base.scheme, base.netloc, path, "", ""))


def _response_headers(headers: dict[str, str], body: bytes) -> dict[str, str]:
    result = {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length"
    }
    result["content-length"] = str(len(body))
    return result


def _is_event_stream(headers: dict[str, str]) -> bool:
    return "text/event-stream" in headers.get("content-type", "").lower()


def _split_sse_frames(raw: bytes) -> list[bytes]:
    frames: list[bytes] = []
    start = 0
    for match in re.finditer(rb"(?:\r?\n){2}", raw):
        frames.append(raw[start : match.end()])
        start = match.end()
    if start < len(raw):
        frames.append(raw[start:])
    return frames


def _read_request_body(handler: BaseHTTPRequestHandler, max_bytes: int) -> bytes:
    transfer_encoding = handler.headers.get("transfer-encoding", "").lower()
    if transfer_encoding and "chunked" not in transfer_encoding:
        raise ValueError("unsupported transfer-encoding")
    if "chunked" in transfer_encoding:
        chunks = bytearray()
        total = 0
        while True:
            line = handler.rfile.readline(64 * 1024)
            if not line or not line.endswith((b"\r\n", b"\n")):
                raise ValueError("malformed chunked request")
            try:
                size = int(line.strip().split(b";", 1)[0], 16)
            except ValueError as exc:
                raise ValueError("malformed chunk size") from exc
            if size == 0:
                trailer_bytes = 0
                while True:
                    trailer = handler.rfile.readline(64 * 1024)
                    trailer_bytes += len(trailer)
                    if trailer_bytes > 64 * 1024:
                        raise ValueError("chunked trailers are too large")
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                return bytes(chunks)
            if size < 0 or total + size > max_bytes:
                raise OverflowError("request body exceeds trace limit")
            chunk = handler.rfile.read(size)
            terminator = handler.rfile.read(2)
            if len(chunk) != size or terminator != b"\r\n":
                raise ValueError("malformed chunked request")
            chunks.extend(chunk)
            total += size
    content_length = handler.headers.get("content-length")
    if content_length is None:
        return b""
    try:
        size = int(content_length)
    except ValueError as exc:
        raise ValueError("invalid content-length") from exc
    if size < 0:
        raise ValueError("invalid content-length")
    if size > max_bytes:
        raise OverflowError("request body exceeds trace limit")
    body = handler.rfile.read(size)
    if len(body) != size:
        raise ValueError("incomplete request body")
    return body


def _stream_frame(
    raw: bytes, ordinal: int, redact_json_pointers: tuple[str, ...] = ()
) -> TraceStreamFrame:
    event = None
    data_lines: list[bytes] = []
    for line in raw.replace(b"\r\n", b"\n").splitlines():
        if line.startswith(b"event:"):
            event = line[6:].strip().decode("utf-8", errors="replace") or None
        elif line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
    parsed_data: Any = None
    original_data: Any = None
    if data_lines:
        data_raw = b"\n".join(data_lines)
        if data_raw == b"[DONE]":
            parsed_data = "[DONE]"
            original_data = "[DONE]"
        else:
            try:
                original_data = json.loads(data_raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                original_data = data_raw.decode("utf-8", errors="replace")
            parsed_data = redact_json(original_data, redact_json_pointers)
    recorded_raw = raw
    if data_lines and parsed_data not in (None, "[DONE]"):
        if parsed_data != original_data:
            replacement = (
                parsed_data.encode("utf-8")
                if isinstance(original_data, str)
                else json.dumps(
                    parsed_data, ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            rewritten: list[bytes] = []
            replaced = False
            for line in raw.splitlines(keepends=True):
                if line.startswith(b"data:"):
                    if replaced:
                        continue
                    newline = b"\r\n" if line.endswith(b"\r\n") else b"\n"
                    rewritten.append(b"data: " + replacement + newline)
                    replaced = True
                else:
                    rewritten.append(line)
            recorded_raw = b"".join(rewritten)
    return TraceStreamFrame(
        ordinal=ordinal,
        raw_base64=base64.b64encode(recorded_raw).decode("ascii"),
        event=event,
        data=parsed_data,
    )


class _TraceRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    runtime: "TraceRuntime"
    channel_name: str

    def setup(self) -> None:
        super().setup()
        timeout = self.runtime._channel_configs[
            self.channel_name
        ].idle_connection_timeout_seconds
        self.connection.settimeout(timeout)

    def do_GET(self) -> None:
        if self.path == "/health":
            body = json.dumps(
                self.runtime._health_payload(self.channel_name),
                separators=(",", ":"),
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/counters":
            body = self.runtime._channel_snapshot(self.channel_name).model_dump_json().encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_error(404, "trace_endpoint_not_found")

    def do_POST(self) -> None:
        headers = {key.lower(): value for key, value in self.headers.items()}
        max_bytes = self.runtime._channel_configs[self.channel_name].max_body_bytes
        try:
            raw_body = _read_request_body(self, max_bytes)
        except OverflowError:
            self.close_connection = True
            self.runtime._record_request_error(self.channel_name)
            self._send_error(413, "trace_request_too_large")
            return
        except ValueError:
            self.close_connection = True
            self.runtime._record_request_error(self.channel_name)
            self._send_error(400, "trace_invalid_request_body")
            return
        if self.runtime.config.mode == "capture":
            self.runtime._capture(self, self.channel_name, headers, raw_body)
        else:
            self.runtime._replay(self, self.channel_name, headers, raw_body)

    def _write_response(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for key, value in _response_headers(headers, body).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _write_stream(
        self, status: int, headers: dict[str, str], frames: list[bytes]
    ) -> None:
        body_size = sum(len(frame) for frame in frames)
        self.send_response(status)
        response_headers = {
            key: value
            for key, value in headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length"
        }
        response_headers["content-length"] = str(body_size)
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.end_headers()
        for frame in frames:
            self.wfile.write(frame)
            self.wfile.flush()

    def _relay_upstream_stream(
        self,
        status: int,
        headers: dict[str, str],
        response: Any,
        max_bytes: int,
    ) -> bytes:
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length":
                self.send_header(key, value)
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        chunks = bytearray()
        total = 0
        while True:
            line = response.readline(max_bytes - total + 1)
            if not line:
                break
            if total + len(line) > max_bytes:
                raise OverflowError("upstream response exceeds trace limit")
            chunks.extend(line)
            total += len(line)
            self.wfile.write(line)
            self.wfile.flush()
        return bytes(chunks)

    def _send_error(self, status: int, error: str) -> None:
        body = json.dumps({"error": {"type": error}}, separators=(",", ":")).encode()
        self._write_response(status, {"content-type": "application/json"}, body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    def __init__(self, *args: Any, concurrency: int, **kwargs: Any):
        self._slots = BoundedSemaphore(concurrency)
        self._drain = Condition()
        self._active_requests = 0
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        self._slots.acquire()
        with self._drain:
            self._active_requests += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._drain:
                self._active_requests -= 1
                self._drain.notify_all()
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._drain:
                self._active_requests -= 1
                self._drain.notify_all()
            self._slots.release()

    def wait_for_drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._drain:
            while self._active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._drain.wait(remaining)
        return True


class _Activation(AbstractContextManager[TraceBindings]):
    def __init__(self, runtime: "TraceRuntime"):
        self.runtime = runtime

    def __enter__(self) -> TraceBindings:
        self.runtime.start()
        return self.runtime.bindings()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.runtime.stop()
        except Exception as stop_error:
            if exc_value is not None:
                exc_value.add_note(f"trace runtime stop failed: {stop_error}")
                return False
            raise


class TraceRuntime:
    def __init__(self, config: TraceRuntimeConfig):
        if config.mode not in {"capture", "replay-zero-delay", "replay-with-delay", "mock-fixed"}:
            raise NotImplementedError(f"trace mode is not part of the Phase 1 MVP: {config.mode}")
        self.config = config
        self._bundle: LoadedBundle | None = None
        self._writer: BundleWriter | None = None
        self._channel_configs: dict[str, TraceChannelConfig] = {}
        self._matchers: dict[str, ReplayMatcher] = {}
        self._capture_counters: dict[str, _CaptureCounters] = {}
        self._servers: dict[str, _BoundedThreadingHTTPServer] = {}
        self._threads: dict[str, Thread] = {}
        self._endpoints: dict[str, str] = {}
        self._ordinals: dict[str, int] = {}
        self._ordinal_lock = Lock()
        self._finalized_manifest_id: str | None = None
        self._external_bundle_id: str | None = None
        self._mismatches: list[dict[str, Any]] = []
        self._mismatch_lock = Lock()
        self._prepare()

    @classmethod
    def prepare(cls, config: TraceRuntimeConfig) -> "TraceRuntime":
        return cls(config)

    def _prepare(self) -> None:
        if self.config.mode == "mock-fixed":
            self._channel_configs = dict(self.config.channels)
            self._capture_counters = {
                name: _CaptureCounters() for name in self.config.channels
            }
            return
        if self.config.deployment == "external" and self.config.mode == "capture":
            self._channel_configs = dict(self.config.channels)
            self._capture_counters = {
                name: _CaptureCounters() for name in self.config.channels
            }
            return
        if self.config.mode == "capture":
            self._channel_configs = dict(self.config.channels)
            channel_data = {
                name: {
                    "protocol": channel.protocol,
                    "match_mode": channel.match_mode,
                    "order_scope": channel.order_scope,
                }
                for name, channel in self.config.channels.items()
            }
            output_path = Path(self.config.output_path or "")
            self._writer = BundleWriter(
                output_path,
                bundle_id=output_path.name,
                profile_id=self.config.profile_id,
                channels=channel_data,
            )
            self._capture_counters = {
                name: _CaptureCounters() for name in self.config.channels
            }
            self._ordinals = {name: 0 for name in self.config.channels}
            return
        self._bundle = load_bundle(Path(self.config.bundle_path or ""))
        configured_channels = self.config.channels or {
            name: TraceChannelConfig(
                protocol=channel.protocol,
                match_mode=channel.match_mode,
                order_scope=channel.order_scope,
            )
            for name, channel in self._bundle.manifest.channels.items()
        }
        self._channel_configs = configured_channels
        for name, config in configured_channels.items():
            manifest_channel = self._bundle.manifest.channels.get(name)
            if manifest_channel is None:
                raise ValueError(f"trace bundle does not contain configured channel: {name}")
            if manifest_channel.protocol != config.protocol:
                raise ValueError(f"trace protocol mismatch for channel {name}")
            if self.config.deployment == "managed":
                self._matchers[name] = ReplayMatcher(
                    records=self._bundle.records[name],
                    match_mode=config.match_mode,
                    copies=self.config.copies,
                    order_scope=config.order_scope,
                )

    def activate(self) -> _Activation:
        return _Activation(self)

    def start(self) -> None:
        if self._servers or self._endpoints:
            raise RuntimeError("trace runtime is already started")
        if self.config.deployment == "external":
            self._start_external()
            return
        for name, config in self._channel_configs.items():
            handler = type(
                f"TraceRequestHandler_{name}",
                (_TraceRequestHandler,),
                {"runtime": self, "channel_name": name},
            )
            server_class = type(
                f"TraceHTTPServer_{name}",
                (_BoundedThreadingHTTPServer,),
                {"request_queue_size": config.backlog},
            )
            server = server_class(
                ("127.0.0.1", 0), handler, concurrency=config.concurrency
            )
            thread = Thread(target=server.serve_forever, name=f"trace-{name}", daemon=True)
            thread.start()
            self._servers[name] = server
            self._threads[name] = thread
            self._endpoints[name] = f"http://127.0.0.1:{server.server_address[1]}"

    def _start_external(self) -> None:
        for name, config in self._channel_configs.items():
            endpoint = config.listen_url
            if not endpoint:
                raise ValueError(f"external trace endpoint is missing for channel {name}")
            parts = urlsplit(endpoint)
            if parts.scheme not in {"http", "https"} or parts.username or parts.password:
                raise ValueError(f"invalid external trace endpoint for channel {name}")
            with urlopen(
                _control_url(endpoint, "/health"),
                timeout=self.config.startup_timeout_seconds,
            ) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
            expected_bundle = self._bundle.manifest.bundle_id if self._bundle else None
            if payload.get("status") != "ok":
                raise ValueError(f"external trace endpoint is unhealthy for channel {name}")
            if payload.get("channel") != name:
                raise ValueError(f"external trace channel identity mismatch for {name}")
            if payload.get("protocol") != config.protocol:
                raise ValueError(f"external trace protocol identity mismatch for {name}")
            if expected_bundle and payload.get("bundle_id") != expected_bundle:
                raise ValueError(f"external trace bundle identity mismatch for {name}")
            if payload.get("bundle_id"):
                self._external_bundle_id = str(payload["bundle_id"])
            self._endpoints[name] = endpoint.rstrip("/")

    def bindings(self) -> TraceBindings:
        if not self._endpoints:
            raise RuntimeError("trace runtime is not started")
        environment = {
            f"TRACE_{name.upper()}_BASE_URL": endpoint
            for name, endpoint in self._endpoints.items()
        }
        return TraceBindings(endpoints=dict(self._endpoints), environment=environment)

    def _next_ordinal(self, channel: str) -> int:
        with self._ordinal_lock:
            self._ordinals[channel] += 1
            return self._ordinals[channel]

    def _record_request_error(self, channel: str) -> None:
        if self.config.mode in {"capture", "mock-fixed"}:
            counters = self._capture_counters[channel]
            counters.begin()
            counters.finish(error=True)
        else:
            self._matchers[channel].record_error()

    def _capture(
        self,
        handler: _TraceRequestHandler,
        channel_name: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> None:
        config = self.config.channels[channel_name]
        counters = self._capture_counters[channel_name]
        counters.begin()
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        request_id = headers.get("x-request-id") or str(uuid.uuid4())
        response_status = 502
        response_headers: dict[str, str] = {"content-type": "application/json"}
        response_body = b'{"error":{"type":"trace_upstream_error"}}'
        streamed = False
        error = False
        try:
            forwarded_headers = {
                key: value
                for key, value in headers.items()
                if key not in HOP_BY_HOP_HEADERS and key not in {"host", "content-length"}
            }
            request = Request(
                _upstream_url(config.upstream_base_url or "", handler.path),
                data=raw_body,
                method="POST",
                headers=forwarded_headers,
            )
            try:
                with urlopen(request, timeout=config.request_timeout_seconds) as response:
                    response_status = response.status
                    response_headers = {
                        key.lower(): value for key, value in response.headers.items()
                    }
                    if _is_event_stream(response_headers):
                        response_body = handler._relay_upstream_stream(
                            response_status, response_headers, response, config.max_body_bytes
                        )
                        streamed = True
                    else:
                        response_body = response.read(config.max_body_bytes + 1)
                        if len(response_body) > config.max_body_bytes:
                            raise OverflowError("upstream response exceeds trace limit")
            except HTTPError as exc:
                response_status = exc.code
                response_headers = {
                    key.lower(): value for key, value in exc.headers.items()
                }
                if _is_event_stream(response_headers):
                    response_body = handler._relay_upstream_stream(
                        response_status, response_headers, exc, config.max_body_bytes
                    )
                    streamed = True
                else:
                    response_body = exc.read(config.max_body_bytes + 1)
                    if len(response_body) > config.max_body_bytes:
                        response_body = response_body[: config.max_body_bytes]
                        error = True
            except (URLError, TimeoutError, OSError):
                response_status = 502
                response_headers = {"content-type": "application/json"}
                response_body = b'{"error":{"type":"trace_upstream_error"}}'
                error = True
                handler._write_response(response_status, response_headers, response_body)
                return
            except OverflowError:
                response_status = 502
                response_headers = {"content-type": "application/json"}
                response_body = b'{"error":{"type":"trace_upstream_response_too_large"}}'
                error = True
                if not handler.close_connection:
                    handler._write_response(response_status, response_headers, response_body)
                return
            duration_ms = (time.monotonic() - started) * 1000
            request_body = _parse_body(raw_body, headers.get("content-type"))
            scope = _scope_from_headers(headers, request_id, request_body)
            provider_request = ProviderRequest(
                method="POST",
                path=redact_json(handler.path),
                headers=redact_headers(headers),
                body=redact_json(request_body, config.redact_json_pointers),
                raw_body_base64=None,
            )
            parsed_response = _parse_body(
                response_body, response_headers.get("content-type")
            )
            redacted_response = redact_json(
                parsed_response, config.redact_json_pointers
            )
            recorded_response_body = redact_bytes(response_body)
            stream_frames: list[TraceStreamFrame] = []
            if _is_event_stream(response_headers):
                stream_frames = [
                    _stream_frame(
                        frame, index, tuple(config.redact_json_pointers)
                    )
                    for index, frame in enumerate(_split_sse_frames(response_body), 1)
                ]
                recorded_response_body = b"".join(
                    frame.body_bytes() for frame in stream_frames
                )
                redacted_response = [frame.data for frame in stream_frames]
            elif redacted_response != parsed_response:
                recorded_response_body = (
                    redacted_response.encode("utf-8")
                    if isinstance(redacted_response, str)
                    else json.dumps(
                        redacted_response,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            elif parsed_response is not None and isinstance(parsed_response, (dict, list)):
                recorded_response_body = json.dumps(
                    redacted_response,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            mode = config.match_mode
            record = TraceRecord(
                trace_id=str(uuid.uuid4()),
                ordinal=self._next_ordinal(channel_name),
                channel=channel_name,
                protocol=config.protocol,
                scope=scope,
                request=provider_request,
                response=ProviderResponse(
                    status=response_status,
                    headers=redact_headers(response_headers),
                    body=redacted_response,
                    raw_body_base64=base64.b64encode(recorded_response_body).decode("ascii"),
                    stream_frames=stream_frames,
                ),
                timing=TraceTiming(
                    started_at=started_at,
                    upstream_duration_ms=duration_ms,
                ),
                matching=TraceMatching(
                    mode=mode,
                    key_version=f"{config.protocol}/1",
                    key=matching_key(provider_request, mode, scope, config.order_scope),
                ),
            )
            if streamed:
                pass
            else:
                handler._write_response(response_status, response_headers, response_body)
            if self._writer is None:
                raise RuntimeError("capture bundle writer is unavailable")
            self._writer.append(record)
        except Exception:
            error = True
            raise
        finally:
            counters.finish(error=error)
        return

    def _replay(
        self,
        handler: _TraceRequestHandler,
        channel_name: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> None:
        if self.config.mode == "mock-fixed":
            self._mock_fixed(handler, channel_name, headers, raw_body)
            return
        matcher = self._matchers[channel_name]
        request_id = headers.get("x-request-id") or str(uuid.uuid4())
        config = self._channel_configs[channel_name]
        request = ProviderRequest(
            method="POST",
            path=redact_json(handler.path),
            headers=redact_headers(headers),
            body=redact_json(
                _parse_body(raw_body, headers.get("content-type")),
                config.redact_json_pointers,
            ),
        )
        scope = _scope_from_headers(headers, request_id, _parse_body(raw_body, headers.get("content-type")))
        try:
            record = matcher.match(request, scope, hold_active=True)
        except Exception:
            matcher.record_error()
            matcher.finish_request()
            raise
        if record is None:
            with self._mismatch_lock:
                if len(self._mismatches) < 10_000:
                    self._mismatches.append(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "channel": channel_name,
                            "path": request.path,
                            "matching_key": matching_key(
                                request,
                                self._channel_configs[channel_name].match_mode,
                                scope,
                                self._channel_configs[channel_name].order_scope,
                            ),
                            "scope": scope.model_dump(mode="json", exclude_none=True),
                        }
                    )
            try:
                handler._send_error(409, "trace_request_mismatch")
            except Exception:
                matcher.record_error()
                raise
            finally:
                matcher.finish_request()
            return
        try:
            if self.config.mode == "replay-with-delay":
                time.sleep(record.timing.upstream_duration_ms * self.config.delay_scale / 1000)
            response_body = record.response.body_bytes()
            if record.response.stream_frames:
                handler._write_stream(
                    record.response.status,
                    record.response.headers,
                    [frame.body_bytes() for frame in record.response.stream_frames],
                )
            else:
                handler._write_response(record.response.status, record.response.headers, response_body)
        except Exception:
            matcher.record_error()
            raise
        finally:
            matcher.finish_request()

    def _mock_fixed(
        self,
        handler: _TraceRequestHandler,
        channel_name: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> None:
        counters = self._capture_counters[channel_name]
        counters.begin()
        error = False
        try:
            request_body = _parse_body(raw_body, headers.get("content-type"))
            protocol = self._channel_configs[channel_name].protocol
            if protocol == "openai-embeddings":
                inputs = request_body.get("input", []) if isinstance(request_body, dict) else []
                input_count = len(inputs) if isinstance(inputs, list) else 1
                payload = {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": index, "embedding": [0.0]}
                        for index in range(input_count)
                    ],
                }
            else:
                if protocol == "openai-responses":
                    payload = {
                        "id": "mock-fixed",
                        "object": "response",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "mock"}],
                            }
                        ],
                    }
                else:
                    payload = {
                        "id": "mock-fixed",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "mock"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
            body = json.dumps(payload, separators=(",", ":")).encode()
            handler._write_response(200, {"content-type": "application/json"}, body)
        except Exception:
            error = True
            raise
        finally:
            counters.finish(error=error)

    def stop(self) -> None:
        if self.config.deployment == "external":
            self._endpoints.clear()
            return
        for server in self._servers.values():
            server.shutdown()
        deadline = time.monotonic() + self.config.drain_timeout_seconds
        drained = True
        for server in self._servers.values():
            if not server.wait_for_drain(max(0, deadline - time.monotonic())):
                drained = False
        for server in self._servers.values():
            server.server_close()
        for thread in self._threads.values():
            thread.join(timeout=self.config.drain_timeout_seconds)
        lingering = [thread.name for thread in self._threads.values() if thread.is_alive()]
        self._servers.clear()
        self._threads.clear()
        self._endpoints.clear()
        if lingering or not drained:
            raise RuntimeError("trace runtime drain timed out: " + ", ".join(lingering))

    def verify_and_collect(self) -> TraceRuntimeSummary:
        if self.config.mode in {"capture", "mock-fixed"} and self.config.deployment == "managed":
            if self.config.mode == "mock-fixed":
                snapshots = {
                    name: counters.snapshot()
                    for name, counters in self._capture_counters.items()
                }
                return TraceRuntimeSummary(
                    mode=self.config.mode,
                    bundle_id=None,
                    valid=all(snapshot.errors == 0 for snapshot in snapshots.values()),
                    channels=snapshots,
                )
            if self._writer is None:
                raise RuntimeError("capture bundle writer is unavailable")
            if self._finalized_manifest_id is None:
                manifest = self._writer.finalize()
                self._finalized_manifest_id = manifest.bundle_id
            snapshots = {
                name: counters.snapshot()
                for name, counters in self._capture_counters.items()
            }
            valid = all(snapshot.errors == 0 for snapshot in snapshots.values())
            return TraceRuntimeSummary(
                mode=self.config.mode,
                bundle_id=self._finalized_manifest_id,
                valid=valid,
                channels=snapshots,
            )
        if self.config.deployment == "external":
            snapshots = {}
            for name, config in self._channel_configs.items():
                endpoint = config.listen_url or ""
                with urlopen(
                    _control_url(endpoint, "/counters"),
                    timeout=config.request_timeout_seconds,
                ) as response:
                    snapshots[name] = TraceCounterSnapshot.model_validate_json(
                        response.read()
                    )
        else:
            snapshots = {
                name: matcher.snapshot() for name, matcher in self._matchers.items()
            }
        valid = all(
            snapshot.mismatched == 0
            and snapshot.remaining == 0
            and snapshot.errors == 0
            and snapshot.active == 0
            for snapshot in snapshots.values()
        )
        return TraceRuntimeSummary(
            mode=self.config.mode,
            bundle_id=(
                self._bundle.manifest.bundle_id
                if self._bundle
                else self._external_bundle_id
            ),
            valid=valid,
            channels=snapshots,
        )

    def collect(self, run_dir: Path, summary: TraceRuntimeSummary) -> None:
        trace_artifacts = run_dir / "artifacts" / "trace_runtime"
        trace_artifacts.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **summary.model_dump(mode="json"),
        }
        with (trace_artifacts / "counter-snapshots.jsonl").open(
            "a", encoding="utf-8"
        ) as stream:
            stream.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
        with self._mismatch_lock:
            mismatches = list(self._mismatches)
        if mismatches:
            with (trace_artifacts / "mismatch.jsonl").open("w", encoding="utf-8") as stream:
                for item in mismatches:
                    stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                    stream.write("\n")

    def _channel_snapshot(self, channel_name: str) -> TraceCounterSnapshot:
        if self.config.mode in {"capture", "mock-fixed"}:
            return self._capture_counters[channel_name].snapshot()
        return self._matchers[channel_name].snapshot()

    def _health_payload(self, channel_name: str) -> dict[str, Any]:
        return {
            "status": "ok",
            "channel": channel_name,
            "protocol": self._channel_configs[channel_name].protocol,
            "bundle_id": self._bundle.manifest.bundle_id if self._bundle else None,
        }


def _control_url(endpoint: str, path: str) -> str:
    parts = urlsplit(endpoint)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
