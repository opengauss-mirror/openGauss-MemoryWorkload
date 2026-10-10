"""Strict, non-privileged profiling request configuration."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HardwareConfig(_Strict):
    stat: Literal["off", "basic", "topdown_l1"] = "basic"
    interval_ms: int = Field(default=1000, ge=100)
    record: Literal["off", "selected_phases"] = "off"
    record_phases: list[str] = Field(default_factory=list)
    max_raw_bytes: int = Field(default=1073741824, gt=0)


class TraceConfig(_Strict):
    source: Literal["adapter", "file", "off"] = "off"
    path: Path | None = None
    stage_map: Path | None = None
    max_raw_bytes: int = Field(default=1073741824, gt=0)


class QualityGates(_Strict):
    min_request_join_rate: float = Field(default=0.95, ge=0, le=1, allow_inf_nan=False)
    min_counter_running_fraction: float = Field(default=0.90, ge=0, le=1, allow_inf_nan=False)


class ExternalTarget(_Strict):
    host_pid: int = Field(gt=0)
    cgroup_path: str = Field(pattern=r"^/sys/fs/cgroup/")
    cpu_scope: str | None = None


class ProfilingConfig(_Strict):
    schema_version: Literal["profiling/1"] = Field(alias="schema")
    mode: Literal["best_effort", "required"] = "best_effort"
    phases: list[Literal["setup", "warmup", "ingest", "drain", "recall", "cooldown"]] = Field(
        default_factory=lambda: ["ingest", "drain", "recall"])
    targets: list[str]
    external_targets: dict[str, ExternalTarget] = Field(default_factory=dict)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    internal_trace: TraceConfig = Field(default_factory=TraceConfig)
    quality_gates: QualityGates = Field(default_factory=QualityGates)

    @model_validator(mode="after")
    def valid_selection(self):
        if not self.phases or not self.targets or len(set(self.phases)) != len(self.phases) or len(set(self.targets)) != len(self.targets):
            raise ValueError("empty or duplicate profiling selection")
        if not set(self.external_targets) <= set(self.targets):
            raise ValueError("external target identity is not selected")
        if self.hardware.record == "selected_phases" and not set(self.hardware.record_phases) <= set(self.phases):
            raise ValueError("record phase not selected")
        return self


def load_profiling_config(path: Path) -> ProfilingConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = ProfilingConfig.model_validate(data)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError("invalid profiling config") from exc
    trace = config.internal_trace
    if trace.source == "file" and (trace.path is None or trace.stage_map is None):
        raise ValueError("file trace needs path and stage_map")
    return config
