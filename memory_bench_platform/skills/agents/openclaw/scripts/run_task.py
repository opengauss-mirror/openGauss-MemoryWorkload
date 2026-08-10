from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys


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


def main() -> None:
    request = json.load(sys.stdin)
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
        "command": cmd,
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
        "artifacts": [],
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
