"""Resolve physical capture identity and probe perf capability."""
from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .deployment_store import DeploymentState
from .profiling_config import ProfilingConfig


@dataclass(frozen=True)
class TargetIdentity:
    target_id: str
    role: str
    physical_instance_id: str
    deployment_kind: str
    container_id: str | None = None
    cgroup_path: str | None = None
    host_pid: int | None = None
    image_digest: str | None = None
    restart_count: int | None = None
    start_time: str | None = None
    cpu_scope: str | None = None
    owner: str | None = None


@dataclass(frozen=True)
class CapabilityResult:
    state: str
    reason: str | None
    event_group: str | None = None
    perf_version: str | None = None
    cpu_scope: str | None = None


def resolve_targets(config: ProfilingConfig, deployment_state: DeploymentState | None,
                    explicit_targets: list[TargetIdentity], docker=None) -> list[TargetIdentity]:
    candidates = {target.target_id: target for target in explicit_targets}
    if deployment_state is not None:
        for project, services in deployment_state.containers.items():
            for role, container_id in services.items():
                target_id = f"{project}:{role}"
                inspect = docker.inspect(container_id) if docker is not None else {}
                if inspect and inspect.get("Id") != container_id:
                    raise ValueError("container identity changed")
                pid = inspect.get("State", {}).get("Pid")
                candidates[target_id] = TargetIdentity(target_id, role, container_id, "compose",
                    container_id=container_id, host_pid=pid,
                    cgroup_path=_cgroup_for_pid(pid) if isinstance(pid, int) and pid > 0 else None,
                    image_digest=inspect.get("Image") or deployment_state.image_ids.get(role),
                    restart_count=deployment_state.restart_counts.get(container_id, inspect.get("RestartCount")),
                    start_time=deployment_state.start_times.get(container_id, inspect.get("State", {}).get("StartedAt")),
                    cpu_scope=inspect.get("HostConfig", {}).get("CpusetCpus") or None,
                    owner=deployment_state.name)
    selected = []
    seen_physical = set()
    for name in config.targets:
        matches = [target for target in candidates.values() if target.target_id == name or target.role == name]
        if not matches:
            raise ValueError(f"unknown profiling target: {name}")
        if len(matches) > 1:
            raise ValueError(f"ambiguous profiling target: {name}")
        target = matches[0]
        if target.physical_instance_id not in seen_physical:
            selected.append(target)
            seen_physical.add(target.physical_instance_id)
    return selected


def _cgroup_for_pid(pid: int) -> str | None:
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            relative = line.split("::", 1)[1].lstrip("/")
            candidate = Path("/sys/fs/cgroup") / relative
            return str(candidate) if candidate.is_dir() else None
    return None


def _changed(target: TargetIdentity, docker) -> bool:
    if target.container_id is None or docker is None:
        return False
    current = docker.inspect(target.container_id)
    return (current.get("Id") != target.container_id or
            (target.restart_count is not None and current.get("RestartCount") != target.restart_count) or
            (target.start_time is not None and current.get("State", {}).get("StartedAt") != target.start_time))


def target_drifted(target: TargetIdentity, docker=None) -> bool:
    if _changed(target, docker):
        return True
    return (target.host_pid is not None and target.cgroup_path is not None and
            _cgroup_for_pid(target.host_pid) != target.cgroup_path)


def probe_target(target: TargetIdentity, config: ProfilingConfig, docker=None,
                 command: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> CapabilityResult:
    if config.hardware.stat == "off":
        return CapabilityResult("skipped", "hardware_off")
    if platform.system() != "Linux":
        return CapabilityResult("skipped", "unsupported_platform")
    if _changed(target, docker):
        return CapabilityResult("invalid", "target_drift")
    if target.cgroup_path is None:
        return CapabilityResult("skipped", "missing_target_identity")
    if target.host_pid is not None and _cgroup_for_pid(target.host_pid) != target.cgroup_path:
        return CapabilityResult("invalid", "target_identity_mismatch")
    cgroup = Path(target.cgroup_path)
    if not cgroup.is_dir() or not str(cgroup).startswith("/sys/fs/cgroup/"):
        return CapabilityResult("skipped", "invalid_cgroup")
    perf = shutil.which("perf")
    if perf is None:
        return CapabilityResult("skipped", "perf_unavailable")
    try:
        version = command([perf, "--version"], capture_output=True, text=True, timeout=5, check=False)
        if version.returncode != 0:
            return CapabilityResult("skipped", "perf_unavailable")
        events = "cycles,instructions,task-clock,cache-misses,branches,branch-misses" if config.hardware.stat == "basic" else "topdown-retiring,topdown-bad-spec,topdown-fe-bound,topdown-be-bound"
        probe = command([perf, "stat", "-a", "-G", str(cgroup), "-e", events, "--", "sleep", "0.01"],
                        capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return CapabilityResult("skipped", "perf_probe_failed")
    if probe.returncode != 0 or "not supported" in probe.stderr.lower():
        return CapabilityResult("skipped", "perf_probe_failed", perf_version=version.stdout.strip())
    return CapabilityResult("ready", None, config.hardware.stat, version.stdout.strip(), target.cpu_scope)
