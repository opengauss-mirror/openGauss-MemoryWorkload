"""oGMemory HTTP adapter. No implicit retries of writes or extraction."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import sys
import time
from typing import Any
import urllib.error
import urllib.request


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _identity(inputs: dict, env: dict) -> dict:
    scope = str(inputs.get("scope_id") or "").strip()
    if not scope:
        raise ValueError("oGMemory requires inputs.scope_id for episode isolation")
    suffix = _digest(scope)
    # Some dev configurations search all owner spaces within an account.
    # Account isolation is therefore required in addition to user/agent isolation.
    return {"accountId": env.get("OGMEM_ACCOUNT_ID", "memory-bench") + "-" + suffix,
            "userId": "mbp-user-" + suffix, "agentId": "mbp-agent-" + suffix}


def _created_at(value: str) -> str:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        stamp = datetime.strptime(value, "%I:%M %p on %d %B, %Y")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.isoformat()


def _result(state: str, operation: dict, output: dict) -> dict:
    return {"status": "ok", "state": state, "operation": operation,
            "output": output, "metrics": [], "artifacts": [], "error": {}}


def run_operation(request: dict[str, Any], *, environ=None, urlopen=None) -> dict:
    env = dict(os.environ if environ is None else environ)
    opener = urlopen or urllib.request.urlopen
    started = time.perf_counter()
    inputs = request.get("inputs") or {}
    action = request.get("action")
    operation = {}
    try:
        identity = _identity(inputs, env)
        base = env.get("OGMEM_API_URL", "http://127.0.0.1:8090").rstrip("/")
        scope = inputs["scope_id"]
        source = inputs.get("operation") or {}
        if source and source.get("scope_id") != scope:
            raise ValueError("operation scope does not match requested episode")
        session_id = str(inputs.get("session_id") or source.get("session_id") or "")
        if inputs.get("session_id") and source.get("session_id") and session_id != source["session_id"]:
            raise ValueError("session_id does not match operation")
        operation = {"scope_id": scope, "session_id": session_id,
                     "type": "memory_" + str(action),
                     "runtime_version": env.get("OGMEM_RUNTIME_VERSION", "unrecorded")}

        def post(path: str, body: dict, timeout: float = 30) -> dict:
            headers = {"Content-Type": "application/json", "X-Account-ID": identity["accountId"],
                       "X-User-ID": identity["userId"], "X-Agent-ID": identity["agentId"]}
            if env.get("OGMEM_API_KEY"):
                headers["X-API-Key"] = env["OGMEM_API_KEY"]
            req = urllib.request.Request(base + path, data=json.dumps({**identity, **body}).encode(), headers=headers, method="POST")
            try:
                with opener(req, timeout=timeout) as response:
                    data = json.load(response)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"oGMemory HTTP {exc.code}: {exc.read().decode(errors='replace')[:2000]}") from exc
            if not isinstance(data, dict):
                raise ValueError("oGMemory returned a non-object response")
            if data.get("ok") is False or data.get("status") in ("failed", "error") or data.get("error"):
                raise RuntimeError("oGMemory rejected operation: " + json.dumps(data, ensure_ascii=False)[:2000])
            return data

        if action == "ingest":
            content = inputs.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("memory.ingest requires inputs.content")
            seed = request.get("idempotency_key") or request.get("task_id")
            if not seed:
                raise ValueError("memory.ingest requires task_id or idempotency_key")
            session_id = "mbp-" + _digest(scope + ":" + str(seed))
            operation["session_id"] = session_id
            body = {"role": "user", "content": content}
            occurred_at = inputs.get("occurred_at") or inputs.get("timestamp")
            if occurred_at:
                body["created_at"] = _created_at(str(occurred_at))
                body["content"] = f"[Occurred at: {occurred_at}]\n{content}"
            data = post(f"/api/v1/sessions/{session_id}/messages", body)
            if data.get("ok") is not True or not data.get("message_id"):
                raise RuntimeError("oGMemory did not confirm a buffered message")
            # Buffered, not yet extracted. The composer follows with flush.
            result = _result("accepted", operation, {"accepted": True, "session_id": session_id, "backend_result": data})
        elif action == "flush":
            if not session_id:
                raise ValueError("memory.flush requires the ingest session_id")
            timeout = float(env.get("OGMEM_FLUSH_TIMEOUT_SECONDS", "900"))
            data = post("/api/v1/compact", {"sessionId": session_id, "shortTermIndexMode": "async"}, timeout)
            if data.get("compacted") is not True:
                raise RuntimeError("compact did not confirm extraction and archival: " + json.dumps(data)[:2000])
            # Compact is synchronous; this identifies the adapter operation,
            # not a backend async job. Readiness is checked by scoped session.
            operation["task_id"] = "compact-" + _digest(scope + ":" + session_id)
            operation["compact_completed"] = True
            result = _result("accepted", operation, {"session_id": session_id, "backend_result": data})
        elif action == "status":
            if not session_id or source.get("compact_completed") is not True:
                raise ValueError("status requires a successfully compacted session operation")
            operation = dict(source)
            body = {"sessionId": session_id, "timeoutSeconds": 1, "drainOutbox": True}
            drained = post("/api/v1/call/wait_until_idle", body, 60)
            if drained.get("reason") == "session_not_found":
                raise RuntimeError("compacted session disappeared; readiness cannot be verified")
            if int((drained.get("drain") or {}).get("failed", 0)):
                raise RuntimeError("oGMemory index writes failed: " + json.dumps(drained))
            settled = post("/api/v1/call/wait_until_idle", {**body, "drainOutbox": False, "waitOutbox": True}, 10)
            outbox = settled.get("outbox") or {}
            if settled.get("reason") == "session_not_found" or outbox.get("supported") is False or outbox.get("error") or "total" not in outbox:
                raise RuntimeError("oGMemory cannot verify scoped index readiness: " + json.dumps(settled))
            complete = settled.get("idle") is True and int(outbox.get("total", 0)) == 0
            result = _result("completed" if complete else "accepted", operation, {"session_id": session_id, "backend_result": settled, "drain": drained.get("drain", {})})
        elif action == "recall":
            query = inputs.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("memory.recall requires inputs.query")
            # A separate session per question prevents QA history entering recall.
            recall_session = "mbp-qa-" + _digest(scope + ":" + str(request.get("task_id") or query))
            budget = int(env.get("OGMEM_COMPOSE_TOKEN_BUDGET", "128000"))
            if budget <= 0:
                raise ValueError("OGMEM_COMPOSE_TOKEN_BUDGET must be positive")
            data = post("/api/v1/compose", {
                "sessionId": recall_session, "prompt": query,
                "messages": [], "tokenBudget": budget,
            }, float(env.get("OGMEM_COMPOSE_TIMEOUT_SECONDS", "300")))
            messages = data.get("messages")
            if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
                raise ValueError("oGMemory compose response requires a messages list")
            evidence = []
            for message in messages:
                if message.get("_ogmem") is not True:
                    continue
                content = message.get("content", "")
                if not isinstance(content, str):
                    raise ValueError("oGMemory composed memory content must be text")
                if content:
                    evidence.append(content)
            result = _result("completed", operation, {
                "evidence_text": "\n\n".join(evidence),
                "backend_result": data, "recall_mode": "compose",
                "session_id": recall_session, "token_budget": budget,
            })
        else:
            raise ValueError(f"unsupported oGMemory action: {action}")
    except Exception as exc:
        message = str(exc)
        for name,value in env.items():
            if value and any(token in name.upper() for token in ("KEY", "TOKEN", "PASSWORD", "SECRET")):
                message = message.replace(value, "[REDACTED]")
        result = {"status": "failed", "state": "failed", "operation": operation, "output": {},
                  "metrics": [], "artifacts": [], "error": {"code": type(exc).__name__, "message": message}}
    result["metrics"].append({"name": "memory_runner_ms", "value": round((time.perf_counter()-started)*1000, 3), "unit": "ms"})
    return result


if __name__ == "__main__":
    json.dump(run_operation(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
