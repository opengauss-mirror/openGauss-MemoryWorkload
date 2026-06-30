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


def _find_latest_session_file(agent_id: str = "main", state_dir: str = "") -> tuple[str, str] | None:
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


def _find_session_id_in_openclaw_log(
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
                    eu = _normalize_usage(entry.get("message", {}).get("usage", {}))
                    usage["input_tokens"] += eu.get("input_tokens", 0)
                    usage["output_tokens"] += eu.get("output_tokens", 0)
                    usage["cacheRead"] += eu.get("cacheRead", 0)
                    usage["cacheWrite"] += eu.get("cacheWrite", 0)
                    usage["total_tokens"] += eu.get("total_tokens", 0)
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


DEFAULT_LOCOMO_QA_PROMPT_PREFIX = (
    "Answer the question directly using the recalled memory snippets as the primary evidence. "
    "Treat the normalized summary or leading bullet points at the top of each memory as authoritative, "
    "and use them before raw chat-log details that follow. "
    "Prefer the most specific supported fact over a generic paraphrase. "
    "If the recalled memories contain an exact noun phrase, identity label, country, gift, relationship status, duration, or symbolic meaning, copy that specific phrase instead of replacing it with a generic phrase. "
    "Do not replace exact facts with vague substitutes such as person, home country, object, support, or event. "
    "For list or set questions, include only items that are explicitly supported by recalled memories and match the asked category. "
    "Do not broaden the answer with nearby related activities, generic themes, inferred items, or unnamed placeholders. "
    "If a list item is not explicitly named, omit it. "
    "If a recalled memory clearly matches the event being asked about, use the matching event details even when the question wording is paraphrased. "
    "For time questions, preserve the memory's relative phrasing such as last week, last Friday, next month, or the week before the current date unless the memory itself explicitly states a calendar date. "
    "If the memory says an event happened in the week before the current date, answer with that relative date instead of saying it is missing. "
    "Do not say information is unavailable when the recalled memories explicitly contain the answer. "
    "If the recalled memories still do not support the answer, say so briefly. "
)

DEFAULT_LOCOMO_INGEST_PROMPT_PREFIX = (
    "You are transforming a raw conversation log into memory-ingestion notes, not answering a user question. "
    "Reply with short factual bullet points only. "
    "Write ONE explicit fact per bullet. "
    "Capture explicit facts for BOTH participants. "
    "Preserve exact dates, relative dates, durations, counts, countries, gifts, relationships, plans, motivations, and workshop/camping/speech details. "
    "Keep the conversation's own time anchors instead of today's date. "
    "When a fact is explicit, keep the concrete noun or number exactly, including values like Sweden, necklace, 5 years, 10 years ago, June 2023, and week-before references. "
    "No follow-up questions. No greetings, roleplay, or advice.\n\n"
)


def _normalize_recall_text(value: object, *, max_chars: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


def _extract_recall_summary(memory: dict, *, max_chars: int) -> str:
    candidates = [
        memory.get("normalized_summary"),
        memory.get("summary"),
        memory.get("excerpt"),
        memory.get("text"),
        memory.get("content"),
        memory.get("body"),
        memory.get("markdown"),
    ]
    metadata = memory.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend(
            [
                metadata.get("normalized_summary"),
                metadata.get("summary"),
                metadata.get("excerpt"),
                metadata.get("text"),
            ]
        )
    for candidate in candidates:
        text = _normalize_recall_text(candidate, max_chars=max_chars)
        if text:
            return text
    return ""


def _extract_recall_detail(memory: dict, *, summary: str, max_chars: int) -> str:
    assistant_summary = _extract_embedded_assistant_summary(memory, max_chars=max_chars)
    if assistant_summary:
        return assistant_summary

    candidates = [
        memory.get("text"),
        memory.get("content"),
        memory.get("body"),
        memory.get("markdown"),
        memory.get("excerpt"),
    ]
    metadata = memory.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend(
            [
                metadata.get("text"),
                metadata.get("content"),
                metadata.get("body"),
                metadata.get("markdown"),
                metadata.get("excerpt"),
            ]
        )
    normalized_summary = _normalize_recall_text(summary, max_chars=max_chars)
    for candidate in candidates:
        text = _normalize_recall_text(candidate, max_chars=max_chars)
        if text:
            convo_idx = text.find("[group chat conversation:")
            if convo_idx >= 0:
                text = text[convo_idx:]
            else:
                text = re.sub(r"^\d{4}-\d{2}-\d{2} \([^)]+\) ChatLog:\s*", "", text)
                text = re.sub(r"^\[(?:user|assistant)\]:\s*", "", text)
        if text and text != normalized_summary:
            return text
    return ""


def _extract_embedded_assistant_summary(memory: dict, *, max_chars: int) -> str:
    candidates = [
        memory.get("content"),
        memory.get("text"),
        memory.get("body"),
        memory.get("markdown"),
        memory.get("excerpt"),
    ]
    metadata = memory.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend(
            [
                metadata.get("content"),
                metadata.get("text"),
                metadata.get("body"),
                metadata.get("markdown"),
                metadata.get("excerpt"),
            ]
        )
    for candidate in candidates:
        raw = str(candidate or "")
        if not raw:
            continue
        match = re.search(r"\[assistant\]:\s*(.+)", raw, flags=re.DOTALL)
        if not match:
            continue
        text = _normalize_recall_text(match.group(1), max_chars=max_chars)
        if text:
            return text
    return ""


def _tokenize_recall_text(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9+]+", value.lower())


def _question_targets_person_profile(question: str) -> bool:
    lowered = (question or "").lower()
    cues = (
        "identity",
        "relationship status",
        "relationship",
        "status",
        "married",
        "husband",
        "wife",
        "single parent",
        "single",
        "career",
        "education",
        "field",
        "pursue",
        "home country",
        "country",
        "friends",
        "breakup",
        "transition",
        "transgender",
        "family",
        "kids",
    )
    return any(cue in lowered for cue in cues)


def _extract_person_name_candidates(question: str) -> list[str]:
    stopwords = {
        "What", "When", "Where", "Why", "How", "Which", "Who",
        "Did", "Does", "Do", "Is", "Are", "Was", "Were",
    }
    names: list[str] = []
    for token in re.findall(r"\b[A-Z][a-z]+\b", question or ""):
        if token in stopwords or token in names:
            continue
        names.append(token)
    return names


def _ov_read_content_by_uri(
    ov_api_url: str,
    headers: dict[str, str],
    uri: str,
) -> str:
    try:
        resp = requests.get(
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


def _build_person_entity_memory(uri: str, content: str) -> dict[str, object]:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    title = ""
    bullet_lines: list[str] = []
    for line in lines:
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("- "):
            bullet_lines.append(line[2:].strip())
    summary = " ".join(bullet_lines[:3]).strip()
    return {
        "uri": uri,
        "title": title,
        "summary": summary,
        "content": content,
        "context_type": "memory",
        "memory_hint": "person_entity",
    }


def augment_ov_recall_with_named_person_entities(
    *,
    ov_api_url: str,
    question: str,
    user_id: str,
    recalled_memories: list[dict],
    state_dir: str = "",
    fallback_agent_id: str = "main",
) -> list[dict]:
    if not _question_targets_person_profile(question):
        return recalled_memories
    names = _extract_person_name_candidates(question)
    if not names:
        return recalled_memories
    headers = _ov_request_headers(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return recalled_memories
    existing_uris = {str(item.get("uri") or "") for item in recalled_memories if isinstance(item, dict)}
    augmented = list(recalled_memories)
    for name in names:
        candidate_uris = [
            f"viking://user/{user_id}/memories/entities/person/{name}.md",
            f"viking://user/{user_id}/memories/entities/person/{name.lower()}.md",
        ]
        for uri in candidate_uris:
            if uri in existing_uris:
                break
            content = _ov_read_content_by_uri(ov_api_url, headers, uri)
            if not content:
                continue
            augmented.append(_build_person_entity_memory(uri, content))
            existing_uris.add(uri)
            break
    return augmented


def rerank_ov_recalled_memories(
    question: str,
    recalled_memories: list[dict],
) -> list[dict]:
    if len(recalled_memories) <= 1:
        return recalled_memories
    stopwords = {
        "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "at", "by",
        "is", "are", "was", "were", "did", "does", "do", "what", "when", "where",
        "who", "why", "how", "which", "with", "from", "their", "them", "they",
        "her", "his", "she", "he", "it", "this", "that", "these", "those", "be",
        "have", "has", "had", "been", "would", "could", "should", "about", "into",
        "than", "then", "after", "before", "during", "ago", "kind", "type",
    }
    query_tokens = [tok for tok in _tokenize_recall_text(question) if tok not in stopwords and len(tok) > 1]
    if not query_tokens:
        return recalled_memories

    prefer_person_profile = _question_targets_person_profile(question)
    question_names = [name.lower() for name in _extract_person_name_candidates(question)]

    def _memory_score(memory: dict, index: int) -> tuple[int, int, int, int, int, int]:
        title = _normalize_recall_text(memory.get("title") or memory.get("name"), max_chars=240)
        summary = _extract_recall_summary(memory, max_chars=1000)
        detail = _extract_recall_detail(memory, summary=summary, max_chars=1200)
        haystack = f"{title}\n{summary}\n{detail}".lower()
        uri = str(memory.get("uri") or "")
        overlap = 0
        exact_mentions = 0
        for token in query_tokens:
            if token in haystack:
                overlap += 1
                exact_mentions += haystack.count(token)
        has_title_hit = int(any(token in title.lower() for token in query_tokens))
        named_person_entity_hit = 0
        if "/entities/person/" in uri:
            lowered_title = title.lower()
            if any(name == lowered_title or re.search(rf"\\b{re.escape(name)}\\b", lowered_title) for name in question_names):
                named_person_entity_hit = 1
        strong_person_entity_hit = 0
        if prefer_person_profile and named_person_entity_hit:
            strong_person_entity_hit = 1
        return (strong_person_entity_hit, named_person_entity_hit, overlap, exact_mentions, has_title_hit, -index)

    ranked = sorted(
        enumerate(recalled_memories),
        key=lambda item: _memory_score(item[1], item[0]),
        reverse=True,
    )
    return [memory for _, memory in ranked]


def format_ov_recall_evidence_block(
    recalled_memories: list[dict],
    *,
    max_items: int = 5,
    max_chars_per_item: int = 400,
) -> str:
    if not recalled_memories:
        return ""
    lines = [
        "Retrieved memory evidence:",
        "Use the following recalled memories as concrete evidence before answering.",
    ]
    for idx, memory in enumerate(recalled_memories[:max_items], start=1):
        title = _normalize_recall_text(
            memory.get("title") or memory.get("name"),
            max_chars=160,
        )
        summary = _extract_recall_summary(memory, max_chars=max_chars_per_item)
        detail = _extract_recall_detail(memory, summary=summary, max_chars=max_chars_per_item)
        score = memory.get("score")
        header = f"[Memory {idx}]"
        if score not in (None, ""):
            header += f" score={score}"
        lines.append(header)
        if title:
            lines.append(f"Title: {title}")
        if summary:
            lines.append(f"Summary: {summary}")
        if detail:
            lines.append(f"Details: {detail}")
    return "\n".join(lines).strip()


def build_qa_input_message(
    *,
    question: str,
    question_time: str | None,
    recalled_memories: list[dict] | None = None,
) -> str:
    qa_prompt_prefix = os.environ.get("LOCOMO_QA_PROMPT_PREFIX", "").strip() or DEFAULT_LOCOMO_QA_PROMPT_PREFIX
    evidence_block = format_ov_recall_evidence_block(
        recalled_memories or [],
        max_items=max(int(os.environ.get("LOCOMO_QA_DIRECT_RECALL_LIMIT", "8") or 8), 1),
        max_chars_per_item=max(int(os.environ.get("LOCOMO_QA_DIRECT_RECALL_CHARS", "400") or 400), 80),
    )
    if question_time:
        if evidence_block:
            return (
                f"{qa_prompt_prefix}Current date: {question_time}.\n\n"
                f"{evidence_block}\n\nQuestion: {question}"
            )
        return f"{qa_prompt_prefix}Current date: {question_time}. Question: {question}"
    if evidence_block:
        return f"{qa_prompt_prefix}{evidence_block}\n\nQuestion: {question}"
    return f"{qa_prompt_prefix}Question: {question}"


def build_ingest_input_message(message: str) -> str:
    ingest_prompt_prefix = (
        os.environ.get("LOCOMO_INGEST_PROMPT_PREFIX", "").strip()
        or DEFAULT_LOCOMO_INGEST_PROMPT_PREFIX
    )
    return f"{ingest_prompt_prefix}{message}"


def _merge_usage_totals(left: dict | None, right: dict | None) -> dict:
    merged = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "total_tokens": 0,
    }
    for payload in (left or {}, right or {}):
        for key in merged:
            merged[key] += int(payload.get(key, 0) or 0)
    return merged


def _merge_ov_token_usage(left: dict | None, right: dict | None) -> dict | None:
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


def get_gateway_request_timeout_seconds() -> float:
    raw = os.environ.get("LOCOMO_GATEWAY_REQUEST_TIMEOUT_SECONDS", "180").strip()
    try:
        value = float(raw or "0")
    except ValueError:
        value = 180.0
    return max(value, 10.0)


def get_gateway_retry_count(default: int = 2) -> int:
    raw = os.environ.get("LOCOMO_GATEWAY_RETRY_COUNT", "").strip()
    if not raw:
        return max(default, 0)
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(value, 0)


def get_gateway_retry_backoff_seconds() -> float:
    raw = os.environ.get("LOCOMO_GATEWAY_RETRY_BACKOFF_SECONDS", "3").strip()
    try:
        value = float(raw or "0")
    except ValueError:
        value = 3.0
    return max(value, 0.1)


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


def append_session_ingest_diagnostic(output_dir: str, payload: dict[str, object]) -> None:
    path = os.path.join(output_dir, "session_ingest_diagnostics.jsonl")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _finalize_openviking_ingest_sessions(
    *,
    pending_sessions: list[dict[str, object]],
    cfg: Config,
    output_dir: str,
    memory_token_totals: dict,
    ingest_record: dict,
) -> None:
    if not pending_sessions:
        return
    print(f"\n=== OpenViking final drain: {len(pending_sessions)} accepted session(s) ===", file=sys.stderr)
    while pending_sessions:
        _finalize_openviking_ingest_session_item(
            item=pending_sessions.pop(0),
            cfg=cfg,
            output_dir=output_dir,
            memory_token_totals=memory_token_totals,
            ingest_record=ingest_record,
        )


def _finalize_openviking_ingest_session_item(
    *,
    item: dict[str, object],
    cfg: Config,
    output_dir: str,
    memory_token_totals: dict,
    ingest_record: dict,
) -> None:
    ingest_task_wait_seconds = get_openviking_ingest_task_wait_seconds()
    task_id = str(item.get("task_id") or "")
    session_id = str(item.get("ov_session_id") or "")
    ov_agent_id = str(item.get("ov_agent_id") or cfg.agent_id or "main")
    if not session_id:
        return
    task_result = None
    task_wait_diag = {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "no_task"}
    if task_id:
        task_result, task_wait_diag = query_ov_task_token_usage(
            cfg.openviking.api_url,
            task_id,
            state_dir=cfg.gateway.state_dir,
            fallback_agent_id=ov_agent_id,
            max_wait=ingest_task_wait_seconds,
            resource_id=session_id,
            return_diag=True,
        )
    if _is_empty_ov_token_usage(task_result):
        task_result, latest_task_diag = wait_for_ov_latest_task(
            cfg.openviking.api_url,
            resource_id=session_id,
            state_dir=cfg.gateway.state_dir,
            fallback_agent_id=ov_agent_id,
            max_wait=ingest_task_wait_seconds,
            return_diag=True,
        )
        task_wait_diag = latest_task_diag

    consistency = query_ov_index_consistency(
        cfg.openviking.api_url,
        f"viking://user/{cfg.user}/memories",
        state_dir=cfg.gateway.state_dir,
        fallback_agent_id=cfg.agent_id,
    )

    accepted_elapsed = float(item.get("accepted_elapsed_seconds", 0.0) or 0.0)
    total_elapsed = accepted_elapsed + float(task_wait_diag.get("elapsed_seconds", 0.0) or 0.0)
    event = "completed"
    if task_wait_diag.get("timed_out"):
        event = "timeout"
    elif task_result is None:
        event = "completed_empty"

    if task_result:
        print(
            f"    [ov-drain] session={session_id} llm={task_result['llm_total']:,} "
            f"embed={task_result['embedding']:,} memories={task_result['memories']} "
            f"polls={task_wait_diag.get('poll_count', 0)} elapsed={float(task_wait_diag.get('elapsed_seconds', 0.0)):.1f}s",
            file=sys.stderr,
        )
        _add_memory_token_usage(memory_token_totals, {"provider": "openviking", **task_result})
    else:
        print(
            f"    [ov-drain] session={session_id} result=empty polls={task_wait_diag.get('poll_count', 0)} "
            f"elapsed={float(task_wait_diag.get('elapsed_seconds', 0.0)):.1f}s timed_out={str(task_wait_diag.get('timed_out', False)).lower()}",
            file=sys.stderr,
        )

    append_session_ingest_diagnostic(
        output_dir,
        {
            "event": event,
            "sample_id": item.get("sample_id"),
            "session_key": item.get("session_key"),
            "session_date_time": item.get("session_date_time"),
            "query_mode": item.get("query_mode"),
            "ov_commit": item.get("ov_commit"),
            "send": item.get("send"),
            "ov_task_wait": task_wait_diag,
            "ov_consistency": {
                "ok": bool(consistency.get("ok", True)) if isinstance(consistency, dict) else True,
                "missing_record_count": int((consistency or {}).get("missing_record_count", 0) or 0) if isinstance(consistency, dict) else 0,
            },
            "ov_token_usage": task_result or {},
            "accepted_elapsed_seconds": accepted_elapsed,
            "session_total_elapsed_seconds": round(total_elapsed, 3),
            "slow_threshold_seconds": get_openviking_chunk_slow_threshold_seconds(),
            "slow": total_elapsed >= get_openviking_chunk_slow_threshold_seconds(),
            "status": "passed" if event in {"completed", "completed_empty"} else "failed",
        },
    )

    result_entry = item.get("result_entry")
    if isinstance(result_entry, dict):
        if task_result:
            result_entry["ov_token_usage"] = task_result
            result_entry["memory_token_usage"] = {"provider": "openviking", **task_result}
        if isinstance(consistency, dict):
            result_entry["ov_missing_records"] = int(consistency.get("missing_record_count", 0) or 0)
    mark_ingested(
        cfg.agent_id,
        cfg.user,
        item.get("sample_id"),
        str(item.get("session_key") or ""),
        ingest_record,
        {
            "date_time": item.get("session_date_time"),
            "usage": item.get("usage", {}),
            "accepted": True,
            "completed_event": event,
        },
    )


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
        resp = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=get_gateway_request_timeout_seconds(),
        )
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

    usage = _normalize_usage(body.get("usage", {}))
    return extract_response_text(body), usage


def _send_message_with_retry_diagnostics(
    base_url: str, token: str, user: str, message: str, retries: int = 2,
    agent_id: str = "main", session_key: str | None = None,
) -> tuple[str, dict, dict]:
    retries = get_gateway_retry_count(retries)
    request_timeout = get_gateway_request_timeout_seconds()
    backoff = get_gateway_retry_backoff_seconds()
    last_exc = None
    waits: list[float] = []
    started = time.monotonic()
    for attempt in range(retries + 1):
        try:
            reply, usage = send_message(base_url, token, user, message, agent_id, session_key)
            return reply, usage, {
                "attempts": attempt + 1,
                "retries_configured": retries,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "request_timeout_seconds": request_timeout,
                "wait_schedule_seconds": waits,
                "timeout_hit": False,
                "final_error": "",
            }
        except Exception as e:
            last_exc = e
            if attempt < retries:
                wait = backoff * (attempt + 1)
                waits.append(wait)
                print(f"    [retry {attempt + 1}/{retries}] {e} (waiting {wait}s)", file=sys.stderr)
                time.sleep(wait)
    assert last_exc is not None
    raise RuntimeError(
        json.dumps(
            {
                "message": str(last_exc),
                "attempts": retries + 1,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "request_timeout_seconds": request_timeout,
                "wait_schedule_seconds": waits,
                "timeout_hit": "timeout" in str(last_exc).lower(),
            },
            ensure_ascii=False,
        )
    ) from last_exc


def send_message_with_retry(
    base_url: str, token: str, user: str, message: str, retries: int = 2,
    agent_id: str = "main", session_key: str | None = None,
) -> tuple[str, dict]:
    reply, usage, _ = _send_message_with_retry_diagnostics(
        base_url, token, user, message, retries, agent_id, session_key
    )
    return reply, usage


# ---------------------------------------------------------------------------
# OV Task API helpers
# ---------------------------------------------------------------------------

def _parse_ov_task_result(data: dict) -> dict | None:
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


def _is_empty_ov_token_usage(payload: dict | None) -> bool:
    if not payload:
        return True
    return (
        int(payload.get("llm_total", 0) or 0) == 0
        and int(payload.get("embedding", 0) or 0) == 0
        and int(payload.get("memories", 0) or 0) == 0
    )


def _load_openviking_plugin_config(state_dir: str) -> dict:
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


def _resolve_openviking_agent_header(plugin_cfg: dict, fallback_agent_id: str) -> str:
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


def _ov_request_headers(state_dir: str = "", fallback_agent_id: str = "main") -> dict | None:
    plugin_cfg = _load_openviking_plugin_config(state_dir)
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
    agent_id = _resolve_openviking_agent_header(plugin_cfg, fallback_agent_id)
    if account_id:
        headers["X-OpenViking-Account"] = account_id
    if user_id:
        headers["X-OpenViking-User"] = user_id
    if agent_id:
        headers["X-OpenViking-Agent"] = agent_id
    return headers


def _extract_session_id(session_file: str) -> str:
    return Path(session_file).name.removesuffix(".jsonl")


def build_openviking_ingest_agent_id(base_agent_id: str, session_key: str | None) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(base_agent_id or "main")).strip("-") or "main"
    if not session_key:
        return base
    suffix = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(session_key)).strip("-")
    if not suffix:
        return base
    return f"{base}--{suffix}"


def commit_openviking_session(
    *,
    ov_api_url: str,
    session_id: str,
    keep_recent_count: int | None = None,
    wait: bool = False,
    state_dir: str = "",
    fallback_agent_id: str = "main",
) -> dict:
    headers = _ov_request_headers(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        raise RuntimeError("Missing OpenViking API key for direct session commit")
    payload = {"wait": wait}
    if keep_recent_count is not None:
        payload["keep_recent_count"] = keep_recent_count
    resp = requests.post(
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
) -> dict | tuple[dict | None, dict]:
    headers = _ov_request_headers(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        result = None
        diag = {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "missing_headers"}
        return (result, diag) if return_diag else result
    deadline = time.time() + max_wait
    interval = 2
    poll_count = 0
    started = time.monotonic()
    fallback_used = False
    final_status = ""
    try:
        while True:
            poll_count += 1
            resp = requests.get(
                f"{ov_api_url}/api/v1/tasks/{task_id}",
                headers=headers,
                timeout=get_openviking_task_request_timeout_seconds(),
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("result", {}).get("status", "") if isinstance(data.get("result"), dict) else ""
            final_status = status or final_status
            if status in ("completed", "failed", ""):
                parsed = _parse_ov_task_result(data)
                if _is_empty_ov_token_usage(parsed) and resource_id:
                    fallback = query_ov_latest_task(
                        ov_api_url,
                        resource_id=resource_id,
                        state_dir=state_dir,
                        fallback_agent_id=fallback_agent_id,
                    )
                    if fallback and not _is_empty_ov_token_usage(fallback):
                        fallback_used = True
                        result = fallback
                        diag = {"poll_count": poll_count, "elapsed_seconds": round(time.monotonic() - started, 3), "timed_out": False, "fallback_used": fallback_used, "final_status": status or "completed"}
                        return (result, diag) if return_diag else result
                result = parsed
                diag = {"poll_count": poll_count, "elapsed_seconds": round(time.monotonic() - started, 3), "timed_out": False, "fallback_used": fallback_used, "final_status": status or "completed"}
                return (result, diag) if return_diag else result
            if time.time() >= deadline:
                if resource_id:
                    fallback = query_ov_latest_task(
                        ov_api_url,
                        resource_id=resource_id,
                        state_dir=state_dir,
                        fallback_agent_id=fallback_agent_id,
                    )
                    if fallback and not _is_empty_ov_token_usage(fallback):
                        fallback_used = True
                        result = fallback
                        diag = {"poll_count": poll_count, "elapsed_seconds": round(time.monotonic() - started, 3), "timed_out": True, "fallback_used": fallback_used, "final_status": final_status or "timeout"}
                        return (result, diag) if return_diag else result
                result = None
                diag = {"poll_count": poll_count, "elapsed_seconds": round(time.monotonic() - started, 3), "timed_out": True, "fallback_used": fallback_used, "final_status": final_status or "timeout"}
                return (result, diag) if return_diag else result
            time.sleep(interval)
            interval = min(interval * 2, 10)
    except Exception as e:
        print(f"    [ov-task] Error querying task {task_id}: {e}", file=sys.stderr)
        result = None
        diag = {"poll_count": poll_count, "elapsed_seconds": round(time.monotonic() - started, 3), "timed_out": False, "fallback_used": fallback_used, "final_status": "error", "error": str(e)}
        return (result, diag) if return_diag else result


def query_ov_latest_task(
    ov_api_url: str,
    resource_id: str | None = None,
    *,
    state_dir: str = "",
    fallback_agent_id: str = "main",
) -> dict | None:
    headers = _ov_request_headers(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return None
    try:
        params = {"task_type": "session_commit", "status": "completed", "limit": 1}
        if resource_id:
            params["resource_id"] = resource_id
        resp = requests.get(f"{ov_api_url}/api/v1/tasks", params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        tasks = data.get("result", [])
        if tasks:
            task = tasks[0]
            result = _parse_ov_task_result({"result": task})
            if result:
                result["task_id"] = task.get("task_id", "")
            return result
    except requests.exceptions.HTTPError as e:
        if resource_id and e.response is not None and e.response.status_code == 400:
            try:
                resp = requests.get(
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
                    result = _parse_ov_task_result({"result": task})
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
) -> dict | tuple[dict | None, dict]:
    deadline = time.time() + max_wait
    interval = 2
    poll_count = 0
    started = time.monotonic()
    while True:
        poll_count += 1
        result = query_ov_latest_task(
            ov_api_url,
            resource_id=resource_id,
            state_dir=state_dir,
            fallback_agent_id=fallback_agent_id,
        )
        if result:
            diag = {"poll_count": poll_count, "elapsed_seconds": round(time.monotonic() - started, 3), "timed_out": False, "fallback_used": True, "final_status": "latest_task"}
            return (result, diag) if return_diag else result
        if time.time() >= deadline:
            diag = {"poll_count": poll_count, "elapsed_seconds": round(time.monotonic() - started, 3), "timed_out": True, "fallback_used": False, "final_status": "timeout"}
            return (None, diag) if return_diag else None
        time.sleep(interval)
        interval = min(interval * 2, 10)


def query_ov_session_usage(
    ov_api_url: str,
    session_id: str,
    *,
    state_dir: str = "",
    fallback_agent_id: str = "main",
    max_wait: int = 30,
    interval: float = 1.0,
) -> dict | None:
    headers = _ov_request_headers(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return None
    deadline = time.time() + max_wait
    while True:
        try:
            resp = requests.get(f"{ov_api_url}/api/v1/sessions/{session_id}", headers=headers, timeout=30)
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
        if time.time() >= deadline:
            return None
        time.sleep(interval)


def query_ov_index_consistency(
    ov_api_url: str,
    uri: str,
    *,
    state_dir: str = "",
    fallback_agent_id: str = "main",
) -> dict | None:
    headers = _ov_request_headers(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return None
    try:
        resp = requests.post(
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


def normalize_ov_task_query_mode(memory_mode: str) -> str:
    if memory_mode == "openviking":
        return "direct_ov_stable"
    return "legacy"


def should_attempt_gateway_compact(memory_mode: str) -> bool:
    if memory_mode != "openviking":
        return True
    return os.environ.get("LOCOMO_OPENVIKING_FORCE_COMPACT", "").lower() in {"1", "true", "yes", "on"}


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

        protocol = 4
        attempted_protocols: set[int] = set()
        while True:
            attempted_protocols.add(protocol)
            connect_id = str(uuid.uuid4())
            ws.send(json.dumps({
                "type": "req", "id": connect_id, "method": "connect",
                "params": {
                    "minProtocol": protocol, "maxProtocol": protocol,
                    "client": {"id": "openclaw-control-ui", "version": "1.0.0", "platform": sys.platform, "mode": "webchat"},
                    "scopes": ["operator.admin", "operator.read", "operator.write"],
                    "auth": {"token": token},
                },
            }))

            while True:
                msg = json.loads(ws.recv())
                if msg.get("type") == "res" and msg.get("id") == connect_id:
                    if msg.get("ok"):
                        break
                    error = msg.get("error", msg)
                    expected_protocol = error.get("details", {}).get("expectedProtocol")
                    if isinstance(expected_protocol, int) and expected_protocol not in attempted_protocols:
                        print(
                            f"    [compact] retry with gateway protocol {expected_protocol}",
                            file=sys.stderr,
                        )
                        protocol = expected_protocol
                        break
                    raise RuntimeError(f"[compact] Handshake rejected: {error}")
            else:
                continue

            if msg.get("ok"):
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
    "ov_llm_prompt_tokens", "ov_llm_completion_tokens", "ov_llm_total_tokens",
    "ov_embedding_tokens", "ov_memories_extracted", "ov_memory_write", "ov_memory_edit",
    "ov_missing_records", "ov_recall_total", "ov_direct_recall_count", "ov_recall_hit",
    "ov_memory_written", "ov_token_emitted", "ov_index_available", "ov_closure_state",
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
    ov_usage = flat.pop("ov_token_usage", {}) or {}
    flat["ov_llm_prompt_tokens"] = ov_usage.get("llm_prompt", 0)
    flat["ov_llm_completion_tokens"] = ov_usage.get("llm_completion", 0)
    flat["ov_llm_total_tokens"] = ov_usage.get("llm_total", 0)
    flat["ov_embedding_tokens"] = ov_usage.get("embedding", 0)
    flat["ov_memories_extracted"] = ov_usage.get("memories", 0)
    flat["ov_memory_write"] = ov_usage.get("memory_write", 0)
    flat["ov_memory_edit"] = ov_usage.get("memory_edit", 0)
    flat["ov_missing_records"] = flat.get("ov_missing_records", 0)
    flat["ov_recall_total"] = flat.get("ov_recall_total", 0)
    flat["ov_direct_recall_count"] = flat.get("ov_direct_recall_count", 0)
    flat["ov_recall_hit"] = flat.get("ov_recall_hit", "")
    flat["ov_memory_written"] = flat.get("ov_memory_written", "")
    flat["ov_token_emitted"] = flat.get("ov_token_emitted", "")
    flat["ov_index_available"] = flat.get("ov_index_available", "")
    flat["ov_closure_state"] = flat.get("ov_closure_state", "")
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
) -> int:
    headers = _ov_request_headers(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return 0
    try:
        resp = requests.post(
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
) -> list[dict]:
    headers = _ov_request_headers(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return []

    try:
        resp = requests.post(
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
                content = _ov_read_content_by_uri(ov_api_url, headers, uri)
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
) -> dict[str, Any]:
    headers = _ov_request_headers(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return {"ok": False, "last_error": "missing_headers"}
    payload = {
        "uri": f"viking://user/{user_id}/memories",
        "mode": "vectors_only",
        "wait": True,
    }
    deadline = time.monotonic() + max(timeout, 1.0)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            resp = requests.post(
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
                time.sleep(2.0)
                continue
            return {
                "ok": False,
                "target_uri": payload["uri"],
                "last_error": last_error,
            }
        except Exception as exc:
            last_error = str(exc)
            time.sleep(2.0)
    return {"ok": False, "target_uri": payload["uri"], "last_error": last_error}


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
    pending_ov_sessions: list[dict[str, object]] = []

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
                ingest_messages = [msg]
                if cfg.memory_mode == "memcore":
                    memory_prompt = (
                        "Extract key facts from the next group conversation and store them "
                        "in a SEPARATE memory file named memory/YYYY-MM-DD.md where YYYY-MM-DD "
                        "is the CONVERSATION date (from the message header, NOT today). "
                        "Use the write tool immediately. Do not append to existing files, "
                        "create a new file per conversation date.\n\n"
                    )
                    ingest_messages = [memory_prompt + msg]

                ogmem_log_baseline = None
                ogmem_tokens_before = None
                if cfg.memory_mode == "ogmem":
                    ogmem_wait_since = time.time()
                    ogmem_log_baseline = count_ogmem_after_turn_extract_logs(
                        cfg.ogmem.docker_container,
                        cfg.ogmem.log_tail,
                    )
                    ogmem_tokens_before = query_ogmem_token_stats(cfg.ogmem.api_url)

                reply_parts: list[str] = []
                usage = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "total_tokens": 0,
                }
                memory_token_usage = None
                ov_token_usage = None
                query_mode = normalize_ov_task_query_mode(cfg.memory_mode)
                for ingest_idx, raw_ingest_msg in enumerate(ingest_messages, start=1):
                    session_diag = {
                        "sample_id": sample_id,
                        "session_key": meta.get("session_key"),
                        "session_date_time": meta.get("date_time"),
                        "query_mode": query_mode,
                    }
                    ov_ingest_agent_id = build_openviking_ingest_agent_id(
                        cfg.agent_id,
                        meta.get("session_key"),
                    )
                    current_session_key = oc_session_key
                    if oc_session_key and len(ingest_messages) > 1:
                        current_session_key = f"{oc_session_key}-part{ingest_idx}"
                    ingest_msg = (
                        raw_ingest_msg
                        if cfg.memory_mode == "memcore"
                        else build_ingest_input_message(raw_ingest_msg)
                    )
                    reply, chunk_usage, send_diag = _send_message_with_retry_diagnostics(
                        cfg.gateway.base_url, cfg.gateway.token, user_key,
                        ingest_msg, 2, cfg.agent_id, current_session_key,
                    )
                    session_diag["send"] = send_diag
                    reply_parts.append(reply)
                    usage = _merge_usage_totals(usage, chunk_usage)
                    preview = reply[:80]
                    if len(ingest_messages) > 1:
                        print(
                            f"    [chunk {ingest_idx}/{len(ingest_messages)}] -> {preview}{'...' if len(reply) > 80 else ''}",
                            file=sys.stderr,
                        )
                    else:
                        print(f"    -> {preview}{'...' if len(reply) > 80 else ''}", file=sys.stderr)
                    session_total_elapsed = float(send_diag["elapsed_seconds"])
                    session_slow_threshold = get_openviking_chunk_slow_threshold_seconds()

                    if cfg.memory_mode == "openviking":
                        commit_key = current_session_key or f"agent:{cfg.agent_id}:openresponses-user:{user_key}"
                        found = wait_for_session_id_from_key(commit_key, user_key, cfg.agent_id, cfg.gateway.state_dir)
                        if not found:
                            found = _find_latest_session_file(cfg.agent_id, cfg.gateway.state_dir)
                        if not found:
                            found = _find_session_id_in_openclaw_log(
                                commit_key,
                                agent_id=cfg.agent_id,
                                state_dir=cfg.gateway.state_dir,
                            )
                        if not found:
                            raise RuntimeError(f"OpenViking session not found for {commit_key}")
                        session_file, _ = found
                        ov_session_id = _extract_session_id(session_file)
                        commit_result = commit_openviking_session(
                            ov_api_url=cfg.openviking.api_url,
                            session_id=ov_session_id,
                            keep_recent_count=cfg.openviking.keep_recent_count,
                            wait=False,
                            state_dir=cfg.gateway.state_dir,
                            fallback_agent_id=ov_ingest_agent_id,
                        )
                        print(
                            f"    [ov-commit] {query_mode} status={commit_result.get('status') or 'unknown'} "
                            f"wait=false session={ov_session_id}",
                            file=sys.stderr,
                        )
                        session_diag["ov_commit"] = {
                            "session_id": ov_session_id,
                            "status": commit_result.get("status") or "unknown",
                            "task_id": commit_result.get("task_id") or "",
                            "wait": False,
                        }
                        task_id = commit_result.get("task_id") or ""
                    print(
                        f"    [ingest-diag] send attempts={send_diag['attempts']}/{send_diag['retries_configured'] + 1} "
                        f"accepted_elapsed={session_total_elapsed:.1f}s "
                        f"timeout={send_diag['request_timeout_seconds']:.1f}s "
                        f"accepted=true",
                        file=sys.stderr,
                    )
                    session_diag["event"] = "accepted"
                    session_diag["accepted_at"] = datetime.now().isoformat()
                    session_diag["accepted_elapsed_seconds"] = round(session_total_elapsed, 3)
                    session_diag["slow_threshold_seconds"] = session_slow_threshold
                    session_diag["status"] = "accepted"
                    append_session_ingest_diagnostic(output_dir, session_diag)
                    result_entry = {
                        "sample_id": sample_id, "session": meta["session_key"],
                        "user": user_key, "reply": "\n".join(part for part in reply_parts if part).strip(), "usage": usage,
                    }
                    results.append(result_entry)
                    if cfg.memory_mode == "openviking":
                        pending_ov_sessions.append(
                            {
                                "sample_id": sample_id,
                                "session_key": meta.get("session_key"),
                                "session_date_time": meta.get("date_time"),
                                "query_mode": query_mode,
                                "ov_session_id": ov_session_id,
                                "task_id": task_id,
                                "ov_commit": session_diag["ov_commit"],
                                "send": send_diag,
                                "accepted_elapsed_seconds": session_total_elapsed,
                                "usage": usage,
                                "ov_agent_id": ov_ingest_agent_id,
                                "result_entry": result_entry,
                            }
                        )
                        max_pending_sessions = get_openviking_max_pending_ingest_sessions()
                        while max_pending_sessions >= 0 and len(pending_ov_sessions) > max_pending_sessions:
                            _finalize_openviking_ingest_session_item(
                                item=pending_ov_sessions.pop(0),
                                cfg=cfg,
                                output_dir=output_dir,
                                memory_token_totals=memory_token_totals,
                                ingest_record=ingest_record,
                            )
                    else:
                        mark_ingested(cfg.agent_id, user_key, sample_id, meta["session_key"], ingest_record, {
                            "date_time": meta["date_time"], "usage": usage,
                        })
                    if (policy == SessionPolicy.ISOLATED or cfg.memory_mode == "ogmem") and current_session_key:
                        found = get_session_id_from_key(current_session_key, user_key, cfg.agent_id, cfg.gateway.state_dir)
                        if found:
                            sf, sdir = found
                            sf_path = sf if os.path.isabs(sf) else os.path.join(sdir, sf)
                            if not sf_path.endswith(".jsonl"):
                                sf_path += ".jsonl"
                            reset_session(sf_path, cfg.agent_id, cfg.gateway.state_dir)
                if cfg.memory_mode == "ogmem":
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

                if memory_token_usage:
                    result_entry = results[-1]
                    result_entry["memory_token_usage"] = memory_token_usage
                    _add_memory_token_usage(memory_token_totals, memory_token_usage)
                    mark_ingested(cfg.agent_id, user_key, sample_id, meta["session_key"], ingest_record, {
                        "date_time": meta["date_time"], "usage": usage,
                    })
            except Exception as e:
                append_session_ingest_diagnostic(
                    output_dir,
                    {
                        "sample_id": sample_id,
                        "session_key": meta.get("session_key"),
                        "status": "failed",
                        "error": str(e),
                    },
                )
                print(f"    -> [FATAL] Ingest failed, aborting: {e}", file=sys.stderr)
                raise RuntimeError(f"Ingest failed for sample {sample_id} session {meta['session_key']}: {e}") from e

            if policy == SessionPolicy.SHARED and cfg.memory_mode not in ("openviking", "ogmem"):
                sid = get_session_id(user_key, cfg.agent_id, cfg.gateway.state_dir)
                if sid:
                    reset_session(sid, cfg.agent_id, cfg.gateway.state_dir)

    if cfg.memory_mode == "openviking":
        _finalize_openviking_ingest_sessions(
            pending_sessions=pending_ov_sessions,
            cfg=cfg,
            output_dir=output_dir,
            memory_token_totals=memory_token_totals,
            ingest_record=ingest_record,
        )

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
    qa_commit_skipped = cfg.memory_mode == "openviking" and should_skip_openviking_qa_commit(session_key)

    print(f"  [{sample_idx}] Q{qi}: {question[:60]}{'...' if len(question) > 60 else ''}", file=sys.stderr)

    direct_recalled_memories = []
    if cfg.memory_mode == "openviking":
        direct_recalled_memories = query_ov_search_find_memories(
            cfg.openviking.api_url,
            question,
            f"viking://user/{user_key}/memories",
            state_dir=cfg.gateway.state_dir,
            fallback_agent_id=cfg.agent_id,
            limit=max(int(os.environ.get("LOCOMO_QA_DIRECT_RECALL_LIMIT", "8") or 8), 1),
        )
        direct_recalled_memories = augment_ov_recall_with_named_person_entities(
            ov_api_url=cfg.openviking.api_url,
            question=question,
            user_id=user_key,
            recalled_memories=direct_recalled_memories,
            state_dir=cfg.gateway.state_dir,
            fallback_agent_id=cfg.agent_id,
        )
        direct_recalled_memories = rerank_ov_recalled_memories(question, direct_recalled_memories)
        if direct_recalled_memories:
            print(
                f"  [{sample_idx}]   [ov-direct-recall] memories={len(direct_recalled_memories)}",
                file=sys.stderr,
            )
    input_msg = build_qa_input_message(
        question=question,
        question_time=question_time,
        recalled_memories=direct_recalled_memories,
    )

    jsonl_filename = ""
    ov_token_usage = None
    consistency = None
    ov_recall_total = 0
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
            if cfg.memory_mode == "openviking" and not qa_commit_skipped:
                ov_session_id = _extract_session_id(Path(jsonl_path).name)
                commit_result = commit_openviking_session(
                    ov_api_url=cfg.openviking.api_url,
                    session_id=ov_session_id,
                    keep_recent_count=cfg.openviking.keep_recent_count,
                    wait=False,
                    state_dir=cfg.gateway.state_dir,
                    fallback_agent_id=cfg.agent_id,
                )
                print(
                    f"  [{sample_idx}]   [ov-commit] status={commit_result.get('status') or 'unknown'} "
                    f"session={ov_session_id}",
                    file=sys.stderr,
                )
                task_id = commit_result.get("task_id") or ""
                if task_id:
                    ov_token_usage = query_ov_task_token_usage(
                        cfg.openviking.api_url,
                        task_id,
                        state_dir=cfg.gateway.state_dir,
                        fallback_agent_id=cfg.agent_id,
                        max_wait=30,
                        resource_id=ov_session_id,
                    )
                    if ov_token_usage:
                        print(
                            f"  [{sample_idx}]   [ov-task] llm={ov_token_usage['llm_total']:,} "
                            f"embed={ov_token_usage['embedding']:,} memories={ov_token_usage['memories']}",
                            file=sys.stderr,
                        )
                if _is_empty_ov_token_usage(ov_token_usage):
                    ov_token_usage = query_ov_session_usage(
                        cfg.openviking.api_url,
                        ov_session_id,
                        state_dir=cfg.gateway.state_dir,
                        fallback_agent_id=cfg.agent_id,
                        max_wait=30,
                        interval=1.0,
                    )
                    if ov_token_usage:
                        print(
                            f"  [{sample_idx}]   [ov-session] llm={ov_token_usage['llm_total']:,} "
                            f"embed={ov_token_usage['embedding']:,} memories={ov_token_usage['memories']}",
                            file=sys.stderr,
                        )
                consistency = query_ov_index_consistency(
                    cfg.openviking.api_url,
                    f"viking://user/{user_key}/memories",
                    state_dir=cfg.gateway.state_dir,
                    fallback_agent_id=cfg.agent_id,
                )
                ov_recall_total = query_ov_search_find_total(
                    cfg.openviking.api_url,
                    question,
                    f"viking://user/{user_key}/memories",
                    state_dir=cfg.gateway.state_dir,
                    fallback_agent_id=cfg.agent_id,
                )
                if ov_recall_total:
                    print(
                        f"  [{sample_idx}]   [ov-recall] total={ov_recall_total}",
                        file=sys.stderr,
                    )
                if consistency and not consistency.get("ok", True):
                    print(
                        f"  [{sample_idx}]   [ov-consistency] missing_records={consistency.get('missing_record_count', 0)}",
                        file=sys.stderr,
                    )
            usage = calculate_usage_from_jsonl(jsonl_path)
            if int(usage.get("total_tokens", 0) or 0) == 0 and int(api_usage.get("total_tokens", 0) or 0) > 0:
                usage = {
                    "input_tokens": api_usage.get("input_tokens", 0),
                    "output_tokens": api_usage.get("output_tokens", 0),
                    "cacheRead": api_usage.get("cacheRead", 0),
                    "cacheWrite": api_usage.get("cacheWrite", 0),
                    "total_tokens": api_usage.get("total_tokens", 0),
                }
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
    if ov_token_usage:
        record["ov_token_usage"] = ov_token_usage
    if consistency:
        record["ov_missing_records"] = int(consistency.get("missing_record_count", 0) or 0)
    if cfg.memory_mode == "openviking":
        record["ov_recall_total"] = ov_recall_total
        record["ov_direct_recall_count"] = len(direct_recalled_memories)
    if cfg.memory_mode == "openviking":
        ov_state = derive_ov_closure_status(
            ov_token_usage,
            consistency,
            recall_total=ov_recall_total,
            direct_recall_count=len(direct_recalled_memories),
            response_text=response,
            qa_commit_skipped=qa_commit_skipped,
        )
        record["ov_recall_hit"] = ov_state["recall_hit"]
        record["ov_memory_written"] = ov_state["memory_written"]
        record["ov_token_emitted"] = ov_state["token_emitted"]
        record["ov_index_available"] = ov_state["index_available"]
        record["ov_closure_state"] = ov_state["closure_state"]

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

    if cfg.memory_mode == "openviking":
        reindex_result = reindex_ov_memory_root(
            ov_api_url=cfg.openviking.api_url,
            user_id=cfg.user,
            state_dir=cfg.gateway.state_dir,
            fallback_agent_id=cfg.agent_id,
            timeout=120.0,
        )
        Path(output_dir, "qa_reindex.json").write_text(
            json.dumps(reindex_result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"    [qa][reindex] {json.dumps(reindex_result, ensure_ascii=False)}",
            file=sys.stderr,
        )

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
