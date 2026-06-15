from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class VersionPolicy(BaseModel):
    default_selection: Literal["latest_official_release_tag"] = "latest_official_release_tag"
    allowed_overrides: list[str] = Field(
        default_factory=lambda: [
            "user_specified_official_version",
            "verified_fallback_release_tag",
            "historical_repro_release_tag",
        ]
    )
    disallowed_defaults: list[str] = Field(
        default_factory=lambda: [
            "dirty_worktree",
            "dev_build",
            "non_tag_commit",
        ]
    )
    record_runtime_version: bool = True
    notes: str | None = None


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
    version_policy: VersionPolicy = Field(default_factory=VersionPolicy)
    dataset: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    judging: dict[str, Any] = Field(default_factory=dict)


class AgentManifest(BaseModel):
    kind: Literal["agent"] = "agent"
    id: str
    version: str
    entry: EntryPoints
    version_policy: VersionPolicy = Field(default_factory=VersionPolicy)
    runtime: dict[str, Any] = Field(default_factory=dict)
    io: dict[str, Any] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    collection: dict[str, Any] = Field(default_factory=dict)
