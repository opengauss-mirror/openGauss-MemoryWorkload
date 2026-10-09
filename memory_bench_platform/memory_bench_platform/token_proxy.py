"""Reusable, opt-in OpenAI-compatible usage relay for memory-system clients.

Only endpoint configuration changes. No SDK patches, retries, model parameter
overrides, prompt logging, or local token estimates. Agent/Judge must bypass it.
"""
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
import re
from pathlib import Path
import threading
from urllib.parse import urlsplit
import uuid

LOG = logging.getLogger(__name__)
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
ROUTES = {"/v1/chat/completions": ("llm", "chat/completions"),
          "/v1/responses": ("llm", "responses"),
          "/v1/embeddings": ("embedding", "embeddings")}
USAGE_FIELDS = {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens",
                "total_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens"}
USAGE_DETAILS = {"prompt_tokens_details": {"cached_tokens"},
                 "input_tokens_details": {"cached_tokens"},
                 "completion_tokens_details": {"reasoning_tokens"},
                 "output_tokens_details": {"reasoning_tokens"}}


def safe_usage(value):
    """Store numeric usage only, never arbitrary provider response fields."""
    if not isinstance(value, dict):
        return {}
    valid = lambda x: isinstance(x, int) and not isinstance(x, bool) and x >= 0
    result = {k: v for k, v in value.items() if k in USAGE_FIELDS and valid(v)}
    for key, fields in USAGE_DETAILS.items():
        detail = value.get(key)
        if isinstance(detail, dict):
            result[key] = {k: v for k, v in detail.items() if k in fields and valid(v)}
    return result


def response_usage(body, streaming):
    """Return one final cumulative usage snapshot, not a sum of SSE events."""
    if not streaming:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return {}, False
        return (safe_usage(payload.get("usage")), bool(payload.get("error"))) if isinstance(payload, dict) else ({}, False)
    latest, terminal, failed = {}, False, False
    for block in body.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n\n"):
        data = b"\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith(b"data:"))
        if data == b"[DONE]":
            terminal = True
            continue
        try:
            event = json.loads(data)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        nested = event.get("response")
        usage = safe_usage(event.get("usage")) or safe_usage(nested.get("usage") if isinstance(nested, dict) else None)
        if usage:
            latest = usage
        terminal |= event.get("type") == "response.completed"
        failed |= bool(event.get("error")) or event.get("type") in {"error", "response.failed", "response.incomplete"}
    return (latest if terminal and not failed else {}), failed or not terminal


# Keep terminal SSE frames back until the upstream response and its usage have
# been validated and recorded. Earlier complete frames remain streaming.
# A CRLF is one newline; a trailing CR may be half of the next chunk's CRLF.
SSE_FRAME_END = re.compile(rb"(?:\r\n|\r(?!\n|$)|(?<!\r)\n){2}")


def terminal_sse_frame(frame):
    lines = frame.replace(b"\r\n", b"\n").replace(b"\r", b"\n").splitlines()
    data = b"\n".join(line[5:].lstrip() for line in lines if line.startswith(b"data:"))
    if data == b"[DONE]":
        return True
    try:
        event = json.loads(data)
    except (ValueError, UnicodeError):
        return False
    return isinstance(event, dict) and event.get("type") in {
        "response.completed", "response.failed", "response.incomplete", "error"
    }


class Audit:
    def __init__(self, directory, memory_backend):
        self.root = Path(directory).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.proxy_id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.events = self.root / "requests.jsonl"
        self.events.touch(mode=0o600)
        manifest = {"schema_version": "token-proxy/1", "proxy_id": self.proxy_id,
                    "pid": os.getpid(), "component": "memory", "memory_backend": memory_backend,
                    "routes": list(ROUTES), "created_at": datetime.now(timezone.utc).isoformat()}
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "manifest.json").chmod(0o600)

    def emit(self, **values):
        row = {"schema_version": "token-request/1", "proxy_id": self.proxy_id,
               "timestamp": datetime.now(timezone.utc).isoformat(), **values}
        try:
            with self.lock, self.events.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
                f.flush()
        except Exception as exc:
            # Statistics failure must not turn a valid provider response into failure.
            LOG.warning("memory-system usage audit unavailable (%s)", type(exc).__name__)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=None):
        self.send_response(status)
        for k, v in (headers or {"content-type": "application/json"}).items():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/health":
            self.reply(404, b'{}')
            return
        self.reply(200, json.dumps({"status": "ok", "proxy_id": self.server.audit.proxy_id}).encode())

    def do_POST(self):
        route = ROUTES.get(self.path)
        if route is None:
            self.reply(404, b'{"error":{"type":"unsupported_model_endpoint"}}')
            return
        try:
            # The native SDKs send Content-Length. Reject ambiguous framing.
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError("ambiguous framing")
            length = int(lengths[0])
            if not 0 <= length <= self.server.max_bytes:
                raise ValueError("request size")
            self.connection.settimeout(self.server.timeout)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete body")
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise ValueError("object required")
        except (ValueError, OSError):
            self.reply(400, b'{"error":{"type":"invalid_model_request"}}')
            return
        kind, suffix = route
        model = parsed.get("model")
        model = model[:256] if isinstance(model, str) else None
        common = {"request_id": uuid.uuid4().hex, "model_kind": kind, "model": model,
                  "endpoint": self.path}
        self.server.audit.emit(event="started", **common)
        base = self.server.upstreams[kind]
        conn_cls = HTTPSConnection if base.scheme == "https" else HTTPConnection
        conn = conn_cls(base.hostname, base.port, timeout=self.server.timeout)
        status, usage, error, sent, complete, audited = None, {}, None, False, False, False
        try:
            connection_tokens = {x.strip().lower() for x in self.headers.get("Connection", "").split(",")}
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP | connection_tokens | {"accept-encoding"}}
            headers["Accept-Encoding"] = "identity"
            conn.request("POST", base.path.rstrip("/") + "/" + suffix, body, headers)
            response = conn.getresponse()
            status = response.status
            rh = dict(response.getheaders())
            response_hop = {x.strip().lower() for x in response.getheader("Connection", "").split(",")}
            rh = {k: v for k, v in rh.items() if k.lower() not in HOP | response_hop}
            streaming = "text/event-stream" in response.getheader("Content-Type", "").lower()
            if streaming:
                self.send_response(status)
                for k, v in rh.items():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = sent = True
                data = bytearray()
                forwarded = scan_offset = search_from = 0
                terminal_seen = False
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    if len(data) + len(chunk) > self.server.max_bytes:
                        raise ValueError("response size")
                    data.extend(chunk)
                    if not terminal_seen:
                        while match := SSE_FRAME_END.search(data, search_from):
                            frame_end = match.end()
                            if terminal_sse_frame(data[scan_offset:frame_end]):
                                terminal_seen = True
                                break
                            scan_offset = search_from = frame_end
                        search_from = max(scan_offset, len(data) - 3)
                        if scan_offset > forwarded:
                            self.wfile.write(data[forwarded:scan_offset])
                            self.wfile.flush()
                            forwarded = scan_offset
                payload = bytes(data)
            else:
                payload = response.read(self.server.max_bytes + 1)
                if len(payload) > self.server.max_bytes:
                    raise ValueError("response size")
            expected = response.getheader("Content-Length")
            if expected is not None and len(payload) != int(expected):
                raise IncompleteRead(payload)
            if response.getheader("Content-Encoding", "identity").lower() in {"", "identity"}:
                usage, failed = response_usage(payload, streaming)
                if failed:
                    error = "provider_error_or_incomplete_stream"
            else:
                error = "usage_encoding_unsupported"
            complete = True
            # A fast run-end snapshot must not race a response the SDK can
            # consider complete. For SSE, only the terminal frame was withheld.
            self.server.audit.emit(event="finished", **common, usage=usage,
                                   http_status=status, error_type=error,
                                   outcome="succeeded" if 200 <= status < 300 and error is None else "failed")
            audited = True
            if streaming:
                if forwarded < len(data):
                    self.wfile.write(data[forwarded:])
                    self.wfile.flush()
            else:
                sent = True
                self.reply(status, payload, rh)
        except Exception as exc:
            error = type(exc).__name__
            if not complete:
                usage = {}  # Incomplete upstream responses are not verified usage.
            self.close_connection = True
            if not sent:
                try:
                    self.reply(502, b'{"error":{"type":"model_upstream_incomplete"}}')
                except OSError:
                    pass
        finally:
            conn.close()
            if not audited:
                self.server.audit.emit(event="finished", **common, usage=usage,
                                       http_status=status, error_type=error,
                                       outcome="succeeded" if status is not None and 200 <= status < 300 and error is None else "failed")


def make_server(config, audit_dir, port=0):
    backend = config.get("memory_backend")
    if not isinstance(backend, str) or not backend.strip() or len(backend) > 128:
        raise ValueError("memory_backend must name the dedicated memory system")
    upstreams = {}
    for kind in ("llm", "embedding"):
        url = urlsplit(config[kind + "_base_url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Use an HTTP(S) base URL without credentials, query or fragment")
        upstreams[kind] = url
    timeout = float(config.get("request_timeout_seconds", 900))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("request_timeout_seconds must be finite and positive")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        server.audit = Audit(audit_dir, backend)
        server.upstreams, server.timeout, server.max_bytes = upstreams, timeout, 64 * 1024 * 1024
        return server
    except Exception:
        server.server_close()
        raise


def serve(config_path, audit_dir, port):
    server = make_server(json.loads(Path(config_path).read_text()), audit_dir, port)
    print(json.dumps({"base_url": f"http://127.0.0.1:{server.server_port}/v1",
                      "audit_dir": str(server.audit.root), "proxy_id": server.audit.proxy_id}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
