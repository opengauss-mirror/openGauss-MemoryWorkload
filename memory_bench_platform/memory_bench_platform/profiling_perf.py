"""Owned perf-stat sessions scoped to a physical target and phase."""
from __future__ import annotations

import csv
import json
import math
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .profiling_archive import CollectorStatus
from .profiling_config import ProfilingConfig
from .profiling_targets import CapabilityResult, TargetIdentity, perf_cgroup_name, probe_target, target_drifted


@dataclass
class CollectorHandle:
    target_id: str
    phase_id: str
    state: str
    reason: str | None = None
    process: object | None = None
    record_process: object | None = None
    record_stop: threading.Event = field(default_factory=threading.Event)
    record_watcher: threading.Thread | None = None
    record_overflow: bool = False
    result: CollectorStatus | None = None


class ProfilerSession:
    def __init__(self, config: ProfilingConfig, targets: list[TargetIdentity], output_dir: Path,
                 command_factory: Callable = subprocess.Popen, probe: Callable | None = None, docker=None):
        self.config = config
        self.targets = {target.target_id: target for target in targets}
        self.output_dir = output_dir
        self.command_factory = command_factory
        self.probe = probe or (lambda target, config: probe_target(target, config, docker))
        self.docker = docker
        self.handles: dict[tuple[str, str], CollectorHandle] = {}
        self.root = output_dir / "profiling"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self._persist()

    def _persist(self) -> None:
        items = [{"target_id": item.target_id, "phase_id": item.phase_id,
                  "state": item.result.state if item.result else item.state,
                  "reason": item.result.reason if item.result else item.reason}
                 for item in self.handles.values()]
        (self.root / "collector_status.json").write_text(json.dumps(
            {"schema": "collector-status/1", "items": items}, indent=2), encoding="utf-8")

    def start_phase(self, phase_id: str, target_ids: list[str]) -> list[CollectorHandle]:
        result = []
        try:
            for target_id in dict.fromkeys(target_ids):
                if target_id not in self.targets:
                    raise ValueError(f"unknown target: {target_id}")
                key = (target_id, phase_id)
                if key in self.handles:
                    raise ValueError("duplicate target phase")
                target = self.targets[target_id]
                handle = CollectorHandle(target_id, phase_id, "probing")
                self.handles[key] = handle
                result.append(handle)
                self._persist()
                capability: CapabilityResult = self.probe(target, self.config)
                if capability.state != "ready":
                    handle.state, handle.reason = capability.state, capability.reason
                    self._persist()
                    if self.config.mode == "required":
                        raise RuntimeError(capability.reason or "collector unavailable")
                    continue
                events = ("cycles,instructions,task-clock,cache-misses,branches,branch-misses" if capability.event_group == "basic" else
                          "topdown-retiring,topdown-bad-spec,topdown-fe-bound,topdown-be-bound")
                argv = ["perf", "stat", "-x", ";", "--no-big-num", "-a", "-e", events,
                        "-G", perf_cgroup_name(target.cgroup_path), "--", "sleep", "infinity"]
                try:
                    handle.process = self.command_factory(argv, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, start_new_session=True)
                    time.sleep(0.05)
                    if handle.process.poll() is not None:
                        raise OSError("perf exited before phase start")
                    if self.config.hardware.record == "selected_phases" and phase_id in self.config.hardware.record_phases:
                        raw_dir = self.root / "raw" / target_id / phase_id
                        raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                        record_path = raw_dir / "perf.data"
                        record_argv = ["perf", "record", "-a", "-e", "cycles", "-G", perf_cgroup_name(target.cgroup_path),
                                       "-o", str(record_path), "--", "sleep", "infinity"]
                        handle.record_process = self.command_factory(record_argv, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
                        time.sleep(0.05)
                        if handle.record_process.poll() is not None:
                            raise OSError("perf record exited before phase start")
                        def watch_record(handle=handle, record_path=record_path):
                            while not handle.record_stop.wait(0.05):
                                try:
                                    if record_path.stat().st_size <= self.config.hardware.max_raw_bytes:
                                        continue
                                except FileNotFoundError:
                                    continue
                                handle.record_overflow = True
                                try:
                                    os.killpg(handle.record_process.pid, signal.SIGINT)
                                except OSError:
                                    pass
                                return
                        handle.record_watcher = threading.Thread(target=watch_record, daemon=True)
                        handle.record_watcher.start()
                except OSError as exc:
                    handle.record_stop.set()
                    if handle.record_watcher is not None:
                        handle.record_watcher.join(timeout=1)
                    if handle.process is not None:
                        self._stop_process(handle.process)
                    if handle.record_process is not None:
                        self._stop_process(handle.record_process)
                    handle.state, handle.reason = "failed", "collector_start_failed"
                    self._persist()
                    if self.config.mode == "required":
                        raise RuntimeError("collector_start_failed") from exc
                    continue
                handle.state = "running"
                self._persist()
        except BaseException:
            self.stop_phase(result)
            raise
        return result

    def stop_phase(self, handles: list[CollectorHandle]) -> list[CollectorStatus]:
        result = []
        for handle in handles:
            if handle.result is not None:
                result.append(handle.result)
                continue
            if handle.process is None:
                handle.result = CollectorStatus(target_id=handle.target_id, phase_id=handle.phase_id,
                                                state=handle.state, reason=handle.reason)
                result.append(handle.result)
                continue
            process = handle.process
            raw = ""
            stop_failed = False
            try:
                raw = self._stop_process(process)
            except (OSError, subprocess.TimeoutExpired):
                stop_failed = True
            finally:
                handle.record_stop.set()
                if handle.record_watcher is not None:
                    handle.record_watcher.join(timeout=1)
                if handle.record_process is not None:
                    try:
                        self._stop_process(handle.record_process)
                    except (OSError, subprocess.TimeoutExpired):
                        stop_failed = True
            if stop_failed:
                handle.state, handle.reason = "failed", "collector_timeout"
            raw = raw or ""
            encoded = raw.encode("utf-8", errors="replace")
            if len(encoded) > self.config.hardware.max_raw_bytes:
                handle.state, handle.reason = "invalid", "raw_size_limit"
                encoded = encoded[:self.config.hardware.max_raw_bytes]
            raw_dir = self.root / "raw" / handle.target_id / handle.phase_id
            raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            raw_path = raw_dir / "perf-stat.txt"
            raw_path.write_bytes(encoded)
            raw_path.chmod(0o600)
            record_path = raw_dir / "perf.data"
            if handle.record_overflow:
                handle.state, handle.reason = "invalid", "raw_size_limit"
            if handle.record_process is not None and not record_path.is_file():
                handle.state, handle.reason = "invalid", "record_missing"
            if record_path.is_file():
                record_path.chmod(0o600)
                if record_path.stat().st_size > self.config.hardware.max_raw_bytes:
                    handle.state, handle.reason = "invalid", "raw_size_limit"
            if handle.state == "running":
                try:
                    counters = _parse_stat(raw, has_cgroup=True)
                except ValueError:
                    handle.state, handle.reason = "invalid", "unparseable_counter"
                else:
                    if any(row["running_fraction"] < self.config.quality_gates.min_counter_running_fraction for row in counters):
                        handle.state, handle.reason = "invalid", "low_running_fraction"
                    else:
                        self._append_counters(handle, counters)
                        if self.config.hardware.stat == "topdown_l1":
                            if not self._append_topdown(handle, counters):
                                handle.state, handle.reason = "invalid", "invalid_topdown"
                            else:
                                handle.state = "completed"
                        else:
                            handle.state = "completed"
            target = self.targets[handle.target_id]
            try:
                drifted = target_drifted(target, self.docker)
            except (OSError, ValueError):
                drifted = True
            if drifted:
                handle.state, handle.reason = "invalid", "target_drift"
            handle.result = CollectorStatus(target_id=handle.target_id, phase_id=handle.phase_id,
                                            state=handle.state, reason=handle.reason)
            result.append(handle.result)
        self._persist()
        return result

    @staticmethod
    def _stop_process(process) -> str:
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
            _, raw = process.communicate(timeout=10)
            return raw or ""
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise

    def _append_counters(self, handle: CollectorHandle, counters: list[dict]) -> None:
        path = self.root / "perf_counters.csv"
        fields = ["target_id", "phase_id", "event", "value", "running_fraction", "cpu_scope", "event_group"]
        with path.open("a", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            if stream.tell() == 0:
                writer.writeheader()
            target = self.targets[handle.target_id]
            for row in counters:
                writer.writerow({"target_id": handle.target_id, "phase_id": handle.phase_id,
                    "cpu_scope": target.cpu_scope or "all", "event_group": self.config.hardware.stat,
                    **{key: row[key] for key in ("event", "value", "running_fraction")}})

    def _append_topdown(self, handle: CollectorHandle, counters: list[dict]) -> bool:
        mapping = {"topdown-fe-bound": "frontend_bound", "topdown-bad-spec": "bad_speculation",
                   "topdown-be-bound": "backend_bound", "topdown-retiring": "retiring"}
        selected = [row for row in counters if row["event"] in mapping]
        runtimes = [row.get("event_runtime") for row in selected]
        if (len(selected) != 4 or {row["event"] for row in selected} != set(mapping) or
                any(runtime is None or runtime <= 0 for runtime in runtimes) or
                max(runtimes) - min(runtimes) > max(runtimes) * 0.01):
            return False
        if all("metric_percent" in row for row in selected):
            values = {mapping[row["event"]]: row["metric_percent"] for row in selected}
        elif (all("metric_percent" not in row for row in selected) and
              all(row["unit"].lower() in {"slot", "slots"} for row in selected)):
            total = sum(row["value"] for row in selected)
            if total <= 0:
                return False
            values = {mapping[row["event"]]: row["value"] / total * 100 for row in selected}
        else:
            return False
        if any(not 0 <= value <= 100 for value in values.values()) or not 95 <= sum(values.values()) <= 105:
            return False
        path = self.root / "topdown_summary.csv"
        fields = ["target_id", "phase_id", "cpu_scope", "event_group", *mapping.values()]
        with path.open("a", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            if stream.tell() == 0:
                writer.writeheader()
            target = self.targets[handle.target_id]
            writer.writerow({"target_id": handle.target_id, "phase_id": handle.phase_id,
                "cpu_scope": target.cpu_scope or "all", "event_group": "topdown_l1", **values})
        return True


def _parse_stat(raw: str, *, has_cgroup: bool = False) -> list[dict]:
    result = []
    for line in raw.splitlines():
        parts = line.split(";")
        if len(parts) < 3 or not parts[2]:
            continue
        value, _, event = parts[:3]
        if "not supported" in value or "not counted" in value:
            raise ValueError("unsupported counter")
        try:
            runtime_index = 4 if has_cgroup else 3
            fraction_index = runtime_index + 1
            fraction = float(parts[fraction_index].rstrip("%")) / 100 if len(parts) > fraction_index and parts[fraction_index] else 1.0
            row = {"event": event, "unit": parts[1].strip(), "value": float(value), "running_fraction": fraction,
                   "event_runtime": float(parts[runtime_index]) if len(parts) > runtime_index and parts[runtime_index] else None}
            if (len(parts) > fraction_index + 2 and parts[fraction_index + 1]
                    and parts[fraction_index + 2].strip().lower() in {"%", "percent"}):
                row["metric_percent"] = float(parts[fraction_index + 1])
            if not all(math.isfinite(number) for number in row.values() if isinstance(number, float)):
                raise ValueError("invalid perf stat row")
            result.append(row)
        except ValueError as exc:
            raise ValueError("invalid perf stat row") from exc
    if not result:
        raise ValueError("empty perf stat output")
    return result
