from __future__ import annotations

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


def build_openclaw_command(request: dict) -> list[str]:
    metadata = request.get("metadata", {})
    agent_id = metadata.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID")
    session_key = metadata.get("session_key")
    session_id = metadata.get("session_id")
    target = metadata.get("to")
    if not any([agent_id, session_key, session_id, target]):
        raise ValueError("OpenClaw runner needs one of agent_id, session_key, session_id, or to")

    cmd = [_resolve_openclaw_bin(), "agent", "--message", _extract_message(request), "--json"]
    if agent_id:
        cmd += ["--agent", str(agent_id)]
    if session_key:
        cmd += ["--session-key", str(session_key)]
    if session_id:
        cmd += ["--session-id", str(session_id)]
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
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=90, check=True)
    payload = json.loads(proc.stdout)
    response = {
        "status": payload.get("status", "ok"),
        "agent": "openclaw",
        "command": cmd,
        "request": request,
        "raw": payload,
        "turns": payload.get("result", {}).get("payloads", []),
        "artifacts": [],
        "metrics": [
            {
                "name": "duration_ms",
                "value": payload.get("result", {}).get("meta", {}).get("durationMs", 0),
            }
        ],
    }
    json.dump(response, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
