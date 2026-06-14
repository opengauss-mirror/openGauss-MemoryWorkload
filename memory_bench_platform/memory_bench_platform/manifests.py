from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EntryPoints(BaseModel):
    case_builder: str | None = None
    task_builder: str | None = None
    scorer: str | None = None
    validator: str | None = None
    healthcheck: str | None = None
    launcher: str | None = None
    runner: str | None = None
    collector: str | None = None
    teardown: str | None = None


class BenchmarkManifest(BaseModel):
    kind: Literal["benchmark"] = "benchmark"
    id: str
    version: str
    entry: EntryPoints
    dataset: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    judging: dict[str, Any] = Field(default_factory=dict)


class AgentManifest(BaseModel):
    kind: Literal["agent"] = "agent"
    id: str
    version: str
    entry: EntryPoints
    runtime: dict[str, Any] = Field(default_factory=dict)
    io: dict[str, Any] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    collection: dict[str, Any] = Field(default_factory=dict)
