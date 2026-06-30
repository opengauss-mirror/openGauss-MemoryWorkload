from __future__ import annotations

import json
import os
import re
import sys
import time


def load_ingest_record(record_path: str) -> dict:
    try:
        with open(record_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, IOError):
        return {}


def save_ingest_record(record: dict, record_path: str) -> None:
    try:
        with open(record_path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
    except IOError as e:
        print(f"Warning: Error saving ingest record: {e}", file=sys.stderr)


def is_already_ingested(agent_id: str, user_key: str, sample_id, session_key: str, record: dict) -> bool:
    key = f"{agent_id}:{user_key}:{sample_id}:{session_key}"
    return key in record and record[key].get("success", False)


def mark_ingested(agent_id: str, user_key: str, sample_id, session_key: str, record: dict, meta: dict | None = None):
    key = f"{agent_id}:{user_key}:{sample_id}:{session_key}"
    record[key] = {"success": True, "timestamp": int(time.time()), "meta": meta or {}}


def get_session_id_from_key(session_key: str, user: str, agent_id: str = "main", state_dir: str = "") -> tuple[str, str] | None:
    agents_base_dir = os.path.join(state_dir, "agents")
    if not os.path.exists(agents_base_dir):
        return None
    for agent_name in os.listdir(agents_base_dir):
        agent_dir = os.path.join(agents_base_dir, agent_name)
        if not os.path.isdir(agent_dir):
            continue
        sessions_dir = os.path.join(agent_dir, "sessions")
        sessions_file = os.path.join(sessions_dir, "sessions.json")
        if not os.path.exists(sessions_file):
            continue
        try:
            with open(sessions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, value in data.items():
                if session_key in key and isinstance(value, dict):
                    session_file = value.get("sessionFile")
                    if session_file:
                        return session_file, sessions_dir
        except (json.JSONDecodeError, IOError):
            continue
    return None


def get_session_id(user: str, agent_id: str = "main", state_dir: str = "") -> str | None:
    sessions_file = os.path.join(state_dir, "agents", agent_id, "sessions", "sessions.json")
    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = f"agent:{agent_id}:openresponses-user:{user}"
        return data.get(key, {}).get("sessionId")
    except (FileNotFoundError, json.JSONDecodeError, IOError):
        return None


def wait_for_session_id_from_key(
    session_key: str,
    user: str,
    agent_id: str = "main",
    state_dir: str = "",
    *,
    timeout: float = 10.0,
    interval: float = 0.5,
) -> tuple[str, str] | None:
    deadline = time.monotonic() + max(timeout, 0.1)
    while True:
        found = get_session_id_from_key(session_key, user, agent_id, state_dir)
        if found:
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(interval, 0.05))


def find_latest_session_file(agent_id: str = "main", state_dir: str = "") -> tuple[str, str] | None:
    agents_base_dir = os.path.join(state_dir, "agents")
    if not os.path.isdir(agents_base_dir):
        return None
    agent_names = []
    if agent_id:
        agent_names.append(agent_id)
    for name in os.listdir(agents_base_dir):
        if name not in agent_names:
            agent_names.append(name)
    candidates = []
    for current_agent in agent_names:
        sessions_dir = os.path.join(agents_base_dir, current_agent, "sessions")
        if not os.path.isdir(sessions_dir):
            continue
        for name in os.listdir(sessions_dir):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(sessions_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            priority = 0 if current_agent == agent_id else 1
            candidates.append((priority, mtime, name, sessions_dir))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]))
    _, _, name, sessions_dir = candidates[0]
    return name, sessions_dir


def find_session_id_in_openclaw_log(
    session_key: str,
    *,
    agent_id: str = "main",
    state_dir: str = "",
    log_dir: str = "/tmp/openclaw",
) -> tuple[str, str] | None:
    if not session_key:
        return None
    log_path = os.path.join(log_dir, f"openclaw-{time.strftime('%Y-%m-%d')}.log")
    if not os.path.exists(log_path):
        return None
    sessions_dir = os.path.join(state_dir, "agents", agent_id, "sessions")
    session_id = ""
    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if session_key not in line:
                    continue
                match = re.search(r'"sessionId":"([^"]+)"', line)
                if match:
                    session_id = match.group(1).strip()
        if not session_id:
            return None
        return f"{session_id}.jsonl", sessions_dir
    except OSError:
        return None


def reset_session(session_path: str, agent_id: str = "main", state_dir: str = "") -> str | None:
    if os.path.isabs(session_path) and os.path.exists(session_path):
        src = session_path
    else:
        sessions_dir = os.path.join(state_dir, "agents", agent_id, "sessions")
        src = os.path.join(sessions_dir, f"{session_path}.jsonl")
    if not os.path.exists(src):
        return None
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    dst = f"{src}.{timestamp}"
    try:
        os.rename(src, dst)
        print(f"    [backup] renamed {os.path.basename(src)} -> {os.path.basename(dst)}", file=sys.stderr)
        return os.path.basename(dst)
    except IOError:
        return None


def calculate_usage_from_jsonl(jsonl_path: str) -> dict:
    usage = {"input_tokens": 0, "output_tokens": 0, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 0}
    if not os.path.exists(jsonl_path):
        return usage
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("type") == "message" and entry.get("message", {}).get("role") == "assistant":
                    entry_usage = _normalize_usage(entry.get("message", {}).get("usage", {}))
                    usage["input_tokens"] += entry_usage.get("input_tokens", 0)
                    usage["output_tokens"] += entry_usage.get("output_tokens", 0)
                    usage["cacheRead"] += entry_usage.get("cacheRead", 0)
                    usage["cacheWrite"] += entry_usage.get("cacheWrite", 0)
                    usage["total_tokens"] += entry_usage.get("total_tokens", 0)
    except (json.JSONDecodeError, IOError):
        pass
    return usage


def _normalize_usage(raw: dict | None) -> dict:
    raw = raw or {}
    input_tokens = int(
        raw.get("input_tokens", 0)
        or raw.get("prompt_tokens", 0)
        or raw.get("inputTokens", 0)
        or raw.get("input", 0)
        or 0
    )
    output_tokens = int(
        raw.get("output_tokens", 0)
        or raw.get("completion_tokens", 0)
        or raw.get("outputTokens", 0)
        or raw.get("output", 0)
        or 0
    )
    total_tokens = int(
        raw.get("total_tokens", 0)
        or raw.get("totalTokens", 0)
        or raw.get("total", 0)
        or (input_tokens + output_tokens)
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cacheRead": int(raw.get("cacheRead", 0) or raw.get("cache_read", 0) or 0),
        "cacheWrite": int(raw.get("cacheWrite", 0) or raw.get("cache_write", 0) or 0),
        "total_tokens": total_tokens,
    }
