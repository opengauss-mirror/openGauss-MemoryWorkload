"""Ingest + QA evaluation — unified 6A/6B via SessionPolicy."""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock

import requests

from .config import Config, SessionPolicy

# ---------------------------------------------------------------------------
# LoCoMo JSON parsing
# ---------------------------------------------------------------------------

def format_locomo_message(msg: dict) -> str:
    speaker = msg.get("speaker", "unknown")
    text = msg.get("text", "")
    line = f"{speaker}: {text}"
    img_urls = msg.get("img_url", [])
    if isinstance(img_urls, str):
        img_urls = [img_urls]
    blip = msg.get("blip_caption", "")
    if img_urls:
        for url in img_urls:
            caption = f": {blip}" if blip else ""
            line += f"\n{url}{caption}"
    elif blip:
        line += f"\n({blip})"
    return line


def load_locomo_data(path: str, samples: list[int] | None = None) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if samples is not None:
        result = []
        for idx in samples:
            if idx < 0 or idx >= len(data):
                print(f"Error: sample index {idx} out of range (0-{len(data)-1})", file=sys.stderr)
                sys.exit(1)
            result.append(data[idx])
        return result
    return data


def _safe_session_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return safe or "unknown"


def build_ingest_session_key(sample_id: str, session_key: str) -> str:
    return f"ingest-{_safe_session_part(str(sample_id))}-{_safe_session_part(session_key)}"


def build_session_messages(
    item: dict,
    tail: str = "[]",
) -> list[dict]:
    conv = item["conversation"]
    speakers = f"{conv['speaker_a']} & {conv['speaker_b']}"
    session_keys = sorted(
        [k for k in conv if k.startswith("session_") and not k.endswith("_date_time")],
        key=lambda k: int(k.split("_")[1]),
    )
    sessions = []
    for sk in session_keys:
        dt_key = f"{sk}_date_time"
        date_time = conv.get(dt_key, "")
        parts = [f"[group chat conversation: {date_time}]"]
        for msg in conv[sk]:
            parts.append(format_locomo_message(msg))
        if tail:
            parts.append(tail)
        combined = "\n\n".join(parts)
        sessions.append({
            "message": combined,
            "meta": {
                "sample_id": item["sample_id"],
                "session_key": sk,
                "date_time": date_time,
                "speakers": speakers,
            },
        })
    return sessions


# ---------------------------------------------------------------------------
# Question time helpers
# ---------------------------------------------------------------------------

def parse_locomo_datetime(date_str: str) -> datetime | None:
    try:
        if " on " in date_str:
            date_part = date_str.split(" on ")[-1]
            return datetime.strptime(date_part.strip(), "%d %B, %Y")
    except ValueError:
        pass
    return None


def get_sample_question_time(sample: dict) -> str | None:
    conversation = sample.get("conversation", {})
    session_keys = [
        k for k in conversation.keys() if k.startswith("session_") and "date_time" not in k
    ]
    if not session_keys:
        return None

    def get_num(key):
        try:
            return int(key.replace("session_", ""))
        except ValueError:
            return 0

    session_keys.sort(key=get_num, reverse=True)
    for sk in session_keys:
        if conversation.get(sk):
            num = get_num(sk)
            dt_key = f"session_{num}_date_time"
            date_str = conversation.get(dt_key)
            if date_str:
                dt = parse_locomo_datetime(date_str)
                if dt:
                    return dt.strftime("%Y-%m-%d")
    return None


# ---------------------------------------------------------------------------
# Ingest record (avoid duplicate ingestion)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# OpenClaw state dir helpers
# ---------------------------------------------------------------------------

def get_session_id_from_key(session_key: str, user: str, agent_id: str = "main", state_dir: str = "") -> tuple[str, str] | None:
    """Find session file by key. Returns (session_file, sessions_dir) or None."""
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
                    sf = value.get("sessionFile")
                    if sf:
                        return sf, sessions_dir
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
                    eu = entry.get("message", {}).get("usage", {})
                    usage["input_tokens"] += eu.get("input", 0)
                    usage["output_tokens"] += eu.get("output", 0)
                    usage["cacheRead"] += eu.get("cacheRead", 0)
                    usage["cacheWrite"] += eu.get("cacheWrite", 0)
                    usage["total_tokens"] += eu.get("totalTokens", 0)
    except (json.JSONDecodeError, IOError):
        pass
    return usage


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def extract_response_text(response_json: dict) -> str:
    try:
        for item in response_json.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        return content.get("text", "")
        for item in response_json.get("output", []):
            if "text" in item:
                return item["text"]
            for content in item.get("content", []):
                if "text" in content:
                    return content["text"]
    except (KeyError, TypeError, IndexError):
        pass
    return f"[ERROR: could not extract text from response]"


def send_message(
    base_url: str, token: str, user: str, message: str,
    agent_id: str = "main", session_key: str | None = None,
) -> tuple[str, dict]:
    url = f"{base_url}/v1/responses"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-OpenClaw-Agent-ID": agent_id,
    }
    if session_key:
        headers["X-OpenClaw-Session-Key"] = session_key
    payload = {"model": "openclaw", "input": message, "stream": False}
    if user:
        payload["user"] = user

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=6000)
        resp.raise_for_status()
        body = resp.json()
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"Connection error to {base_url}: {e}")
    except requests.exceptions.Timeout as e:
        raise RuntimeError(f"Request timeout to {base_url}: {e}")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"HTTP error {e.response.status_code}: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Error parsing response: {e}")

    usage = body.get("usage", {"input_tokens": 0, "output_tokens": 0, "cacheRead": 0, "total_tokens": 0})
    return extract_response_text(body), usage


def send_message_with_retry(
    base_url: str, token: str, user: str, message: str, retries: int = 2,
    agent_id: str = "main", session_key: str | None = None,
) -> tuple[str, dict]:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return send_message(base_url, token, user, message, agent_id, session_key)
        except Exception as e:
            last_exc = e
            if attempt < retries:
                wait = 3 * (attempt + 1)
                print(f"    [retry {attempt + 1}/{retries}] {e} (waiting {wait}s)", file=sys.stderr)
                time.sleep(wait)
    raise last_exc


# ---------------------------------------------------------------------------
# OV Task API helpers
# ---------------------------------------------------------------------------

def _parse_ov_task_result(data: dict) -> dict | None:
    result = data.get("result", {})
    if isinstance(result, dict) and "result" in result:
        result = result["result"]
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


def query_ov_task_token_usage(ov_api_url: str, task_id: str, max_wait: int = 60) -> dict | None:
    deadline = time.time() + max_wait
    interval = 2
    try:
        while True:
            resp = requests.get(f"{ov_api_url}/api/v1/tasks/{task_id}", timeout=30)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("result", {}).get("status", "") if isinstance(data.get("result"), dict) else ""
            if status in ("completed", "failed", ""):
                return _parse_ov_task_result(data)
            if time.time() >= deadline:
                return _parse_ov_task_result(data)
            time.sleep(interval)
            interval = min(interval * 2, 10)
    except Exception as e:
        print(f"    [ov-task] Error querying task {task_id}: {e}", file=sys.stderr)
        return None


def query_ov_latest_task(ov_api_url: str, resource_id: str | None = None) -> dict | None:
    try:
        params = {"task_type": "session_commit", "status": "completed", "limit": 1}
        if resource_id:
            params["resource_id"] = resource_id
        resp = requests.get(f"{ov_api_url}/api/v1/tasks", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        tasks = data.get("result", [])
        if tasks:
            task = tasks[0]
            result = _parse_ov_task_result({"result": task})
            if result:
                result["task_id"] = task.get("task_id", "")
            return result
    except Exception as e:
        print(f"    [ov-task] Error querying latest task: {e}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# oGMemory API / log helpers
# ---------------------------------------------------------------------------

OGMEM_EXTRACT_LOG_MARKER = "after_turn background extract done"


def query_ogmem_token_stats(ogmem_api_url: str) -> dict:
    """Read cumulative oGMemory token stats."""
    try:
        resp = requests.get(f"{ogmem_api_url}/api/v1/token_stats", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"    [ogmem-token] Error querying token stats: {e}", file=sys.stderr)
        return {}


def _ogmem_token_delta(before: dict, after: dict) -> dict:
    before_llm = before.get("llm", {}) if isinstance(before, dict) else {}
    after_llm = after.get("llm", {}) if isinstance(after, dict) else {}
    before_embed = before.get("embedding", {}) if isinstance(before, dict) else {}
    after_embed = after.get("embedding", {}) if isinstance(after, dict) else {}

    llm_prompt = int(after_llm.get("input_tokens", 0) or 0) - int(before_llm.get("input_tokens", 0) or 0)
    llm_completion = int(after_llm.get("output_tokens", 0) or 0) - int(before_llm.get("output_tokens", 0) or 0)
    llm_total = int(after_llm.get("total_tokens", 0) or 0) - int(before_llm.get("total_tokens", 0) or 0)
    embedding = int(after_embed.get("total_tokens", 0) or 0) - int(before_embed.get("total_tokens", 0) or 0)
    llm_calls = int(after_llm.get("calls", 0) or 0) - int(before_llm.get("calls", 0) or 0)
    embedding_calls = int(after_embed.get("calls", 0) or 0) - int(before_embed.get("calls", 0) or 0)

    return {
        "provider": "ogmem",
        "llm_prompt": max(0, llm_prompt),
        "llm_completion": max(0, llm_completion),
        "llm_total": max(0, llm_total),
        "embedding": max(0, embedding),
        "memories": 0,
        "llm_calls": max(0, llm_calls),
        "embedding_calls": max(0, embedding_calls),
    }


def _empty_memory_token_totals(provider: str) -> dict:
    return {
        "provider": provider,
        "llm_prompt": 0,
        "llm_completion": 0,
        "llm_total": 0,
        "embedding": 0,
        "memories": 0,
        "llm_calls": 0,
        "embedding_calls": 0,
    }


def _add_memory_token_usage(total: dict, delta: dict | None) -> None:
    if not delta:
        return
    for key in ("llm_prompt", "llm_completion", "llm_total", "embedding", "memories", "llm_calls", "embedding_calls"):
        total[key] = int(total.get(key, 0) or 0) + int(delta.get(key, 0) or 0)


def count_ogmem_after_turn_extract_logs(
    container: str = "ogmem",
    log_tail: int = 500,
    since: float | None = None,
) -> int:
    """Count oGMemory background extraction completion log lines."""
    cmd = ["docker", "logs", "--tail", str(log_tail)]
    if since is not None:
        cmd.extend(["--since", str(max(0, int(since)))])
    cmd.append(container)
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout.strip() or f"docker logs failed for {container}")
    return sum(1 for line in proc.stdout.splitlines() if OGMEM_EXTRACT_LOG_MARKER in line)


def wait_for_ogmem_after_turn_extract(
    *,
    container: str,
    session_key: str,
    baseline_count: int,
    timeout: int,
    interval: float,
    log_tail: int = 500,
    since: float | None = None,
) -> dict:
    """Wait until oGMemory logs one more background extraction completion."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        current_count = count_ogmem_after_turn_extract_logs(container, log_tail, since=since)
        if current_count > (0 if since is not None else baseline_count):
            print(f"    [ogmem] after_turn background extract done ({session_key})", file=sys.stderr)
            return {"completed": True, "baseline_count": baseline_count, "current_count": current_count}
        time.sleep(interval)
    raise RuntimeError(
        f"Timed out waiting for oGMemory extract completion for {session_key}. "
        f"Check: docker logs --tail {log_tail} {container} 2>&1 | grep '{OGMEM_EXTRACT_LOG_MARKER}'"
    )


# ---------------------------------------------------------------------------
# OpenClaw compact via WebSocket RPC
# ---------------------------------------------------------------------------

def trigger_openclaw_compact(
    base_url: str, token: str, session_key: str, timeout: int = 300,
) -> dict:
    import websocket

    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws = websocket.create_connection(ws_url, timeout=timeout)

    try:
        challenge = json.loads(ws.recv())
        if challenge.get("event") != "connect.challenge":
            raise RuntimeError(f"[compact] Expected connect.challenge, got: {challenge}")

        connect_id = str(uuid.uuid4())
        ws.send(json.dumps({
            "type": "req", "id": connect_id, "method": "connect",
            "params": {
                "minProtocol": 4, "maxProtocol": 4,
                "client": {"id": "openclaw-control-ui", "version": "1.0.0", "platform": sys.platform, "mode": "webchat"},
                "scopes": ["operator.admin", "operator.read", "operator.write"],
                "auth": {"token": token},
            },
        }))

        while True:
            msg = json.loads(ws.recv())
            if msg.get("type") == "res" and msg.get("id") == connect_id:
                if not msg.get("ok"):
                    raise RuntimeError(f"[compact] Handshake rejected: {msg.get('error', msg)}")
                break

        compact_id = str(uuid.uuid4())
        ws.send(json.dumps({
            "type": "req", "id": compact_id,
            "method": "sessions.compact", "params": {"key": session_key},
        }))

        while True:
            msg = json.loads(ws.recv())
            if msg.get("type") == "res" and msg.get("id") == compact_id:
                payload = msg.get("payload", {})
                if msg.get("ok"):
                    compacted = payload.get("compacted", False)
                    print(f"    [compact] OK (compacted={compacted})", file=sys.stderr)
                    if not compacted:
                        raise RuntimeError(f"[compact] compact returned compacted=False, memory extraction did not run")
                else:
                    raise RuntimeError(f"[compact] Failed: {msg.get('error', {})}")
                return payload
    finally:
        try:
            ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "sample_id", "sample_idx", "qi", "question", "expected",
    "response", "category", "evidence", "input_tokens",
    "output_tokens", "cacheRead", "cacheWrite", "total_tokens",
    "timestamp", "jsonl_filename", "result", "reasoning",
]

csv_lock = Lock()


def load_executed_records(csv_path: str) -> set:
    executed = set()
    if os.path.exists(csv_path):
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    executed.add((row["sample_id"], int(row["qi"])))
        except (csv.Error, IOError, KeyError, ValueError):
            pass
    return executed


def save_record_to_csv(csv_path: str, record: dict) -> None:
    file_exists = os.path.exists(csv_path)
    flat = record.copy()
    usage = flat.pop("usage", {})
    flat["input_tokens"] = usage.get("input_tokens", 0)
    flat["output_tokens"] = usage.get("output_tokens", 0)
    flat["cacheRead"] = usage.get("cacheRead", 0)
    flat["cacheWrite"] = usage.get("cacheWrite", 0)
    flat["total_tokens"] = usage.get("total_tokens", 0)
    flat["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    flat.setdefault("jsonl_filename", "")
    flat.setdefault("result", "")
    flat.setdefault("reasoning", "")
    try:
        with open(csv_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(flat)
            f.flush()
    except (csv.Error, IOError) as e:
        print(f"Warning: Error writing CSV: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def run_ingest(cfg: Config, output_dir: str) -> tuple[list[dict], dict]:
    """Load conversations into OpenClaw. Returns (result entries, memory_token_totals)."""
    record_path = os.path.join(output_dir, ".ingest_record.json")
    ingest_record = load_ingest_record(record_path)

    samples = load_locomo_data(cfg.data_file, cfg.samples)
    results = []
    skipped = 0
    policy = cfg.session.policy
    memory_token_totals = _empty_memory_token_totals(cfg.memory_mode)

    for item in samples:
        sample_id = item["sample_id"]
        user_key = cfg.user
        sessions = build_session_messages(item, tail=cfg.session.tail)

        print(f"\n=== Sample {sample_id} ===", file=sys.stderr)
        print(f"    user: {user_key}, agent: {cfg.agent_id}, policy: {policy.value}", file=sys.stderr)
        print(f"    {len(sessions)} session(s) to ingest", file=sys.stderr)

        for sess in sessions:
            meta = sess["meta"]
            msg = sess["message"]
            label = f"{meta['session_key']} ({meta['date_time']})"

            # Session key logic based on policy. oGMemory ingest is always
            # isolated so each LoCoMo session can be extracted and observed
            # independently before the next session starts.
            oc_session_key = None
            if policy == SessionPolicy.ISOLATED or cfg.memory_mode == "ogmem":
                oc_session_key = build_ingest_session_key(sample_id, meta["session_key"])

            if is_already_ingested(cfg.agent_id, user_key, sample_id, meta["session_key"], ingest_record):
                print(f"  [{label}] [SKIP] already ingested", file=sys.stderr)
                skipped += 1
                continue

            preview = msg.replace("\n", " | ")[:80]
            print(f"  [{label}] {preview}...", file=sys.stderr)
            if oc_session_key:
                print(f"    [session-key] {oc_session_key}", file=sys.stderr)

            try:
                ingest_msg = msg
                if cfg.memory_mode == "memcore":
                    memory_prompt = (
                        "Extract key facts from the next group conversation and store them "
                        "in a SEPARATE memory file named memory/YYYY-MM-DD.md where YYYY-MM-DD "
                        "is the CONVERSATION date (from the message header, NOT today). "
                        "Use the write tool immediately. Do not append to existing files, "
                        "create a new file per conversation date.\n\n"
                    )
                    ingest_msg = memory_prompt + msg

                ogmem_log_baseline = None
                ogmem_tokens_before = None
                if cfg.memory_mode == "ogmem":
                    ogmem_wait_since = time.time()
                    ogmem_log_baseline = count_ogmem_after_turn_extract_logs(
                        cfg.ogmem.docker_container,
                        cfg.ogmem.log_tail,
                    )
                    ogmem_tokens_before = query_ogmem_token_stats(cfg.ogmem.api_url)

                reply, usage = send_message_with_retry(
                    cfg.gateway.base_url, cfg.gateway.token, user_key,
                    ingest_msg, 2, cfg.agent_id, oc_session_key,
                )
                print(f"    -> {reply[:80]}{'...' if len(reply) > 80 else ''}", file=sys.stderr)

                memory_token_usage = None
                ov_token_usage = None
                if cfg.memory_mode == "openviking":
                    compact_key = oc_session_key or f"agent:{cfg.agent_id}:openresponses-user:{user_key}"
                    compact_result = trigger_openclaw_compact(cfg.gateway.base_url, cfg.gateway.token, compact_key)
                    if compact_result and compact_result.get("compacted") and cfg.openviking.api_url:
                        task_id = compact_result.get("taskId")
                        if task_id:
                            ov_token_usage = query_ov_task_token_usage(cfg.openviking.api_url, task_id)
                        if not ov_token_usage:
                            sid = get_session_id(user_key, cfg.agent_id, cfg.gateway.state_dir)
                            ov_token_usage = query_ov_latest_task(cfg.openviking.api_url, resource_id=sid)
                        if ov_token_usage:
                            print(f"    [ov-task] llm={ov_token_usage['llm_total']:,} embed={ov_token_usage['embedding']:,} memories={ov_token_usage['memories']}", file=sys.stderr)
                            memory_token_usage = {"provider": "openviking", **ov_token_usage}
                elif cfg.memory_mode == "ogmem":
                    wait_for_ogmem_after_turn_extract(
                        container=cfg.ogmem.docker_container,
                        session_key=oc_session_key or meta["session_key"],
                        baseline_count=ogmem_log_baseline or 0,
                        timeout=cfg.ogmem.wait_timeout,
                        interval=cfg.ogmem.wait_interval,
                        log_tail=cfg.ogmem.log_tail,
                        since=ogmem_wait_since,
                    )
                    ogmem_tokens_after = query_ogmem_token_stats(cfg.ogmem.api_url)
                    memory_token_usage = _ogmem_token_delta(ogmem_tokens_before or {}, ogmem_tokens_after)
                    print(
                        f"    [ogmem-token] llm={memory_token_usage['llm_total']:,} "
                        f"embed={memory_token_usage['embedding']:,}",
                        file=sys.stderr,
                    )

                result_entry = {
                    "sample_id": sample_id, "session": meta["session_key"],
                    "user": user_key, "reply": reply, "usage": usage,
                }
                if ov_token_usage:
                    result_entry["ov_token_usage"] = ov_token_usage
                if memory_token_usage:
                    result_entry["memory_token_usage"] = memory_token_usage
                    _add_memory_token_usage(memory_token_totals, memory_token_usage)
                results.append(result_entry)

                mark_ingested(cfg.agent_id, user_key, sample_id, meta["session_key"], ingest_record, {
                    "date_time": meta["date_time"], "usage": usage,
                })
            except Exception as e:
                print(f"    -> [FATAL] Ingest failed, aborting: {e}", file=sys.stderr)
                raise RuntimeError(f"Ingest failed for sample {sample_id} session {meta['session_key']}: {e}") from e

            # Archive session (isolated policy) or keep alive (shared).
            # For ogmem, oc_session_key is always set above to keep each ingest
            # session isolated even when QA policy is shared.
            if (policy == SessionPolicy.ISOLATED or cfg.memory_mode == "ogmem") and oc_session_key:
                found = get_session_id_from_key(oc_session_key, user_key, cfg.agent_id, cfg.gateway.state_dir)
                if found:
                    sf, sdir = found
                    sf_path = sf if os.path.isabs(sf) else os.path.join(sdir, sf)
                    if not sf_path.endswith(".jsonl"):
                        sf_path += ".jsonl"
                    reset_session(sf_path, cfg.agent_id, cfg.gateway.state_dir)
            elif policy == SessionPolicy.SHARED and cfg.memory_mode not in ("openviking", "ogmem"):
                sid = get_session_id(user_key, cfg.agent_id, cfg.gateway.state_dir)
                if sid:
                    reset_session(sid, cfg.agent_id, cfg.gateway.state_dir)

    save_ingest_record(ingest_record, record_path)
    print(f"\n=== Ingest summary: {len(results)} completed, {skipped} skipped ===", file=sys.stderr)

    if memory_token_totals["llm_total"] or memory_token_totals["embedding"]:
        label = "OV" if memory_token_totals.get("provider") == "openviking" else "oGMemory"
        print(
            f"  {label} totals: llm={memory_token_totals['llm_total']:,} "
            f"embed={memory_token_totals['embedding']:,} memories={memory_token_totals['memories']}",
            file=sys.stderr,
        )

    # Memory index warmup for memcore
    if cfg.memory_mode == "memcore" and results:
        print("Triggering memory index build...", file=sys.stderr)
        try:
            send_message(cfg.gateway.base_url, cfg.gateway.token, f"_warmup_{cfg.user}", "Search your memory.", cfg.agent_id)
        except Exception as e:
            print(f"  Index warmup failed (non-fatal): {e}", file=sys.stderr)

    return results, memory_token_totals


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------

def _process_single_question(
    sample_id: str, sample_idx: int, qi: int, qa: dict,
    cfg: Config, csv_path: str, question_time: str | None,
) -> dict:
    question = qa["question"]
    expected = str(qa["answer"])
    category = qa.get("category", "")
    evidence = qa.get("evidence", [])
    user_key = cfg.user
    policy = cfg.session.policy

    # Session strategy based on policy
    if policy == SessionPolicy.SHARED:
        qa_user = user_key
        session_key = None
    else:
        qa_user = str(sample_id)
        session_key = f"qa-{sample_id}-q{qi}"

    print(f"  [{sample_idx}] Q{qi}: {question[:60]}{'...' if len(question) > 60 else ''}", file=sys.stderr)

    qa_prompt_prefix = os.environ.get("LOCOMO_QA_PROMPT_PREFIX", "")
    if question_time:
        input_msg = f"{qa_prompt_prefix}Current date: {question_time}. Answer the question directly: {question}"
    else:
        input_msg = f"{qa_prompt_prefix}Answer the question directly: {question}"

    jsonl_filename = ""
    try:
        response, api_usage = send_message_with_retry(
            cfg.gateway.base_url, cfg.gateway.token, qa_user,
            input_msg, 2, cfg.agent_id, session_key,
        )
        print(f"  [{sample_idx}]   A: {response[:60]}{'...' if len(response) > 60 else ''}", file=sys.stderr)

        # Token usage: read from JSONL first (has cacheRead), then archive
        qa_sessions_dir = ""
        jsonl_path = ""
        if policy == SessionPolicy.ISOLATED and session_key:
            found = get_session_id_from_key(session_key, user_key, cfg.agent_id, cfg.gateway.state_dir)
            if found:
                sf, qa_sessions_dir = found
                # sf may be absolute path or just filename; may or may not have .jsonl
                if os.path.isabs(sf):
                    jsonl_path = sf if sf.endswith(".jsonl") else f"{sf}.jsonl"
                else:
                    jsonl_path = os.path.join(qa_sessions_dir, sf if sf.endswith(".jsonl") else f"{sf}.jsonl")

        if jsonl_path and os.path.exists(jsonl_path):
            usage = calculate_usage_from_jsonl(jsonl_path)
            # Now archive the session
            jsonl_filename = reset_session(jsonl_path, cfg.agent_id, cfg.gateway.state_dir) or ""
        else:
            usage = {
                "input_tokens": api_usage.get("input_tokens", 0),
                "output_tokens": api_usage.get("output_tokens", 0),
                "cacheRead": api_usage.get("cacheRead", 0),
                "cacheWrite": api_usage.get("cacheWrite", 0),
                "total_tokens": api_usage.get("total_tokens", 0),
            }
    except Exception as e:
        print(f"  [{sample_idx}]   [FATAL] QA failed: {e}", file=sys.stderr)
        raise

    record = {
        "sample_id": sample_id, "sample_idx": sample_idx, "qi": qi,
        "question": question, "expected": expected, "response": response,
        "category": category, "evidence": evidence,
        "usage": usage, "jsonl_filename": jsonl_filename,
    }

    with csv_lock:
        save_record_to_csv(csv_path, record)
    return record


def run_qa(cfg: Config, output_dir: str) -> dict:
    """Run QA questions. Returns total usage dict."""
    samples = load_locomo_data(cfg.data_file, cfg.samples)

    parallel = max(1, min(10, cfg.parallel))
    # Shared policy forces serial QA (concurrent writes to same session would race)
    if cfg.session.policy == SessionPolicy.SHARED and parallel > 1:
        print(f"    [wm] shared session forces parallel=1 (was {parallel})", file=sys.stderr)
        parallel = 1

    csv_path = os.path.join(output_dir, "qa_results.csv")
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    executed = load_executed_records(csv_path)
    print(f"    Loaded {len(executed)} already executed records", file=sys.stderr)
    print(f"    Running with {parallel} concurrent workers", file=sys.stderr)

    total_usage = {"input_tokens": 0, "output_tokens": 0, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 0}

    for idx, item in enumerate(samples):
        sample_id = item["sample_id"]
        question_time = get_sample_question_time(item)
        qas = [q for q in item.get("qa", []) if str(q.get("category", "")) != "5"]
        if cfg.count is not None:
            qas = qas[:cfg.count]

        pending = [(qi, qa) for qi, qa in enumerate(qas, start=1) if (sample_id, qi) not in executed]
        if not pending:
            print(f"\n=== Sample {sample_id} [{idx+1}]: all QA done, skipping ===", file=sys.stderr)
            continue

        print(f"\n=== Sample {sample_id} [{idx+1}] ({len(pending)} questions) ===", file=sys.stderr)
        if question_time:
            print(f"    Question time: {question_time}", file=sys.stderr)

        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = []
            for qi, qa in pending:
                f = executor.submit(
                    _process_single_question,
                    sample_id, idx + 1, qi, qa, cfg, csv_path, question_time,
                )
                futures.append(f)
            for f in as_completed(futures):
                record = f.result()
                u = record.get("usage", {})
                for k in total_usage:
                    total_usage[k] += u.get(k, 0)

    print(f"\n    Total tokens: in={total_usage['input_tokens']} out={total_usage['output_tokens']} total={total_usage['total_tokens']}", file=sys.stderr)
    return total_usage
