from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class VersionTarget(BaseModel):
    name: str
    scope: Literal[
        "system_under_test",
        "benchmark_tooling",
        "runtime_dependency",
        "memory_backend",
    ]
    version_source: Literal[
        "upstream_release_tag",
        "runtime_observed_only",
    ] = "upstream_release_tag"
    upstream: str | None = None
    required: bool = True
    record_runtime_version: bool = True
    notes: str | None = None

    @model_validator(mode="after")
    def require_upstream_for_release_tag_resolution(self) -> "VersionTarget":
        if self.version_source == "upstream_release_tag" and not self.upstream:
            raise ValueError(
                "targets using upstream_release_tag must declare upstream explicitly"
            )
        return self


class VersionPolicy(BaseModel):
    default_selection: Literal["latest_official_release_tag"] = "latest_official_release_tag"
    resolution_order: list[str] = Field(
        default_factory=lambda: [
            "user_specified_official_version",
            "latest_official_release_tag",
            "verified_fallback_release_tag",
            "historical_repro_release_tag",
        ]
    )
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
    targets: list[VersionTarget] = Field(min_length=1)
    record_runtime_version: bool = True
    notes: str | None = None

    @model_validator(mode="after")
    def require_latest_tag_in_resolution_order(self) -> "VersionPolicy":
        if "latest_official_release_tag" not in self.resolution_order:
            raise ValueError(
                "version_policy.resolution_order must include latest_official_release_tag"
            )
        return self


class EntryPoints(BaseModel):
    case_builder: str | None = None
    scenario_builder: str | None = None
    task_builder: str | None = None
    scorer: str | None = None
    validator: str | None = None
    healthcheck: str | None = None
    launcher: str | None = None
    runner: str | None = None
    collector: str | None = None
    teardown: str | None = None


class SmokeEntryPoints(BaseModel):
    probe_builder: str
    validator: str
    reporter: str


class BenchmarkManifest(BaseModel):
    kind: Literal["benchmark"] = "benchmark"
    id: str
    version: str
    entry: EntryPoints
    version_policy: VersionPolicy
    dataset: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    judging: dict[str, Any] = Field(default_factory=dict)
    requirements: dict[str, Any] = Field(default_factory=dict)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    integration: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def require_explicit_version_policy(cls, data: Any) -> Any:
        if isinstance(data, dict) and "version_policy" not in data:
            raise ValueError(
                "benchmark manifest must declare version_policy explicitly; "
                "default software selection should stay machine-readable"
            )
        return data


class AgentManifest(BaseModel):
    kind: Literal["agent"] = "agent"
    id: str
    version: str
    entry: EntryPoints
    version_policy: VersionPolicy
    runtime: dict[str, Any] = Field(default_factory=dict)
    io: dict[str, Any] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    collection: dict[str, Any] = Field(default_factory=dict)
    integration: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def require_explicit_version_policy(cls, data: Any) -> Any:
        if isinstance(data, dict) and "version_policy" not in data:
            raise ValueError(
                "agent manifest must declare version_policy explicitly; "
                "default software selection should stay machine-readable"
            )
        return data


class MemoryManifest(BaseModel):
    kind: Literal["memory"] = "memory"
    id: str
    version: str
    version_policy: VersionPolicy
    entry: EntryPoints = Field(default_factory=EntryPoints)
    runtime: dict[str, Any] = Field(default_factory=dict)
    ingest: dict[str, Any] = Field(default_factory=dict)
    recall: dict[str, Any] = Field(default_factory=dict)
    completion: dict[str, Any] = Field(default_factory=dict)
    integration: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def require_explicit_version_policy(cls, data: Any) -> Any:
        if isinstance(data, dict) and "version_policy" not in data:
            raise ValueError(
                "memory manifest must declare version_policy explicitly; "
                "default software selection should stay machine-readable"
            )
        return data


class MemoryPluginManifest(BaseModel):
    kind: Literal["memory_plugin"] = "memory_plugin"
    id: str
    version: str
    agent: str
    memory: str
    entry: EntryPoints
    runtime: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    phases: dict[str, Any] = Field(default_factory=dict)
    integration: dict[str, Any] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)


class SmokeManifest(BaseModel):
    kind: Literal["smoke"] = "smoke"
    id: str
    version: str
    scope: dict[str, Any] = Field(default_factory=dict)
    entry: SmokeEntryPoints
    stages: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    pass_criteria: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
