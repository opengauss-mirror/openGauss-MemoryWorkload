"""OpenViking backend API helpers for LoCoMo evaluation.

This module contains OpenViking-specific HTTP, token parsing, and indexing
helpers. LoCoMo run orchestration should stay in eval.py.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Callable

import requests


RequestHeadersFn = Callable[..., dict | None]


def merge_ov_token_usage(left: dict | None, right: dict | None) -> dict | None:
    if not left and not right:
        return None
    provider = (
        (left or {}).get("provider")
        or (right or {}).get("provider")
        or "openviking"
    )
    merged = {
        "provider": provider,
        "llm_prompt": 0,
        "llm_completion": 0,
        "llm_total": 0,
        "embedding": 0,
        "memories": 0,
        "memory_write": 0,
        "memory_edit": 0,
        "task_id": "",
    }
    for payload in (left or {}, right or {}):
        merged["llm_prompt"] += int(payload.get("llm_prompt", 0) or 0)
        merged["llm_completion"] += int(payload.get("llm_completion", 0) or 0)
        merged["llm_total"] += int(payload.get("llm_total", 0) or 0)
        merged["embedding"] += int(payload.get("embedding", 0) or 0)
        merged["memories"] += int(payload.get("memories", 0) or 0)
        merged["memory_write"] += int(payload.get("memory_write", 0) or 0)
        merged["memory_edit"] += int(payload.get("memory_edit", 0) or 0)
        task_id = str(payload.get("task_id", "") or "").strip()
        if task_id:
            merged["task_id"] = task_id
    return merged


def get_openviking_commit_timeout_seconds() -> float:
    raw = os.environ.get("LOCOMO_OPENVIKING_COMMIT_TIMEOUT_SECONDS", "60").strip()
    try:
        value = float(raw or "0")
    except ValueError:
        value = 60.0
    return max(value, 5.0)


def get_openviking_task_request_timeout_seconds() -> float:
    raw = os.environ.get("LOCOMO_OPENVIKING_TASK_REQUEST_TIMEOUT_SECONDS", "30").strip()
    try:
        value = float(raw or "0")
    except ValueError:
        value = 30.0
    return max(value, 5.0)


def get_openviking_ingest_task_wait_seconds() -> int:
    raw = os.environ.get("LOCOMO_OPENVIKING_INGEST_TASK_WAIT_SECONDS", "900").strip()
    try:
        value = int(raw or "0")
    except ValueError:
        value = 900
    return max(value, 30)


def get_openviking_max_pending_ingest_sessions() -> int:
    raw = os.environ.get("LOCOMO_OPENVIKING_MAX_PENDING_INGEST_SESSIONS", "0").strip()
    try:
        value = int(raw or "0")
    except ValueError:
        value = 0
    return max(value, 0)


def get_openviking_chunk_slow_threshold_seconds() -> float:
    raw = os.environ.get("LOCOMO_OPENVIKING_CHUNK_SLOW_THRESHOLD_SECONDS", "120").strip()
    try:
        value = float(raw or "0")
    except ValueError:
        value = 120.0
    return max(value, 1.0)


def should_skip_openviking_qa_commit(session_key: str | None) -> bool:
    if not session_key:
        return False
    override = os.environ.get("LOCOMO_OPENVIKING_QA_COMMIT", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return False
    return session_key.startswith("qa-")


def should_wait_for_openviking_ingest_commit() -> bool:
    override = os.environ.get("LOCOMO_OPENVIKING_INGEST_COMMIT_WAIT", "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    return True


def parse_ov_task_result(data: dict) -> dict | None:
    result = data.get("result", {})
    if isinstance(result, dict) and "result" in result:
        result = result["result"]
    if not isinstance(result, dict):
        result = {}
    token = result.get("token_usage", {})
    llm = token.get("llm", {})
    embed = token.get("embedding", {})
    memories = result.get("memories_extracted", {})
    mem_count = memories.get("memory_write", 0) + memories.get("memory_edit", 0)
    return {
        "llm_prompt": llm.get("prompt_tokens", 0),
        "llm_completion": llm.get("completion_tokens", 0),
        "llm_total": llm.get("total_tokens", 0),
        "embedding": embed.get("total_tokens", 0),
        "memories": mem_count,
        "task_id": data.get("result", {}).get("task_id", ""),
    }


def is_empty_ov_token_usage(payload: dict | None) -> bool:
    if not payload:
        return True
    return (
        int(payload.get("llm_total", 0) or 0) == 0
        and int(payload.get("embedding", 0) or 0) == 0
        and int(payload.get("memories", 0) or 0) == 0
    )


def load_openviking_plugin_config(state_dir: str) -> dict:
    if not state_dir:
        return {}
    config_path = os.path.join(state_dir, "openclaw.json")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (
            data.get("plugins", {})
            .get("entries", {})
            .get("openviking", {})
            .get("config", {})
        ) or {}
    except (json.JSONDecodeError, IOError, AttributeError):
        return {}


def resolve_openviking_agent_header(plugin_cfg: dict, fallback_agent_id: str) -> str:
    configured_prefix = str(plugin_cfg.get("agent_prefix") or "").strip()
    if configured_prefix:
        return f"{configured_prefix}_main"
    explicit_agent = str(plugin_cfg.get("agentId") or "").strip()
    if explicit_agent:
        return f"{explicit_agent}_main"
    if fallback_agent_id:
        return fallback_agent_id
    env_agent = str(os.environ.get("OPENVIKING_AGENT_ID", "")).strip()
    if env_agent:
        return env_agent
    return "main"


def ov_request_headers(state_dir: str = "", fallback_agent_id: str = "main") -> dict | None:
    plugin_cfg = load_openviking_plugin_config(state_dir)
    api_key = str(
        plugin_cfg.get("apiKey")
        or os.environ.get("OPENVIKING_API_KEY")
        or os.environ.get("OPENVIKING_ROOT_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return None

    headers = {"X-API-Key": api_key}
    account_id = str(plugin_cfg.get("accountId") or os.environ.get("OPENVIKING_ACCOUNT_ID") or "").strip()
    user_id = str(plugin_cfg.get("userId") or os.environ.get("OPENVIKING_USER_ID") or "").strip()
    agent_id = resolve_openviking_agent_header(plugin_cfg, fallback_agent_id)
    if account_id:
        headers["X-OpenViking-Account"] = account_id
    if user_id:
        headers["X-OpenViking-User"] = user_id
    if agent_id:
        headers["X-OpenViking-Agent"] = agent_id
    return headers


def build_openviking_ingest_agent_id(base_agent_id: str, session_key: str | None) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(base_agent_id or "main")).strip("-") or "main"
    if not session_key:
        return base
    suffix = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(session_key)).strip("-")
    if not suffix:
        return base
    return f"{base}--{suffix}"


def read_content_by_uri(
    ov_api_url: str,
    headers: dict[str, str],
    uri: str,
    *,
    requests_module=requests,
) -> str:
    try:
        resp = requests_module.get(
            f"{ov_api_url}/api/v1/content/read",
            headers=headers,
            params={"uri": uri},
            timeout=60,
        )
        if not resp.ok:
            return ""
        payload = resp.json() if resp.content else {}
        result = payload.get("result", payload) if isinstance(payload, dict) else payload
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("content", "text", "body", "markdown"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            parts = result.get("parts")
            if isinstance(parts, list):
                texts = []
                for part in parts:
                    if isinstance(part, dict):
                        value = part.get("text") or part.get("content")
                        if isinstance(value, str) and value.strip():
                            texts.append(value.strip())
                if texts:
                    return "\n".join(texts)
        return ""
    except Exception:
        return ""


def commit_openviking_session(
    *,
    ov_api_url: str,
    session_id: str,
    keep_recent_count: int | None = None,
    wait: bool = False,
    state_dir: str = "",
    fallback_agent_id: str = "main",
    request_headers_fn: RequestHeadersFn = ov_request_headers,
    requests_module=requests,
) -> dict:
    headers = request_headers_fn(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        raise RuntimeError("Missing OpenViking API key for direct session commit")
    payload = {"wait": wait}
    if keep_recent_count is not None:
        payload["keep_recent_count"] = keep_recent_count
    resp = requests_module.post(
        f"{ov_api_url}/api/v1/sessions/{session_id}/commit",
        json=payload,
        headers=headers,
        timeout=get_openviking_commit_timeout_seconds(),
    )
    if resp.status_code == 404:
        return {
            "status": "not_found",
            "task_id": "",
            "archived": False,
            "memories_extracted": {},
            "error": f"HTTP 404 for session {session_id}",
        }
    resp.raise_for_status()
    body = resp.json()
    result = body.get("result", {}) if isinstance(body, dict) else {}
    return {
        "status": result.get("status", ""),
        "task_id": result.get("task_id", ""),
        "archived": result.get("archived", False),
        "memories_extracted": result.get("memories_extracted", {}),
    }


def query_ov_task_token_usage(
    ov_api_url: str,
    task_id: str,
    *,
    state_dir: str = "",
    fallback_agent_id: str = "main",
    max_wait: int = 60,
    resource_id: str | None = None,
    return_diag: bool = False,
    request_headers_fn: RequestHeadersFn = ov_request_headers,
    requests_module=requests,
    time_module=time,
    latest_task_fn: Callable[..., dict | None] | None = None,
) -> dict | tuple[dict | None, dict]:
    headers = request_headers_fn(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        result = None
        diag = {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "missing_headers"}
        return (result, diag) if return_diag else result
    deadline = time_module.time() + max_wait
    interval = 2
    poll_count = 0
    started = time_module.monotonic()
    fallback_used = False
    final_status = ""
    latest = latest_task_fn or query_ov_latest_task
    try:
        while True:
            poll_count += 1
            resp = requests_module.get(
                f"{ov_api_url}/api/v1/tasks/{task_id}",
                headers=headers,
                timeout=get_openviking_task_request_timeout_seconds(),
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("result", {}).get("status", "") if isinstance(data.get("result"), dict) else ""
            final_status = status or final_status
            if status in ("completed", "failed", ""):
                parsed = parse_ov_task_result(data)
                if is_empty_ov_token_usage(parsed) and resource_id:
                    fallback = latest(
                        ov_api_url,
                        resource_id=resource_id,
                        state_dir=state_dir,
                        fallback_agent_id=fallback_agent_id,
                    )
                    if fallback and not is_empty_ov_token_usage(fallback):
                        fallback_used = True
                        result = fallback
                        diag = {"poll_count": poll_count, "elapsed_seconds": round(time_module.monotonic() - started, 3), "timed_out": False, "fallback_used": fallback_used, "final_status": status or "completed"}
                        return (result, diag) if return_diag else result
                result = parsed
                diag = {"poll_count": poll_count, "elapsed_seconds": round(time_module.monotonic() - started, 3), "timed_out": False, "fallback_used": fallback_used, "final_status": status or "completed"}
                return (result, diag) if return_diag else result
            if time_module.time() >= deadline:
                if resource_id:
                    fallback = latest(
                        ov_api_url,
                        resource_id=resource_id,
                        state_dir=state_dir,
                        fallback_agent_id=fallback_agent_id,
                    )
                    if fallback and not is_empty_ov_token_usage(fallback):
                        fallback_used = True
                        result = fallback
                        diag = {"poll_count": poll_count, "elapsed_seconds": round(time_module.monotonic() - started, 3), "timed_out": True, "fallback_used": fallback_used, "final_status": final_status or "timeout"}
                        return (result, diag) if return_diag else result
                result = None
                diag = {"poll_count": poll_count, "elapsed_seconds": round(time_module.monotonic() - started, 3), "timed_out": True, "fallback_used": fallback_used, "final_status": final_status or "timeout"}
                return (result, diag) if return_diag else result
            time_module.sleep(interval)
            interval = min(interval * 2, 10)
    except Exception as e:
        print(f"    [ov-task] Error querying task {task_id}: {e}", file=sys.stderr)
        result = None
        diag = {"poll_count": poll_count, "elapsed_seconds": round(time_module.monotonic() - started, 3), "timed_out": False, "fallback_used": fallback_used, "final_status": "error", "error": str(e)}
        return (result, diag) if return_diag else result


def query_ov_latest_task(
    ov_api_url: str,
    resource_id: str | None = None,
    *,
    state_dir: str = "",
    fallback_agent_id: str = "main",
    request_headers_fn: RequestHeadersFn = ov_request_headers,
    requests_module=requests,
) -> dict | None:
    headers = request_headers_fn(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return None
    try:
        params = {"task_type": "session_commit", "status": "completed", "limit": 1}
        if resource_id:
            params["resource_id"] = resource_id
        resp = requests_module.get(f"{ov_api_url}/api/v1/tasks", params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        tasks = data.get("result", [])
        if tasks:
            task = tasks[0]
            result = parse_ov_task_result({"result": task})
            if result:
                result["task_id"] = task.get("task_id", "")
            return result
    except requests_module.exceptions.HTTPError as e:
        if resource_id and e.response is not None and e.response.status_code == 400:
            try:
                resp = requests_module.get(
                    f"{ov_api_url}/api/v1/tasks",
                    params={"task_type": "session_commit", "status": "completed", "limit": 1},
                    headers=headers,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                tasks = data.get("result", [])
                if tasks:
                    task = tasks[0]
                    result = parse_ov_task_result({"result": task})
                    if result:
                        result["task_id"] = task.get("task_id", "")
                    return result
            except Exception as inner:
                print(f"    [ov-task] Error querying latest task without resource_id: {inner}", file=sys.stderr)
                return None
        print(f"    [ov-task] Error querying latest task: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"    [ov-task] Error querying latest task: {e}", file=sys.stderr)
    return None


def wait_for_ov_latest_task(
    ov_api_url: str,
    resource_id: str | None = None,
    *,
    state_dir: str = "",
    fallback_agent_id: str = "main",
    max_wait: int = 60,
    return_diag: bool = False,
    time_module=time,
    latest_task_fn: Callable[..., dict | None] = query_ov_latest_task,
) -> dict | tuple[dict | None, dict]:
    deadline = time_module.time() + max_wait
    interval = 2
    poll_count = 0
    started = time_module.monotonic()
    while True:
        poll_count += 1
        result = latest_task_fn(
            ov_api_url,
            resource_id=resource_id,
            state_dir=state_dir,
            fallback_agent_id=fallback_agent_id,
        )
        if result:
            diag = {"poll_count": poll_count, "elapsed_seconds": round(time_module.monotonic() - started, 3), "timed_out": False, "fallback_used": True, "final_status": "latest_task"}
            return (result, diag) if return_diag else result
        if time_module.time() >= deadline:
            diag = {"poll_count": poll_count, "elapsed_seconds": round(time_module.monotonic() - started, 3), "timed_out": True, "fallback_used": False, "final_status": "timeout"}
            return (None, diag) if return_diag else None
        time_module.sleep(interval)
        interval = min(interval * 2, 10)


def query_ov_session_usage(
    ov_api_url: str,
    session_id: str,
    *,
    state_dir: str = "",
    fallback_agent_id: str = "main",
    max_wait: int = 30,
    interval: float = 1.0,
    request_headers_fn: RequestHeadersFn = ov_request_headers,
    requests_module=requests,
    time_module=time,
) -> dict | None:
    headers = request_headers_fn(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return None
    deadline = time_module.time() + max_wait
    while True:
        try:
            resp = requests_module.get(f"{ov_api_url}/api/v1/sessions/{session_id}", headers=headers, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            result = body.get("result", {}) if isinstance(body, dict) else {}
            if int(result.get("commit_count", 0) or 0) > 0:
                llm = result.get("llm_token_usage", {}) or {}
                embed = result.get("embedding_token_usage", {}) or {}
                memories = result.get("memories_extracted", {}) or {}
                return {
                    "llm_prompt": int(llm.get("prompt_tokens", 0) or 0),
                    "llm_completion": int(llm.get("completion_tokens", 0) or 0),
                    "llm_total": int(llm.get("total_tokens", 0) or 0),
                    "embedding": int(embed.get("total_tokens", 0) or 0),
                    "memories": int(memories.get("total", 0) or 0),
                    "memory_write": int(memories.get("memory_write", 0) or 0),
                    "memory_edit": int(memories.get("memory_edit", 0) or 0),
                    "commit_count": int(result.get("commit_count", 0) or 0),
                    "last_commit_at": result.get("last_commit_at", ""),
                    "session_id": session_id,
                    "source": "session_meta",
                }
        except Exception as e:
            print(f"    [ov-session] Error querying session {session_id}: {e}", file=sys.stderr)
            return None
        if time_module.time() >= deadline:
            return None
        time_module.sleep(interval)


def query_ov_index_consistency(
    ov_api_url: str,
    uri: str,
    *,
    state_dir: str = "",
    fallback_agent_id: str = "main",
    request_headers_fn: RequestHeadersFn = ov_request_headers,
    requests_module=requests,
) -> dict | None:
    headers = request_headers_fn(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return None
    try:
        resp = requests_module.post(
            f"{ov_api_url}/api/v1/system/consistency",
            json={"uri": uri},
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("result", {}) if isinstance(body, dict) else {}
    except Exception as e:
        print(f"    [ov-consistency] Error checking {uri}: {e}", file=sys.stderr)
        return None


def normalize_ov_task_query_mode(memory_mode: str) -> str:
    if memory_mode == "openviking":
        return "direct_ov_stable"
    return "legacy"


def derive_ov_closure_status(
    ov_token_usage: dict | None,
    consistency: dict | None,
    *,
    recall_total: int = 0,
    direct_recall_count: int = 0,
    response_text: str = "",
    qa_commit_skipped: bool = False,
) -> dict:
    token_emitted = False
    memory_written = False
    index_available = False
    recall_hit = int(recall_total or 0) > 0
    answered = bool(str(response_text or "").strip())

    if ov_token_usage:
        token_emitted = int(ov_token_usage.get("llm_total", 0) or 0) > 0 or int(
            ov_token_usage.get("embedding", 0) or 0
        ) > 0
        memory_written = int(ov_token_usage.get("memories", 0) or 0) > 0 or int(
            ov_token_usage.get("memory_write", 0) or 0
        ) > 0 or int(ov_token_usage.get("memory_edit", 0) or 0) > 0

    if consistency:
        index_available = bool(consistency.get("ok", False))

    if qa_commit_skipped and int(direct_recall_count or 0) > 0:
        memory_written = True
        index_available = True
        recall_hit = True
        state = "qa_direct_recall_only"
    elif qa_commit_skipped:
        state = "qa_direct_recall_miss"
    elif not token_emitted and not memory_written:
        state = "no_memory_signal"
    elif token_emitted and not memory_written:
        state = "token_emitted_only"
    elif token_emitted and memory_written and recall_hit and answered and not index_available:
        state = "memory_recalled_with_consistency_gap"
    elif token_emitted and memory_written and recall_hit and answered:
        state = "memory_closed_loop_ready"
    elif token_emitted and memory_written and not index_available:
        state = "memory_written_but_index_unavailable"
    elif token_emitted and memory_written and index_available:
        state = "memory_closed_loop_ready"
    else:
        state = "partial_memory_signal"

    return {
        "memory_written": str(memory_written).lower(),
        "token_emitted": str(token_emitted).lower(),
        "index_available": str(index_available).lower(),
        "recall_hit": str(recall_hit).lower(),
        "closure_state": state,
    }


def query_ov_search_find_total(
    ov_api_url: str,
    query: str,
    target_uri: str,
    *,
    state_dir: str = "",
    fallback_agent_id: str = "main",
    limit: int = 5,
    request_headers_fn: RequestHeadersFn = ov_request_headers,
    requests_module=requests,
) -> int:
    headers = request_headers_fn(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return 0
    try:
        resp = requests_module.post(
            f"{ov_api_url}/api/v1/search/find",
            headers=headers,
            json={"query": query, "target_uri": target_uri, "limit": limit},
            timeout=60,
        )
        if not resp.ok:
            return 0
        payload = resp.json()
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        return int(result.get("total", 0) or 0)
    except Exception:
        return 0


def query_ov_search_find_memories(
    ov_api_url: str,
    query: str,
    target_uri: str,
    *,
    state_dir: str = "",
    fallback_agent_id: str = "main",
    limit: int = 3,
    request_headers_fn: RequestHeadersFn = ov_request_headers,
    read_content_fn: Callable[..., str] = read_content_by_uri,
    requests_module=requests,
) -> list[dict]:
    headers = request_headers_fn(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return []

    try:
        resp = requests_module.post(
            f"{ov_api_url}/api/v1/search/find",
            headers=headers,
            json={
                "query": query,
                "target_uri": target_uri,
                "limit": max(limit, 1),
                "score_threshold": 0,
            },
            timeout=60,
        )
        if not resp.ok:
            return []
        payload = resp.json()
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        memories = result.get("memories", [])
        if not isinstance(memories, list):
            return []
        enriched = []
        for item in memories:
            if not isinstance(item, dict):
                continue
            memory = dict(item)
            uri = str(memory.get("uri") or "").strip()
            abstract = str(memory.get("abstract") or "").strip()
            if uri.endswith("/.overview.md"):
                continue
            if "[Directory abstract is not ready]" in abstract:
                continue
            has_text = any(
                isinstance(memory.get(key), str) and memory.get(key).strip()
                for key in ("normalized_summary", "summary", "excerpt", "text", "content", "body", "markdown")
            )
            if uri and not has_text:
                content = read_content_fn(ov_api_url, headers, uri, requests_module=requests_module)
                if content:
                    memory["content"] = content
            enriched.append(memory)
        return enriched
    except Exception:
        return []


def reindex_ov_memory_root(
    *,
    ov_api_url: str,
    user_id: str,
    state_dir: str = "",
    fallback_agent_id: str = "main",
    timeout: float = 120.0,
    request_headers_fn: RequestHeadersFn = ov_request_headers,
    requests_module=requests,
    time_module=time,
) -> dict[str, Any]:
    headers = request_headers_fn(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return {"ok": False, "last_error": "missing_headers"}
    payload = {
        "uri": f"viking://user/{user_id}/memories",
        "mode": "vectors_only",
        "wait": True,
    }
    deadline = time_module.monotonic() + max(timeout, 1.0)
    last_error = ""
    while time_module.monotonic() < deadline:
        try:
            resp = requests_module.post(
                f"{ov_api_url.rstrip('/')}/api/v1/content/reindex",
                headers=headers,
                json=payload,
                timeout=max(30.0, timeout),
            )
            data = resp.json() if resp.content else {}
            if resp.ok:
                return {
                    "ok": True,
                    "target_uri": payload["uri"],
                    "result": data.get("result", data) if isinstance(data, dict) else data,
                }
            last_error = (
                data.get("error", {}).get("message")
                if isinstance(data, dict)
                else ""
            ) or resp.text or f"HTTP {resp.status_code}"
            if resp.status_code == 409 and "tree lock" in last_error.lower():
                time_module.sleep(2.0)
                continue
            return {
                "ok": False,
                "target_uri": payload["uri"],
                "last_error": last_error,
            }
        except Exception as exc:
            last_error = str(exc)
            time_module.sleep(2.0)
    return {
        "ok": False,
        "target_uri": payload["uri"],
        "last_error": last_error or "timeout",
    }


# Backward-compatible private aliases for code that still imports old helper names.
_merge_ov_token_usage = merge_ov_token_usage
_parse_ov_task_result = parse_ov_task_result
_is_empty_ov_token_usage = is_empty_ov_token_usage
_load_openviking_plugin_config = load_openviking_plugin_config
_resolve_openviking_agent_header = resolve_openviking_agent_header
_ov_request_headers = ov_request_headers
_ov_read_content_by_uri = read_content_by_uri
