from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


def _resolve_openclaw_bin() -> str:
    custom = os.environ.get("OPENCLAW_BIN")
    if custom:
        return custom
    found = shutil.which("openclaw")
    if found:
        return found
    raise FileNotFoundError("openclaw binary not found")


def _extract_message(request: dict) -> str:
    messages = request.get("messages", [])
    for message in reversed(messages):
        if message.get("role") == "user" and message.get("content"):
            return str(message["content"])
    raise ValueError("RenderedTaskInput must contain at least one user message")


def build_openclaw_message(request: dict) -> str:
    system_prompt = str(request.get("system_prompt", "") or "").strip()
    messages = request.get("messages", [])
    attachments = request.get("attachments", [])
    lines: list[str] = []
    if system_prompt:
        lines.append("System instructions:")
        lines.append(system_prompt)
        lines.append("")
    lines.append("Conversation:")
    for message in messages:
        role = str(message.get("role", "user"))
        content = str(message.get("content", ""))
        lines.append(f"[{role}] {content}")
    if attachments:
        lines.append("")
        lines.append("Attachments:")
        for item in attachments:
            lines.append(f"- {item}")
    rendered = "\n".join(lines).strip()
    return rendered or _extract_message(request)


def session_id_from_key(session_key: str) -> str:
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
    return "-".join(
        [
            digest[:8],
            digest[8:12],
            digest[12:16],
            digest[16:20],
            digest[20:32],
        ]
    )


def build_openclaw_command(request: dict) -> list[str]:
    metadata = request.get("metadata", {})
    agent_id = metadata.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID")
    session_key = metadata.get("session_key")
    session_id = metadata.get("session_id")
    target = metadata.get("to")
    if not any([agent_id, session_key, session_id, target]):
        raise ValueError("OpenClaw runner needs one of agent_id, session_key, session_id, or to")

    cmd = [_resolve_openclaw_bin(), "agent", "--message", build_openclaw_message(request), "--json"]
    if agent_id:
        cmd += ["--agent", str(agent_id)]
    if session_id:
        cmd += ["--session-id", str(session_id)]
    elif session_key:
        cmd += ["--session-id", session_id_from_key(str(session_key))]
    if target:
        cmd += ["--to", str(target)]

    model = metadata.get("model")
    if model:
        cmd += ["--model", str(model)]
    thinking = metadata.get("thinking")
    if thinking:
        cmd += ["--thinking", str(thinking)]
    timeout_seconds = metadata.get("timeout_seconds")
    if timeout_seconds:
        cmd += ["--timeout", str(timeout_seconds)]
    if metadata.get("local"):
        cmd.append("--local")
    if metadata.get("deliver"):
        cmd.append("--deliver")
    return cmd


def resolve_transport() -> str:
    configured = os.environ.get("OPENCLAW_TRANSPORT", "").strip().lower()
    if configured:
        if configured not in {"cli", "http"}:
            raise ValueError("OPENCLAW_TRANSPORT must be 'cli' or 'http'")
        return configured
    if os.environ.get("OPENCLAW_GATEWAY_URL") or os.environ.get("OPENCLAW_GATEWAY_BASE_URL"):
        return "http"
    return "cli"


def _resolve_gateway_url() -> str:
    configured = (
        os.environ.get("OPENCLAW_GATEWAY_URL")
        or os.environ.get("OPENCLAW_GATEWAY_BASE_URL")
        or ""
    ).strip()
    if configured:
        return configured.rstrip("/")
    port = os.environ.get("OPENCLAW_GATEWAY_PORT", "18789").strip() or "18789"
    return f"http://127.0.0.1:{port}"


def build_openclaw_http_request(request: dict) -> tuple[str, dict[str, str], dict]:
    metadata = request.get("metadata", {})
    agent_id = metadata.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID")
    if not agent_id:
        raise ValueError("OpenClaw HTTP runner requires agent_id")

    headers = {
        "Content-Type": "application/json",
        "X-OpenClaw-Agent-ID": str(agent_id),
    }
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    session_key = metadata.get("session_key")
    session_id = metadata.get("session_id")
    if session_key:
        headers["X-OpenClaw-Session-Key"] = str(session_key)
    elif session_id:
        headers["X-OpenClaw-Session-Key"] = f"agent:{agent_id}:explicit:{session_id}"

    model = metadata.get("model")
    if model:
        headers["X-OpenClaw-Model"] = str(model)

    correlation = {
        "X-Trace-Run-ID": os.environ.get("TRACE_RUN_ID"),
        "X-Trace-Case-ID": metadata.get("case_id"),
        "X-Trace-Step-ID": metadata.get("step_id") or request.get("task_id"),
        "X-Trace-User-ID": metadata.get("user_id"),
        "X-Trace-Session-ID": session_id
        or (session_id_from_key(str(session_key)) if session_key else None),
        "X-Request-ID": metadata.get("request_id") or str(uuid.uuid4()),
    }
    headers.update({key: str(value) for key, value in correlation.items() if value})

    payload = {
        "model": f"openclaw/{agent_id}",
        "input": [
            {"type": "message", "role": str(message.get("role", "user")),
             "content": str(message.get("content", ""))}
            for message in request.get("messages", [])
        ],
        "stream": False,
    }
    if request.get("attachments"):
        payload["input"].append({"type": "message", "role": "user", "content":
            "Attachments:\n" + "\n".join(str(x) for x in request["attachments"])})
    if request.get("system_prompt"):
        payload["instructions"] = str(request["system_prompt"])
    trace_metadata = {
        key: metadata[key]
        for key in ("run_id", "case_id", "step_id", "user_id", "session_id")
        if metadata.get(key) is not None
    }
    if trace_metadata:
        payload["metadata"] = trace_metadata
    return f"{_resolve_gateway_url()}/v1/responses", headers, payload


def collect_openclaw_session_artifacts(
    request: dict, *, agent_id: str, session_id: str
) -> list[dict]:
    metadata = request.get("metadata", {})
    configured = metadata.get("session_jsonl") or os.environ.get("OPENCLAW_SESSION_JSONL")
    if configured:
        candidates = [Path(str(configured))]
    else:
        state_dir = Path(os.environ.get("OPENCLAW_STATE_DIR") or Path.home() / ".openclaw")
        candidates = [state_dir / "agents" / agent_id / "sessions" / f"{session_id}.jsonl"]
    for candidate in candidates:
        if candidate.is_file():
            return [
                {
                    "kind": "openclaw_session_jsonl",
                    "path": str(candidate),
                    "content_type": "application/x-ndjson",
                    "size_bytes": candidate.stat().st_size,
                    "tags": ["openclaw", "session", "trace-source"],
                }
            ]
    return []


def extract_openclaw_response_text(payload: dict) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("text"), str) and item["text"].strip():
            return item["text"]
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                if content["text"].strip():
                    return content["text"]
    choices = payload.get("choices", [])
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def run_openclaw_http(request: dict) -> dict:
    url, headers, body = build_openclaw_http_request(request)
    timeout_seconds = int(request.get("metadata", {}).get("timeout_seconds") or 600)
    http_request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(http_request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenClaw HTTP {exc.code}: {detail}") from exc
    duration_ms = int((time.monotonic() - started) * 1000)
    if payload.get("error") or payload.get("status") in {"failed", "cancelled", "incomplete"}:
        raise RuntimeError("OpenClaw response failed or incomplete; inspect Gateway logs")
    text = extract_openclaw_response_text(payload)
    metadata = request.get("metadata", {})
    configured_session_key = metadata.get("session_key")
    agent_id = str(metadata.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID") or "main")
    gateway_key = headers.get("X-OpenClaw-Session-Key", "").lower()
    if gateway_key and not gateway_key.startswith("agent:"):
        gateway_key = f"agent:{agent_id.lower()}:{gateway_key}"
    # Gateway allocates a UUID; the CLI's deterministic hash is not its session ID.
    resolved_session_id = ""
    state_dir = Path(os.environ.get("OPENCLAW_STATE_DIR") or Path.home() / ".openclaw")
    store = state_dir / "agents" / agent_id / "sessions/sessions.json"
    if store.is_file() and gateway_key:
        resolved_session_id = str(json.loads(store.read_text()).get(gateway_key, {}).get("sessionId") or "")
    return {
        "status": "ok",
        "agent": "openclaw",
        "transport": "http",
        "http_request": {"method": "POST", "url": url},
        "request": request,
        "raw": payload,
        "output": {
            "session_id": resolved_session_id,
            "session_key": str(configured_session_key or ""),
            "session_handle": {
                "session_id": resolved_session_id,
                "session_key": str(configured_session_key or ""),
                "gateway_session_key": gateway_key,
            },
        },
        "turns": [{"text": text}] if text else [],
        "artifacts": collect_openclaw_session_artifacts(
            request,
            agent_id=str(metadata.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID") or "main"),
            session_id=resolved_session_id,
        ),
        "metrics": [{"name": "duration_ms", "value": duration_ms}],
    }


def main() -> None:
    request = json.load(sys.stdin)
    if resolve_transport() == "http":
        json.dump(run_openclaw_http(request), sys.stdout, ensure_ascii=False)
        return
    cmd = build_openclaw_command(request)
    configured_timeout = request.get("metadata", {}).get("timeout_seconds")
    subprocess_timeout = max(90, int(configured_timeout or 60) + 30)
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=subprocess_timeout,
        check=True,
    )
    payload = json.loads(proc.stdout)
    result_payload = payload.get("result", payload)
    if not isinstance(result_payload, dict):
        result_payload = {}
    result_meta = result_payload.get("meta", {})
    if not isinstance(result_meta, dict):
        result_meta = {}
    prompt_report = result_meta.get("systemPromptReport", {})
    if not isinstance(prompt_report, dict):
        prompt_report = {}
    metadata = request.get("metadata", {})
    configured_session_id = metadata.get("session_id")
    configured_session_key = metadata.get("session_key")
    resolved_session_id = (
        str(configured_session_id)
        if configured_session_id
        else session_id_from_key(str(configured_session_key))
        if configured_session_key
        else ""
    )
    gateway_session_key = str(prompt_report.get("sessionKey") or "")
    response = {
        "status": payload.get("status", "ok"),
        "agent": "openclaw",
        "transport": "cli",
        "command": cmd,
        "stderr": proc.stderr,
        "request": request,
        "raw": payload,
        "output": {
            "session_id": resolved_session_id,
            "session_key": str(configured_session_key or ""),
            "session_handle": {
                "session_id": resolved_session_id,
                "session_key": str(configured_session_key or ""),
                "gateway_session_key": gateway_session_key,
            },
        },
        "turns": result_payload.get("payloads", []),
        "artifacts": collect_openclaw_session_artifacts(
            request,
            agent_id=str(metadata.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID") or "main"),
            session_id=resolved_session_id,
        ),
        "metrics": [
            {
                "name": "duration_ms",
                "value": result_payload.get("meta", {}).get("durationMs", 0),
            }
        ],
    }
    json.dump(response, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
