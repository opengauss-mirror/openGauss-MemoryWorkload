"""Resolve the profile target passed to a single ordinary run."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .profiling_config import ProfilingConfig
from .profiling_perf import CollectorHandle, ProfilerSession
from .profiling_phase import PhaseSink
from .profiling_targets import TargetIdentity, resolve_targets


def prepare_phase_sink(config: ProfilingConfig, run_dir: Path, run_id: str,
                       environment: dict[str, str] | None = None) -> PhaseSink:
    env = environment if environment is not None else os.environ
    if env.get("MEMORY_BENCH_PROFILE_TARGETS"):
        rows = json.loads(env["MEMORY_BENCH_PROFILE_TARGETS"])
        if not isinstance(rows, list):
            raise ValueError("invalid profiling targets")
        targets = [TargetIdentity(**row) for row in rows]
        if {target.target_id for target in resolve_targets(config, None, targets)} != {target.target_id for target in targets}:
            raise ValueError("profile targets do not match selected deployment")
    else:
        explicit = [TargetIdentity(name, name, name, "external",
            host_pid=spec.host_pid, cgroup_path=spec.cgroup_path,
            cpu_scope=spec.cpu_scope)
            for name, spec in config.external_targets.items()]
        targets = resolve_targets(config, None, explicit)
    session = ProfilerSession(config, targets, run_dir)
    if config.mode == "required":
        for target in targets:
            capability = session.probe(target, config)
            if capability.state != "ready":
                raise RuntimeError(capability.reason or "collector unavailable")
    return PhaseSink(run_dir, run_id, session)


def mark_phase_unavailable(sink: PhaseSink, reason: str = "phase_boundary_unavailable") -> None:
    for phase in sink.session.config.phases:
        for target_id in sink.session.targets:
            key = (target_id, phase)
            if key not in sink.session.handles:
                sink.session.handles[key] = CollectorHandle(target_id, phase, "skipped", reason)
    sink.session._persist()
