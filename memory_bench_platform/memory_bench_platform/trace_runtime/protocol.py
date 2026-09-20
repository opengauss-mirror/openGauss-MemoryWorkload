from __future__ import annotations

import base64
import json
from typing import Any, Literal

from pydantic import BaseModel, Field


class TraceScope(BaseModel):
    run_id: str | None = None
    case_id: str | None = None
    step_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    request_id: str | None = None


class ProviderRequest(BaseModel):
    method: str
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    raw_body_base64: str | None = None


class TraceStreamFrame(BaseModel):
    ordinal: int = Field(gt=0)
    raw_base64: str
    event: str | None = None
    data: Any = None

    def body_bytes(self) -> bytes:
        return base64.b64decode(self.raw_base64)


class ProviderResponse(BaseModel):
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    raw_body_base64: str | None = None
    stream_frames: list[TraceStreamFrame] = Field(default_factory=list)

    def body_bytes(self) -> bytes:
        if self.raw_body_base64 is not None:
            return base64.b64decode(self.raw_body_base64)
        if isinstance(self.body, str):
            return self.body.encode("utf-8")
        return json.dumps(self.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class TraceTiming(BaseModel):
    started_at: str
    upstream_duration_ms: float = Field(ge=0, allow_inf_nan=False)


class TraceMatching(BaseModel):
    mode: Literal["strict", "fingerprint", "compat", "ordered"]
    key_version: str
    key: str


class TraceRedaction(BaseModel):
    policy: str = "default/1"
    applied: bool = True


class TraceRecord(BaseModel):
    schema_version: Literal["trace-record/1"] = "trace-record/1"
    trace_id: str
    ordinal: int = Field(gt=0)
    channel: str
    protocol: str
    scope: TraceScope
    request: ProviderRequest
    response: ProviderResponse
    timing: TraceTiming
    matching: TraceMatching | None = None
    redaction: TraceRedaction = Field(default_factory=TraceRedaction)


class TraceBundleChannel(BaseModel):
    protocol: str
    records: str
    record_count: int = Field(ge=0)
    sha256: str
    match_mode: Literal["strict", "fingerprint", "compat", "ordered"]
    order_scope: Literal["global", "session", "user", "fingerprint"]


class TraceBundleManifest(BaseModel):
    schema_version: Literal["trace-bundle/1"] = "trace-bundle/1"
    bundle_id: str
    profile_id: str
    created_at: str
    source_run_id: str | None = None
    workload: dict[str, Any] = Field(default_factory=dict)
    channels: dict[str, TraceBundleChannel]
    producer: dict[str, Any] = Field(default_factory=dict)
    redaction_policy: str = "default/1"
