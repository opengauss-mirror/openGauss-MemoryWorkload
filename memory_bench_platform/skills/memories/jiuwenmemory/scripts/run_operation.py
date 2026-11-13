"""JiuwenMemory v0.2.0 native HTTP adapter."""
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def historical_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.strptime(value, "%I:%M %p on %d %B, %Y")
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()


def run_operation(request):
    started = time.monotonic()
    operation = {}
    error = {}
    try:
        inputs = request.get("inputs") or {}
        scope_id = inputs.get("scope_id") or request.get("scope_id")
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError("scope_id required")
        # The native local indexes match the entire Scope. All history and
        # fresh QA sessions of an episode therefore share one backend scope.
        scope = {
            "org": os.environ.get("JIUWENMEMORY_ORG", "local"),
            "user": os.environ.get("JIUWENMEMORY_USER", "developer"),
            "session": hashlib.sha256(scope_id.encode()).hexdigest(),
        }
        operation = {"scope_id": scope_id, "backend_scope": scope,
                     "runtime_version": os.environ.get("JIUWENMEMORY_RUNTIME_VERSION", "unrecorded")}
        base = os.environ.get("JIUWENMEMORY_API_URL", "http://127.0.0.1:8137").rstrip("/")
        endpoint = urlsplit(base)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username is not None or endpoint.query or endpoint.fragment:
            raise ValueError("invalid API URL")
        headers = {"Content-Type": "application/json"}
        if key := os.environ.get("JIUWENMEMORY_API_KEY"):
            headers["Authorization"] = "Bearer " + key

        def call(verb, payload, timeout):
            http_request = Request(base + "/v1/" + verb, data=json.dumps(payload).encode(), headers=headers)
            with build_opener(NoRedirect()).open(http_request, timeout=timeout) as response:
                return json.load(response)

        if request.get("action") == "ingest":
            content = inputs.get("content")
            event = inputs.get("event_id") or request.get("idempotency_key") or request.get("task_id")
            if not isinstance(content, str) or not content.strip() or not event:
                raise ValueError("history and event identity required")
            session_id = str(event)
            payload = {
                "scope": scope,
                "content": content,
                "system_metadata": {"infer": "true"},
                "user_metadata": {"history_session_id": session_id},
            }
            if stamp := inputs.get("occurred_at") or inputs.get("timestamp"):
                payload["occurred_at"] = historical_time(stamp)
                payload["system_metadata"]["observation_date"] = payload["occurred_at"][:10]
            rows = call("add", payload, 870)
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("id") for row in rows):
                raise ValueError("invalid native add result")
            operation["session_id"] = session_id
            output = {"session_id": session_id, "memory_ids": [row["id"] for row in rows]}
        elif request.get("action") == "recall":
            query = inputs.get("query")
            limit = inputs.get("node_limit", inputs.get("top_k", 10))
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query required")
            if isinstance(limit, bool) or not isinstance(limit, (int, str)) or not 1 <= int(limit) <= 1000:
                raise ValueError("invalid retrieval limit")
            result = call("search", {"context": {"scope": scope}, "query": query, "top_k": int(limit)}, 60)
            rows = result.get("items") if isinstance(result, dict) else None
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("content"), str) or not row["content"].strip() for row in rows):
                raise ValueError("invalid native search result")
            channel_errors = result.get("errors", [])
            output = {"memories": rows, "evidence_text": "\n\n".join(row["content"] for row in rows),
                      "channel_errors": channel_errors}
            if channel_errors and not rows:
                error = {"code": "recall_channel_error",
                         "message": "JiuwenMemory recall returned no results and reported channel errors"}
        else:
            raise ValueError("unsupported action")
        result = {"status": "failed" if error else "ok", "state": "failed" if error else "completed",
                  "operation": operation, "output": output, "error": error}
    except Exception as exc:
        result = {"status": "failed", "state": "failed", "operation": operation, "output": {},
                  "error": {"code": type(exc).__name__, "message": "JiuwenMemory operation failed: " + type(exc).__name__}}
        if hasattr(exc, "code"):
            result["error"]["http_status"] = exc.code
    result.update(metrics=[{"name": "memory_runner_ms", "value": (time.monotonic() - started) * 1000, "unit": "ms"}], artifacts=[])
    return result


if __name__ == "__main__":
    json.dump(run_operation(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
