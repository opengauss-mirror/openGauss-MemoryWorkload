"""Synchronous Mem0 OSS HTTP adapter for memory/1 (no write retries)."""
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request


def run_operation(request, *, environ=None, urlopen=None):
    env = dict(os.environ if environ is None else environ)
    opener = urlopen or urllib.request.urlopen
    started = time.monotonic()
    operation = {}
    try:
        inputs = request.get("inputs") or {}
        scope = str(inputs.get("scope_id") or "").strip()
        if not scope:
            raise ValueError("Mem0 requires scope_id for episode isolation")
        digest = lambda s: hashlib.sha256(s.encode()).hexdigest()[:24]
        user = "mbp-" + digest(scope)
        action = request.get("action")
        profile = env.get("MEM0_HTTP_PROFILE", "server")
        if profile not in ("server", "benchmark"):
            raise ValueError("MEM0_HTTP_PROFILE must be server or benchmark")
        operation = {"scope_id": scope, "user_id": user,
                     "runtime_version": env.get("MEM0_RUNTIME_VERSION", "unrecorded"),
                     "http_profile": profile}
        if action == "ingest":
            content = inputs.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("ingest requires nonempty content")
            seed = request.get("idempotency_key") or request.get("task_id")
            if not seed:
                raise ValueError("ingest requires task_id or idempotency_key")
            session = "mbp-session-" + digest(scope + ":" + str(seed))
            operation["session_id"] = session
            metadata = {"scope_id": scope, "source_session_id": session}
            occurred = inputs.get("occurred_at") or inputs.get("timestamp")
            if occurred:
                metadata["occurred_at"] = str(occurred)
                content = f"[Historical conversation date: {occurred}]\n{content}"
            body = {"user_id": user, "messages": [{"role": "user", "content": content}],
                    "metadata": metadata}
            # benchmark server uses infer=True by default but does not expose it.
            if profile == "server":
                body["infer"] = True
            path, timeout = "/memories", 850
        elif action == "recall":
            query = inputs.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("recall requires nonempty query")
            limit = int(inputs.get("node_limit") or inputs.get("top_k") or env.get("MEM0_TOP_K", "20"))
            if not 1 <= limit <= 1000:
                raise ValueError("Mem0 retrieval limit must be between 1 and 1000")
            body = {"query": query, "filters": {"user_id": user}}
            if profile == "server":
                body["top_k"] = limit
            else:
                body.update(user_id=user, limit=limit)
            path, timeout = "/search", 80
        else:
            raise ValueError(f"unsupported Mem0 action: {action}")
        timeout = float(env.get("MEM0_HTTP_TIMEOUT_SECONDS", timeout))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Mem0 timeout must be positive and finite")
        headers = {"Content-Type": "application/json"}
        if env.get("MEM0_API_KEY"):
            headers["X-API-Key"] = env["MEM0_API_KEY"]
        base = env.get("MEM0_API_URL", "http://127.0.0.1:8000").rstrip("/")
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with opener(req, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            # Error bodies can contain credentials, prompts or infrastructure details.
            raise RuntimeError(f"Mem0 HTTP {exc.code}; inspect server logs") from exc
        if isinstance(data, dict):
            if data.get("error") or data.get("event_id") or data.get("status") in ("failed", "error", "PENDING", "pending"):
                raise ValueError("Mem0 returned an error or asynchronous acknowledgement; synchronous OSS required")
            rows = data.get("results")
        else:
            rows = data
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ValueError("Mem0 response requires a results list")
        for row in rows:
            if row.get("status") in ("PENDING", "pending", "FAILED", "failed"):
                raise ValueError("Mem0 operation did not complete")
            if action == "ingest" and not row.get("id"):
                raise ValueError("Mem0 write result requires a memory id")
            if action == "recall" and (not isinstance(row.get("memory"), str) or not row.get("id")):
                raise ValueError("Mem0 search result requires id and memory text")
            if row.get("user_id") not in (None, user):
                raise ValueError("Mem0 response belongs to another episode")
        output = {"backend_result": data}
        if action == "ingest":
            output.update(session_id=operation["session_id"], memory_count=len(rows))
        else:
            output.update(evidence_text="\n\n".join(r["memory"] for r in rows), memories=rows, top_k=limit)
        result = {"status": "ok", "state": "completed", "operation": operation,
                  "output": output, "metrics": [], "artifacts": [], "error": {}}
    except Exception as exc:
        message = str(exc)
        for name, value in env.items():
            if value and any(k in name.upper() for k in ("KEY", "TOKEN", "PASSWORD", "SECRET")):
                message = message.replace(value, "[REDACTED]")
        result = {"status": "failed", "state": "failed", "operation": operation,
                  "output": {}, "metrics": [], "artifacts": [],
                  "error": {"code": type(exc).__name__, "message": message}}
    result["metrics"].append({"name": "memory_runner_ms", "value": (time.monotonic()-started)*1000, "unit": "ms"})
    return result


if __name__ == "__main__":
    json.dump(run_operation(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
