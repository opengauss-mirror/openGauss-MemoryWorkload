"""MemOS self-hosted product API adapter. Synchronous writes, no write retries."""
from datetime import datetime
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward credentials or replay operations to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class InvalidHistoricalTime(ValueError):
    """Invalid explicitly supplied historical time; never substitute wall time."""


def _chat_time(value):
    if isinstance(value, str) and value.strip():
        value = value.strip()
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
        # LoCoMo's native session timestamp; preserve its unspecified timezone.
        try:
            return datetime.strptime(value, "%I:%M %p on %d %B, %Y").isoformat()
        except ValueError:
            pass
    raise InvalidHistoricalTime("expected ISO date/time or LoCoMo session time")


def _memories(data):
    if not isinstance(data, dict):
        raise ValueError("MemOS search data must be an object")
    rows = []
    if "memory_detail_list" in data:
        preferences = data.get("preference_detail_list")
        buckets = [("text", data["memory_detail_list"]),
                   ("preference", [] if preferences is None else preferences)]
        for kind, items in buckets:
            if not isinstance(items, list):
                raise ValueError("MemOS memory list must be an array")
            for item in items:
                rows.append((kind, item))
    elif "text_mem" in data:
        for kind, key in [("text", "text_mem"), ("preference", "pref_mem")]:
            buckets = data.get(key)
            if buckets is None:
                buckets = []
            if not isinstance(buckets, list):
                raise ValueError("MemOS memory buckets must be an array")
            for bucket in buckets:
                if not isinstance(bucket, dict) or not isinstance(bucket.get("memories"), list):
                    raise ValueError("MemOS bucket requires memories array")
                rows.extend((kind, item) for item in bucket["memories"])
    else:
        raise ValueError("MemOS response has no recognized memory result")
    memories = []
    for kind, item in rows:
        if not isinstance(item, dict):
            raise ValueError("MemOS memory must be an object")
        text = item.get("memory_value", item.get("memory", item.get("preference")))
        if not isinstance(text, str) or not text.strip():
            raise ValueError("MemOS memory requires nonempty text")
        memories.append({**item, "memory": text, "kind": kind})
    note = data.get("preference_note") or data.get("pref_note")
    if note is not None:
        if not isinstance(note, str):
            raise ValueError("MemOS preference note must be text")
        if note.strip():
            memories.append({"memory": note, "kind": "preference_note"})
    return memories


def run_operation(request, *, environ=None, urlopen=None):
    env = dict(os.environ if environ is None else environ)
    opener = urlopen or urllib.request.build_opener(NoRedirect()).open
    started = time.monotonic()
    operation = {}
    try:
        inputs = request.get("inputs") or {}
        scope = inputs.get("scope_id")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("MemOS requires scope_id for episode isolation")
        digest = lambda text: hashlib.sha256(text.encode()).hexdigest()[:24]
        user = "mbp-" + digest(scope)
        operation = {"scope_id": scope, "user_id": user, "mem_cube_id": user,
                     "runtime_version": env.get("MEMOS_RUNTIME_VERSION", "unrecorded")}
        action = request.get("action")
        body = {"user_id": user, "mem_cube_id": user}
        if action == "ingest":
            if env.get("MEMOS_EXCLUSIVE_INSTANCE") != "1":
                raise ValueError("MemOS requires a dedicated instance for scheduler completion")
            content = inputs.get("content")
            seed = request.get("idempotency_key") or request.get("task_id")
            if not isinstance(content, str) or not content.strip() or not seed:
                raise ValueError("MemOS ingest requires content and task identity")
            session = "mbp-session-" + digest(scope + ":" + str(seed))
            operation["session_id"] = session
            occurred = inputs.get("occurred_at")
            if occurred is None:
                occurred = inputs.get("timestamp")
            message = {"role": "user", "content": content}
            if occurred is not None:
                message["chat_time"] = _chat_time(occurred)
                message["content"] = f"[Historical conversation date: {occurred}]\n{content}"
            body.update(messages=[message],
                        session_id=session, writable_cube_ids=[user], async_mode="sync")
            path, expected = "/product/add", "Memory added successfully"
        elif action == "recall":
            query = inputs.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("MemOS recall requires query")
            raw = inputs.get("node_limit", inputs.get("top_k", env.get("MEMOS_TOP_K", "20")))
            if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                raise ValueError("MemOS retrieval limit must be an integer")
            limit = int(raw)
            if not 1 <= limit <= 1000:
                raise ValueError("MemOS retrieval limit must be between 1 and 1000")
            body.update(query=query, readable_cube_ids=[user], top_k=limit, mode="fast",
                        include_preference=True, pref_top_k=6, context_format="memory",
                        search_tool_memory=False, tool_mem_top_k=0,
                        include_skill_memory=False, skill_mem_top_k=0)
            path, expected = "/product/search", "Search completed successfully"
        else:
            raise ValueError("unsupported MemOS action")
        timeout = float(env.get("MEMOS_HTTP_TIMEOUT_SECONDS", "850"))
        if not math.isfinite(timeout) or not 0 < timeout <= 850:
            raise ValueError("MemOS HTTP timeout must be in (0, 850]")
        base = env.get("MEMOS_API_URL", "http://127.0.0.1:8000").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if env.get("MEMOS_API_KEY"):
            headers["Authorization"] = "Token " + env["MEMOS_API_KEY"]
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with opener(req, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"MemOS HTTP {exc.code}; inspect server logs") from exc
        if not isinstance(result, dict) or result.get("code") != 200 or result.get("message") != expected:
            raise ValueError("MemOS returned an invalid or unsuccessful product response")
        data = result.get("data")
        output = {"backend_result": result}
        if action == "ingest":
            if not isinstance(data, list) or any(not isinstance(row, dict) or not row.get("memory_id") for row in data):
                raise ValueError("MemOS synchronous write requires memory results")
            if any(row.get("cube_id") not in (None, user) for row in data):
                raise ValueError("MemOS write result belongs to another scope")
            # A sync add can still enqueue consolidation. Drain the native queue
            # on a dedicated instance before exposing a completed ingest.
            if env.get("MEMOS_EXCLUSIVE_INSTANCE") != "1":
                raise ValueError("MemOS requires a dedicated instance for scheduler completion")
            deadline = started + timeout
            while True:
                status_req = urllib.request.Request(base + "/product/scheduler/allstatus", headers=headers)
                with opener(status_req, timeout=max(1, min(30, deadline-time.monotonic()))) as response:
                    status = json.load(response)
                summary = (status.get("data") or {}).get("scheduler_summary") if isinstance(status, dict) else None
                fields = ("waiting", "in_progress", "pending", "failed", "cancelled")
                if status.get("code") != 200 or not isinstance(summary, dict) or any(type(summary.get(k)) is not int or summary[k] < 0 for k in fields):
                    raise ValueError("MemOS scheduler status is unavailable")
                if summary["failed"] or summary["cancelled"]:
                    raise ValueError("MemOS scheduler reported failed tasks")
                if not any(summary[k] for k in ("waiting", "in_progress", "pending")):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("MemOS scheduler did not settle")
                time.sleep(min(1, max(0, deadline-time.monotonic())))
            output.update(session_id=session, memory_count=len(data), scheduler_status=status)
        else:
            memories = _memories(data)
            output.update(memories=memories, top_k=limit,
                          evidence_text="\n\n".join(row["memory"] for row in memories))
        response = {"status": "ok", "state": "completed", "operation": operation,
                    "output": output, "metrics": [], "artifacts": [], "error": {}}
    except Exception as exc:
        # Do not include remote response bodies, URLs, credentials or prompts.
        response = {"status": "failed", "state": "failed", "operation": operation,
                    "output": {}, "metrics": [], "artifacts": [],
                    "error": {"code": type(exc).__name__, "message": "MemOS operation failed: " + type(exc).__name__}}
    response["metrics"].append({"name": "memory_runner_ms", "value": (time.monotonic()-started)*1000, "unit": "ms"})
    return response


if __name__ == "__main__":
    json.dump(run_operation(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
