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
    upstream: str | None = None
    required: bool = True
    record_runtime_version: bool = True
    notes: str | None = None


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
    version_policy: VersionPolicy
    dataset: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    judging: dict[str, Any] = Field(default_factory=dict)

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

    @model_validator(mode="before")
    @classmethod
    def require_explicit_version_policy(cls, data: Any) -> Any:
        if isinstance(data, dict) and "version_policy" not in data:
            raise ValueError(
                "agent manifest must declare version_policy explicitly; "
                "default software selection should stay machine-readable"
            )
        return data
