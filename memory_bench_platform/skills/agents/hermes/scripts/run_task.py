from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


def _resolve_hermes_bin() -> str:
    custom = os.environ.get("HERMES_BIN")
    if custom:
        return custom
    found = shutil.which("hermes")
    if found:
        return found
    raise FileNotFoundError("hermes binary not found")


def render_hermes_prompt(request: dict) -> str:
    lines: list[str] = []
    system_prompt = str(request.get("system_prompt", "") or "").strip()
    if system_prompt:
        lines.append("System instructions:")
        lines.append(system_prompt)
        lines.append("")
    lines.append("Conversation:")
    for message in request.get("messages", []):
        role = str(message.get("role", "user"))
        content = str(message.get("content", ""))
        lines.append(f"[{role}] {content}")
    attachments = request.get("attachments", [])
    if attachments:
        lines.append("")
        lines.append("Attachments:")
        for item in attachments:
            lines.append(f"- {item}")
    return "\n".join(lines).strip()


def build_hermes_command(request: dict) -> list[str]:
    metadata = request.get("metadata", {})
    cmd = [_resolve_hermes_bin(), "chat", "-q", render_hermes_prompt(request), "-Q", "--ignore-rules"]
    model = metadata.get("model") or os.environ.get("HERMES_MODEL")
    if model:
        cmd += ["--model", str(model)]
    provider = metadata.get("provider") or os.environ.get("HERMES_PROVIDER")
    if provider:
        cmd += ["--provider", str(provider)]
    toolsets = metadata.get("toolsets")
    if toolsets:
        cmd += ["--toolsets", str(toolsets)]
    return cmd


def main() -> None:
    request = json.load(sys.stdin)
    cmd = build_hermes_command(request)
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=180, check=False)
    response = {
        "status": "ok" if proc.returncode == 0 and proc.stdout.strip() else "failed",
        "agent": "hermes",
        "command": cmd,
        "request": request,
        "turns": [{"text": proc.stdout.strip()}] if proc.stdout.strip() else [],
        "artifacts": [],
        "metrics": [{"name": "duration_ms", "value": 0}],
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "exit_code": proc.returncode,
    }
    json.dump(response, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
