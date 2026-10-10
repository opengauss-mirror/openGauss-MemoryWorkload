"""Owned deployment → run → offline analysis orchestration."""
from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .deployment_config import load_deployment
from .deployment_lifecycle import deploy, status, undeploy
from .deployment_run import run_deployment
from .deployment_store import DeploymentStore
from .profiling_config import load_profiling_config
from .result_analysis import analyze_run


@dataclass(frozen=True)
class ExperimentResult:
    archive_dir: Path
    status: str
    owned_deployment: bool
    suite_dir: Path | None


def run_experiment(config_path: Path, profiling_path: Path, cwd: Path, docker, *,
                   pressure_test: str | None = None,
                   cleanup_owned_deployment: bool = False) -> ExperimentResult:
    if pressure_test is not None:
        raise ValueError("--pressure-test requires the separate multi-agent pressure implementation")
    config_path = config_path.resolve()
    profiling_path = profiling_path.resolve()
    config = load_deployment(config_path)
    profile = load_profiling_config(profiling_path)
    if profile.mode == "required" and profile.hardware.stat != "off" and (
            platform.system() != "Linux" or shutil.which("perf") is None):
        raise RuntimeError("required perf is unavailable on this host")

    archive = cwd / "runs" / "experiments" / f"{config.name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    archive.mkdir(parents=True, mode=0o700)
    archive.chmod(0o700)
    payload = {"schema": "profiling-experiment/1", "deployment": config.name,
               "status": "running", "owned_deployment": False,
               "suite_dir": None, "steps": []}

    def record(step: str, state: str, **details) -> None:
        payload["steps"].append({"step": step, "state": state, **details})
        if state == "failed":
            payload["status"] = "failed"
        temporary = archive / ".experiment.json.tmp"
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, archive / "experiment.json")

    owned = False
    suite_dir = None
    try:
        record("create", "completed")
        if DeploymentStore(cwd).directory(config.name).exists():
            record("deploy", "reused")
        else:
            deploy(config_path, cwd, docker)
            owned = True
            payload["owned_deployment"] = True
            record("deploy", "completed")
        checked = status(config.name, cwd, docker)
        if not checked.ready:
            raise RuntimeError("deployment is not ready: " + "; ".join(checked.drift))
        record("verify", "completed")
        before = set((cwd / "runs").glob(f"{config.name}-*/suite_summary.json"))
        rc = run_deployment(config.name, cwd, None, docker, profiling_config=profiling_path)
        summaries = set((cwd / "runs").glob(f"{config.name}-*/suite_summary.json")) - before
        if len(summaries) != 1:
            raise RuntimeError("suite summary is missing or ambiguous")
        summary_path = summaries.pop()
        suite_dir = summary_path.parent
        payload["suite_dir"] = str(suite_dir)
        record("run", "completed" if rc == 0 else "failed", return_code=rc)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        combinations = summary.get("combinations", [])
        if not combinations:
            raise RuntimeError("suite has no combinations")
        for combination in combinations:
            child = cwd / "runs" / combination["run_id"]
            if child.is_dir():
                analyze_run(child)
                record("analyze", "completed", run_id=combination["run_id"])
            elif combination.get("status") == "passed":
                raise RuntimeError("completed combination has no run archive")
        payload["status"] = ("completed" if rc == 0 and all(
            item.get("status") == "passed" for item in combinations) else "failed")
    except BaseException as exc:
        record("experiment", "failed", error_type=type(exc).__name__)
        raise
    finally:
        if owned and cleanup_owned_deployment:
            try:
                undeploy(config.name, cwd, docker)
                record("cleanup", "completed")
            except BaseException as exc:
                record("cleanup", "failed", error_type=type(exc).__name__)
                raise
        record("finish", payload["status"])
    return ExperimentResult(archive, payload["status"], owned, suite_dir)
