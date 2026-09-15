from __future__ import annotations

import base64
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

from .bundle import BundleWriter
from .protocol import (
    ProviderRequest,
    ProviderResponse,
    TraceMatching,
    TraceRecord,
    TraceScope,
    TraceStreamFrame,
    TraceTiming,
    TraceBundleManifest,
)
from .redaction import redact_json


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    return (
        f"event: {event}\n".encode()
        + b"data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        + b"\n\n"
    )


def _append_frame(
    frames: list[TraceStreamFrame], event: str, payload: dict[str, Any]
) -> None:
    frames.append(
        TraceStreamFrame(
            ordinal=len(frames) + 1,
            raw_base64=base64.b64encode(_sse(event, payload)).decode(),
            event=event,
            data=payload,
        )
    )


def _assistant_output(message: dict[str, Any]) -> tuple[list[dict[str, Any]], list[TraceStreamFrame]]:
    content = message.get("content", "")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    output: list[dict[str, Any]] = []
    frames: list[TraceStreamFrame] = []
    response_id = "resp-" + uuid.uuid4().hex[:12]
    _append_frame(
        frames,
        "response.created",
        {
            "type": "response.created",
            "response": {"id": response_id, "status": "in_progress"},
        },
    )
    for index, part in enumerate(content if isinstance(content, list) else []):
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in {"toolCall", "tool_call", "function_call"}:
            tool_id = str(part.get("id") or part.get("toolCallId") or f"call-{uuid.uuid4().hex[:8]}")
            name = str(part.get("name") or part.get("function", {}).get("name") or "")
            arguments = part.get("arguments") or part.get("function", {}).get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    pass
            arguments = redact_json(arguments)
            argument_text = (
                arguments
                if isinstance(arguments, str)
                else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            )
            item = {
                "type": "function_call",
                "id": tool_id,
                "call_id": tool_id,
                "name": name,
                "arguments": argument_text,
            }
            output.append(item)
            _append_frame(
                frames,
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": index, "item": item},
            )
            _append_frame(
                frames,
                "response.function_call_arguments.delta",
                {"type": "response.function_call_arguments.delta", "item_id": tool_id, "output_index": index, "delta": argument_text},
            )
        elif kind in {"text", "output_text"} and part.get("text"):
            text = redact_json(str(part["text"]))
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            )
            _append_frame(
                frames,
                "response.output_text.delta",
                {"type": "response.output_text.delta", "output_index": index, "delta": text},
            )
    _append_frame(
        frames,
        "response.completed",
        {
            "type": "response.completed",
            "response": {"id": response_id, "status": "completed", "output": output},
        },
    )
    return output, frames


def import_openclaw_session(
    session_jsonl: Path,
    output_bundle: Path,
    *,
    case_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> TraceBundleManifest:
    if not session_jsonl.is_file():
        raise FileNotFoundError(f"OpenClaw session JSONL does not exist: {session_jsonl}")
    resolved_session_id = session_id or session_jsonl.stem
    writer = BundleWriter(
        output_bundle,
        bundle_id=output_bundle.name,
        profile_id="openai-responses@1",
        channels={"agent_chat": {"protocol": "openai-responses", "match_mode": "ordered", "order_scope": "session"}},
    )
    ordinal = 0
    pending_users: deque[dict[str, Any]] = deque()
    malformed_lines = 0
    try:
        with session_jsonl.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    malformed_lines += 1
                    continue
                if row.get("type") == "session" and session_id is None:
                    resolved_session_id = str(row.get("id") or resolved_session_id)
                message = row.get("message")
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role in {"user", "toolResult", "tool"}:
                    pending_users.append(message)
                    continue
                if role != "assistant" or not pending_users:
                    continue
                pending_user = pending_users.popleft()
                ordinal += 1
                output, frames = _assistant_output(message)
                response = {
                    "id": f"resp-imported-{ordinal}",
                    "object": "response",
                    "status": "completed",
                    "output": output,
                }
                scope = TraceScope(
                    case_id=case_id,
                    user_id=user_id,
                    session_id=resolved_session_id,
                    request_id=f"{resolved_session_id}-{ordinal}",
                )
                request = ProviderRequest(
                    method="POST",
                    path="/v1/responses",
                    headers={"content-type": "application/json"},
                    body=redact_json(
                        {
                            "model": message.get("model", "imported"),
                            "input": pending_user.get("content", ""),
                            "stream": True,
                        }
                    ),
                )
                record = TraceRecord(
                    trace_id=f"{resolved_session_id}-{ordinal}",
                    ordinal=ordinal,
                    channel="agent_chat",
                    protocol="openai-responses",
                    scope=scope,
                    request=request,
                    response=ProviderResponse(
                        status=200,
                        headers={"content-type": "text/event-stream"},
                        body=response,
                        raw_body_base64=base64.b64encode(
                            b"".join(frame.body_bytes() for frame in frames)
                        ).decode(),
                        stream_frames=frames,
                    ),
                    timing=TraceTiming(
                        started_at=datetime.now(timezone.utc).isoformat(),
                        upstream_duration_ms=0,
                    ),
                    matching=TraceMatching(
                        mode="ordered",
                        key_version="openai-responses/1",
                        key=resolved_session_id,
                    ),
                )
                writer.append(record)
        if malformed_lines:
            raise ValueError(
                f"invalid JSON in {malformed_lines} line(s) while importing {session_jsonl}"
            )
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise


def import_session_jsonl(
    session_jsonl: Path,
    output_bundle: Path,
    *,
    case_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> TraceBundleManifest:
    """Compatibility name used by the openclaw-session Trace Skill."""
    return import_openclaw_session(
        session_jsonl,
        output_bundle,
        case_id=case_id,
        user_id=user_id,
        session_id=session_id,
    )
