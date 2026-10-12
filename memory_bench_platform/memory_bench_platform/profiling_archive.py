"""Validated, offline inputs for profiling analysis."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PhaseEvent(_Strict):
    schema_version: Literal["phase-event/1"] = Field(alias="schema")
    run_id: str
    phase_id: str
    parent_phase_id: str | None = None
    target_id: str
    start_utc: datetime
    end_utc: datetime
    start_monotonic_ns: int = Field(ge=0)
    end_monotonic_ns: int = Field(ge=0)
    state: Literal["completed", "failed", "interrupted", "not_applicable"]
    request_count: int = Field(ge=0)
    warmup: bool

    @model_validator(mode="after")
    def valid_window(self):
        if self.start_utc.tzinfo is None or self.end_utc.tzinfo is None:
            raise ValueError("phase timestamps must have a timezone")
        if self.end_monotonic_ns < self.start_monotonic_ns or self.end_utc < self.start_utc:
            raise ValueError("negative phase window")
        if self.state != "not_applicable" and self.end_monotonic_ns == self.start_monotonic_ns:
            raise ValueError("empty measured phase window")
        return self


class _TargetRegistry(_Strict):
    schema_version: Literal["target-registry/1"] = Field(alias="schema")
    targets: list[dict]


class CollectorStatus(_Strict):
    target_id: str
    phase_id: str
    state: Literal["planned", "probing", "ready", "running", "stopping", "completed", "skipped", "failed", "invalid"]
    reason: str | None = None


class _CollectorFile(_Strict):
    schema_version: Literal["collector-status/1"] = Field(alias="schema")
    items: list[CollectorStatus]


@dataclass(frozen=True)
class ClientEvent:
    run_id: str
    instance_id: str | None
    user_id: str | None
    request_id: str | None
    operation: str | None
    started_utc: datetime | None
    ended_utc: datetime | None
    status: str
    duration_ms: float | None


@dataclass(frozen=True)
class ProfileArchive:
    run_dir: Path
    phases: tuple[PhaseEvent, ...]
    targets: tuple[dict, ...]
    collectors: tuple[CollectorStatus, ...]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid profiling artifact: {path.name}") from exc


def load_profile_archive(run_dir: Path) -> ProfileArchive | None:
    phase_path = run_dir / "phase_events.jsonl"
    registry_path = run_dir / "target_registry.json"
    collector_path = run_dir / "profiling" / "collector_status.json"
    if not (phase_path.exists() or registry_path.exists() or (run_dir / "profiling").exists()):
        return None
    for path in (phase_path, registry_path, collector_path):
        if not path.is_file():
            raise ValueError(f"missing profiling artifact: {path.name}")
    try:
        registry = _TargetRegistry.model_validate(_read_json(registry_path))
        collectors = _CollectorFile.model_validate(_read_json(collector_path))
        phases = tuple(PhaseEvent.model_validate_json(line) for line in phase_path.read_text(encoding="utf-8").splitlines())
    except (OSError, ValueError) as exc:
        raise ValueError("invalid profiling archive") from exc
    target_ids = [target.get("target_id") for target in registry.targets]
    if any(not isinstance(item, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,127}", item) is None
           for item in target_ids) or len(set(target_ids)) != len(target_ids):
        raise ValueError("invalid or duplicate target")
    run_record = run_dir / "run_config.json"
    if not run_record.is_file():
        run_record = run_dir / "run.json"
    if run_record.is_file():
        run_id = _read_json(run_record).get("run_id")
        if any(phase.run_id != run_id for phase in phases):
            raise ValueError("phase run_id does not match archive")
    by_target: dict[str, list[PhaseEvent]] = {}
    seen: set[tuple[str, str]] = set()
    for phase in phases:
        if phase.target_id not in target_ids:
            raise ValueError("unknown phase target")
        key = (phase.target_id, phase.phase_id)
        if key in seen:
            raise ValueError("duplicate target phase")
        seen.add(key)
        by_target.setdefault(phase.target_id, []).append(phase)
    for events in by_target.values():
        ordered = sorted(events, key=lambda item: item.start_monotonic_ns)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                if right.start_monotonic_ns >= left.end_monotonic_ns:
                    break
                nested = (right.parent_phase_id == left.phase_id and
                          right.end_monotonic_ns <= left.end_monotonic_ns)
                if not nested:
                    raise ValueError("overlapping target phases")
    if any(item.target_id not in target_ids or (item.target_id, item.phase_id) not in seen
           for item in collectors.items):
        raise ValueError("collector references unknown target phase")
    collector_keys = [(item.target_id, item.phase_id) for item in collectors.items]
    if len(collector_keys) != len(set(collector_keys)):
        raise ValueError("duplicate collector status")
    return ProfileArchive(run_dir, phases, tuple(registry.targets), tuple(collectors.items))


def load_client_events(run_dir: Path) -> list[ClientEvent]:
    def memory_instance() -> str | None:
        registry = run_dir / "target_registry.json"
        if not registry.is_file():
            return None
        targets = _read_json(registry).get("targets", [])
        memories = {item.get("physical_instance_id") for item in targets
                    if isinstance(item, dict) and item.get("role") == "memory"}
        return next(iter(memories)) if len(memories) == 1 else None

    config_path = run_dir / "run_config.json"
    if config_path.is_file() and _read_json(config_path).get("schema") == "multi-user-run/1":
        config = _read_json(config_path)
        users = {item["user_id"]: item["instance"] for item in config["users"].values()}
        origin = datetime.fromisoformat(config.get("request_time_origin_utc", config["started_at"]))
        if origin.tzinfo is None:
            raise ValueError("run started_at must have timezone")
        rows = []
        seen = set()
        with (run_dir / "request_events.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                if users.get(event["user_id"]) != event["instance_id"]:
                    raise ValueError("invalid event binding")
                key = (event["request_id"], event["operation"])
                if key in seen:
                    raise ValueError("duplicate client event")
                seen.add(key)
                start, end = float(event["started_ms"]), float(event["ended_ms"])
                if start < 0 or end < start:
                    raise ValueError("invalid client event window")
                rows.append(ClientEvent(config["run_id"], event["instance_id"], event["user_id"],
                    event["request_id"], event["operation"], origin + timedelta(milliseconds=start),
                    origin + timedelta(milliseconds=end), event["status"], end - start))
        return rows
    replay_dir = run_dir / "external_artifacts" / "replay"
    replay_config = replay_dir / "run_config.json"
    if replay_config.is_file() and _read_json(replay_config).get("schema") == "production-replay-config/1":
        from math import isfinite
        from .external_report_import import _load_production_events
        run_id = _read_json(run_dir / "run.json")["run_id"]
        if _read_json(replay_config).get("run_id") != run_id:
            raise ValueError("production replay run_id mismatch")
        rows = []
        instance = memory_instance()
        for event in _load_production_events(replay_dir / "request_events.jsonl"):
            try:
                ended = datetime.fromisoformat(event["ts"])
                duration = float(event["duration_ms"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid production replay event window") from exc
            if ended.tzinfo is None or not isfinite(duration) or duration < 0:
                raise ValueError("invalid production replay event window")
            rows.append(ClientEvent(run_id, instance, None, event["request_id"], event["operation"],
                ended - timedelta(milliseconds=duration), ended, event["status"], duration))
        return rows
    native_path = run_dir / "profiling_client_events.jsonl"
    if native_path.is_file():
        run_id = _read_json(run_dir / "run.json")["run_id"]
        instance = memory_instance()
        rows = []
        seen = set()
        with native_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    started = datetime.fromisoformat(event["started_utc"])
                    ended = datetime.fromisoformat(event["ended_utc"])
                    key = (event["request_id"], event["operation"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("invalid native profiling event") from exc
                if (event.get("run_id") != run_id or key in seen or
                        event["operation"] not in {"add", "drain", "search"} or
                        event.get("status") not in {"ok", "failed"} or
                        started.tzinfo is None or ended.tzinfo is None or ended < started):
                    raise ValueError("invalid native profiling event")
                seen.add(key)
                rows.append(ClientEvent(run_id, instance, None, key[0], key[1], started, ended,
                    event["status"], (ended - started).total_seconds() * 1000))
        return rows
    record = _read_json(run_dir / "run.json")
    cases = _read_json(run_dir / "reports" / "case_results.json")
    if not isinstance(cases, list):
        raise ValueError("invalid case results")
    return [ClientEvent(record["run_id"], item.get("instance_id"), item.get("user_id"),
        item.get("request_id"), item.get("operation"), None, None,
        "ok" if item.get("passed") is True else "failed" if item.get("passed") is False else "ungraded",
        item.get("duration_ms")) for item in cases]
