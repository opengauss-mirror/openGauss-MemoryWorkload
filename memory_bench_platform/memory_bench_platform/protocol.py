from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


TraceMode = Literal[
    "off",
    "capture",
    "replay-zero-delay",
    "replay-with-delay",
    "mock-fixed",
]


class TraceChannelConfig(BaseModel):
    protocol: Literal[
        "openai-chat-completions",
        "openai-responses",
        "openai-embeddings",
        "memory-http",
        "openclaw-session",
    ]
    mode: TraceMode | None = None
    upstream_base_url: str | None = None
    listen_url: str | None = None
    match_mode: Literal["strict", "fingerprint", "compat", "ordered"]
    order_scope: Literal["global", "session", "user", "fingerprint"]
    concurrency: int = Field(default=64, gt=0)
    backlog: int = Field(default=1024, gt=0)
    request_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    idle_connection_timeout_seconds: float = Field(
        default=30, gt=0, allow_inf_nan=False
    )
    max_body_bytes: int = Field(default=16 * 1024 * 1024, gt=0)
    redact_json_pointers: list[str] = Field(default_factory=list)

    @field_validator("redact_json_pointers")
    @classmethod
    def validate_redact_json_pointers(cls, pointers: list[str]) -> list[str]:
        invalid = [
            pointer
            for pointer in pointers
            if (pointer and not pointer.startswith("/"))
            or re.search(r"~(?![01])", pointer)
        ]
        if invalid:
            raise ValueError("redact_json_pointers must use JSON Pointer syntax")
        return pointers


class TraceRuntimeConfig(BaseModel):
    mode: TraceMode
    deployment: Literal["managed", "external"] = "managed"
    profile_id: str
    bundle_path: str | None = None
    output_path: str | None = None
    copies: int = Field(default=1, gt=0)
    delay_scale: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    startup_timeout_seconds: int = Field(default=120, gt=0)
    drain_timeout_seconds: int = Field(default=300, ge=0)
    channels: dict[str, TraceChannelConfig]

    @model_validator(mode="after")
    def require_mode_inputs(self) -> "TraceRuntimeConfig":
        if self.mode == "capture":
            if self.deployment == "managed" and not self.output_path:
                raise ValueError("capture mode requires output_path")
            missing = [
                name
                for name, channel in self.channels.items()
                if self.deployment == "managed"
                and (channel.mode or self.mode) == "capture"
                and not channel.upstream_base_url
            ]
            if missing:
                raise ValueError(
                    "capture channels require upstream_base_url: " + ", ".join(missing)
                )
        if self.mode.startswith("replay-") and not self.bundle_path:
            raise ValueError(f"{self.mode} requires bundle_path")
        if not math.isfinite(self.delay_scale):
            raise ValueError("delay_scale must be finite")
        return self


class TraceCounterSnapshot(BaseModel):
    loaded: int = 0
    matched: int = 0
    mismatched: int = 0
    remaining: int = 0
    errors: int = 0
    active: int = 0
    queued: int = 0
    peak_active: int = 0


class TraceRuntimeSummary(BaseModel):
    mode: TraceMode
    bundle_id: str | None = None
    valid: bool
    channels: dict[str, TraceCounterSnapshot] = Field(default_factory=dict)


class RunRecord(BaseModel):
    run_id: str
    source_id: str
    source_kind: Literal[
        "benchmark_case_source",
        "benchmark_scenario",
        "native_workflow",
        "external_benchmark_runner",
    ]
    operator_targets: list[str] = Field(default_factory=list)
    benchmark_skill_version: str | None = None
    benchmark_version: str | None = None
    agent_id: str | None = None
    agent_skill_version: str | None = None
    agent_version: str | None = None
    memory_backend: str | None = None
    hardware_profile: str | None = None
    benchmark_version_policy: dict[str, Any] = Field(default_factory=dict)
    agent_version_policy: dict[str, Any] = Field(default_factory=dict)
    version_selection: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: Literal["pending", "running", "passed", "failed", "partial", "invalid", "stubbed"]


class CaseRecord(BaseModel):
    case_id: str
    run_id: str
    title: str
    goal: str
    capability: str
    depends_on_cases: list[str] = Field(default_factory=list)
    reference: dict[str, Any] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    judge_mode: Literal["none", "builtin", "external"] = "builtin"


class StepRecord(BaseModel):
    step_id: str
    case_id: str
    name: str
    operator_kind: Literal["bash", "http", "agent", "wait", "memory", "memory_plugin", "poll"]
    depends_on: list[str] = Field(default_factory=list)
    retry_limit: int = 0
    timeout_seconds: int | None = None
    gate_policy: Literal["hard", "soft", "none"] = "none"
    inputs: dict[str, Any] = Field(default_factory=dict)


class StepResultRecord(BaseModel):
    step_result_id: str
    step_id: str
    attempt: int
    status: Literal["pending", "running", "passed", "failed", "skipped"]
    exit_code: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    structured_output: dict[str, Any] = Field(default_factory=dict)
    gate_passed: bool | None = None
    gate_detail: str | None = None


class TraceEventRecord(BaseModel):
    trace_id: str
    case_id: str
    step_id: str | None = None
    event_type: Literal[
        "step_started",
        "step_finished",
        "gate_passed",
        "gate_failed",
        "retry_scheduled",
        "poll_probe",
        "case_judge_started",
        "case_judge_finished",
    ]
    timestamp: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ExecutionSpec(BaseModel):
    case_mode: Literal["single_path", "dag"] = "single_path"
    max_parallel_steps: int = 1
    fail_fast: bool = True
    default_retry_limit: int = 0
    default_timeout_seconds: int | None = None


class RenderedTaskInput(BaseModel):
    task_id: str
    system_prompt: str | None = None
    messages: list[dict[str, str]] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowRuntimeContext(BaseModel):
    run_id: str
    run_dir: str
    benchmark_id: str
    agent_id: str
    memory_id: str | None = None
    memory_integration: Literal["backend_direct", "agent_plugin"] = "backend_direct"
    memory_plugin_id: str | None = None
    run_contract: dict[str, Any] = Field(default_factory=dict)
    version_selection: dict[str, Any] = Field(default_factory=dict)


class MemoryTaskInput(BaseModel):
    protocol_version: Literal["memory/1"] = "memory/1"
    task_id: str
    action: Literal[
        "ingest",
        "flush",
        "commit",
        "status",
        "wait_ready",
        "recall",
        "inspect_memory",
        "consistency",
    ]
    inputs: dict[str, Any] = Field(default_factory=dict)
    runtime_context: WorkflowRuntimeContext
    idempotency_key: str
    checkpoint_id: str | None = None
    scope_id: str | None = None


class MemoryTaskOutput(BaseModel):
    protocol_version: Literal["memory/1"] = "memory/1"
    status: Literal["ok", "failed"]
    state: Literal["accepted", "running", "completed", "failed"]
    operation: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, Any] = Field(default_factory=dict)


class MemoryPluginTaskInput(BaseModel):
    protocol_version: Literal["memory-plugin/1"] = "memory-plugin/1"
    task_id: str
    action: Literal[
        "validate",
        "prepare",
        "set_phase",
        "commit",
        "wait_ready",
        "flush",
        "wait_settle",
        "enter_qa",
        "before_agent",
        "after_agent",
        "finalize",
    ]
    inputs: dict[str, Any] = Field(default_factory=dict)
    runtime_context: WorkflowRuntimeContext
    idempotency_key: str
    checkpoint_id: str | None = None
    scope_id: str | None = None


class MemoryPluginTaskOutput(BaseModel):
    protocol_version: Literal["memory-plugin/1"] = "memory-plugin/1"
    status: Literal["ok", "failed"]
    state: Literal["accepted", "running", "completed", "failed"]
    operation: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, Any] = Field(default_factory=dict)


class JudgeInput(BaseModel):
    case_id: str
    goal: str | None = None
    reference: dict[str, Any] = Field(default_factory=dict)
    step_results: list[dict[str, Any]] = Field(default_factory=list)
    trace_events: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    resource_summary: dict[str, Any] = Field(default_factory=dict)


class ArtifactRecord(BaseModel):
    artifact_id: str
    run_id: str
    case_id: str | None = None
    step_id: str | None = None
    kind: str
    path: str
    content_type: str | None = None
    size_bytes: int | None = None
    tags: list[str] = Field(default_factory=list)


class MetricRecord(BaseModel):
    metric_id: str
    run_id: str
    case_id: str | None = None
    step_id: str | None = None
    scope: Literal["run", "case", "step"]
    name: str
    value: int | float | str | bool
    unit: str | None = None
    dimension: dict[str, str] = Field(default_factory=dict)


class JudgeResult(BaseModel):
    judge_id: str
    run_id: str
    case_id: str
    score: float | None = None
    label: str | None = None
    passed: bool | None = None
    rationale: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    raw_output_ref: str | None = None


class ReportSummary(BaseModel):
    run_id: str
    status: Literal["pending", "running", "passed", "failed", "partial", "invalid", "stubbed"]
    case_total: int
    case_passed: int
    case_failed: int
    case_ungraded: int = 0
    benchmark_score: float | None = None
    raw_benchmark_score: float | None = None
    evaluation_coverage: float | None = None
    checkpoint_ready_rate: float | None = None
    runtime_failure_rate: float | None = None
    readiness_latency_ms: float | None = None
    run_validity: dict[str, Any] = Field(default_factory=dict)
    resource_summary: dict[str, Any] = Field(default_factory=dict)
    category_summary: dict[str, Any] = Field(default_factory=dict)


class EntryPointRecord(BaseModel):
    entrypoint_id: str
    entrypoint_kind: Literal["case_builder", "scenario_builder", "external_runner"]
    command: list[str] = Field(default_factory=list)
    output_dir: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    task_id: str
    run_id: str
    sample_id: str
    split: str | None = None
    scenario: str | None = None
    input_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    judge_mode: Literal["none", "builtin", "external"] = "none"


class TurnRecord(BaseModel):
    turn_id: str
    task_id: str
    index: int
    role: Literal["system", "user", "agent", "tool", "benchmark"]
    content: str
    timestamp: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
