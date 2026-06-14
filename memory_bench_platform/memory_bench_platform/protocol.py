from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunRecord(BaseModel):
    run_id: str
    source_id: str
    source_kind: Literal["benchmark_case_source", "native_workflow"]
    operator_targets: list[str] = Field(default_factory=list)
    benchmark_version: str | None = None
    agent_id: str | None = None
    agent_version: str | None = None
    memory_backend: str | None = None
    hardware_profile: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: Literal["pending", "running", "passed", "failed", "partial", "stubbed"]


class CaseRecord(BaseModel):
    case_id: str
    run_id: str
    title: str
    goal: str
    capability: str
    reference: dict[str, Any] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    judge_mode: Literal["none", "builtin", "external"] = "builtin"


class StepRecord(BaseModel):
    step_id: str
    case_id: str
    name: str
    operator_kind: Literal["bash", "http", "agent", "wait"]
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
    status: Literal["pending", "running", "passed", "failed", "partial", "stubbed"]
    case_total: int
    case_passed: int
    case_failed: int
    resource_summary: dict[str, Any] = Field(default_factory=dict)
    category_summary: dict[str, Any] = Field(default_factory=dict)


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
