from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunRecord(BaseModel):
    run_id: str
    benchmark_id: str
    agent_id: str
    benchmark_version: str | None = None
    agent_version: str | None = None
    memory_backend: str | None = None
    hardware_profile: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: Literal["pending", "running", "passed", "failed", "partial", "stubbed"]


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


class ExecutionSpec(BaseModel):
    mode: Literal["single_turn", "multi_turn"] = "single_turn"
    requires_stateful_agent: bool = False
    task_isolation: Literal["per_run", "per_task", "per_turn"] = "per_task"
    max_parallel_tasks: int = 1
    turn_ordering: Literal["strict", "none"] = "strict"


class RenderedTaskInput(BaseModel):
    task_id: str
    system_prompt: str | None = None
    messages: list[dict[str, str]] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class JudgeInput(BaseModel):
    task_id: str
    question: str | None = None
    expected_answer: str | None = None
    agent_answer: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class ArtifactRecord(BaseModel):
    artifact_id: str
    run_id: str
    task_id: str | None = None
    turn_id: str | None = None
    kind: str
    path: str
    content_type: str | None = None
    size_bytes: int | None = None
    tags: list[str] = Field(default_factory=list)


class MetricRecord(BaseModel):
    metric_id: str
    run_id: str
    task_id: str | None = None
    turn_id: str | None = None
    scope: Literal["run", "task", "turn"]
    name: str
    value: int | float | str | bool
    unit: str | None = None
    dimension: dict[str, str] = Field(default_factory=dict)


class JudgeResult(BaseModel):
    judge_id: str
    run_id: str
    task_id: str
    score: float | None = None
    label: str | None = None
    passed: bool | None = None
    rationale: str | None = None
    raw_output_ref: str | None = None
