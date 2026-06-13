from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .loader import load_all_skills
from .manifests import AgentManifest, BenchmarkManifest
from .paths import SKILLS_ROOT
from .protocol import RenderedTaskInput


def _manifest_path(kind: str, skill_id: str) -> Path:
    return SKILLS_ROOT / kind / skill_id / "manifest.yaml"


def get_benchmark_manifest(skill_id: str) -> BenchmarkManifest:
    loaded = load_all_skills(SKILLS_ROOT)
    for manifest in loaded["benchmarks"]:
        if manifest.id == skill_id:
            return manifest
    raise FileNotFoundError(f"benchmark skill not found: {skill_id}")


def get_agent_manifest(skill_id: str) -> AgentManifest:
    loaded = load_all_skills(SKILLS_ROOT)
    for manifest in loaded["agents"]:
        if manifest.id == skill_id:
            return manifest
    raise FileNotFoundError(f"agent skill not found: {skill_id}")


def _script_for_manifest(manifest_path: Path, relative_script: str) -> Path:
    return manifest_path.parent / relative_script


def run_json_script(script_path: Path, *, args: list[str] | None = None, stdin_payload: dict | None = None) -> dict:
    cmd = [sys.executable, str(script_path), *(args or [])]
    proc = subprocess.run(
        cmd,
        input=None if stdin_payload is None else json.dumps(stdin_payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout or "{}")


def validate_benchmark(skill_id: str, source_path: str | None = None) -> dict:
    manifest = get_benchmark_manifest(skill_id)
    manifest_path = _manifest_path("benchmarks", skill_id)
    script = manifest.entry.validator or manifest.entry.task_builder
    if not script:
        raise ValueError(f"benchmark skill {skill_id} has no validator or task_builder")
    args = [source_path] if source_path else []
    return run_json_script(_script_for_manifest(manifest_path, script), args=args)


def validate_agent(skill_id: str) -> dict:
    manifest = get_agent_manifest(skill_id)
    manifest_path = _manifest_path("agents", skill_id)
    script = manifest.entry.healthcheck or manifest.entry.runner
    if not script:
        raise ValueError(f"agent skill {skill_id} has no healthcheck or runner")
    return run_json_script(_script_for_manifest(manifest_path, script))


def build_benchmark_tasks(skill_id: str, source_path: str | None = None) -> dict:
    manifest = get_benchmark_manifest(skill_id)
    manifest_path = _manifest_path("benchmarks", skill_id)
    if not manifest.entry.task_builder:
        raise ValueError(f"benchmark skill {skill_id} has no task_builder")
    args = [source_path] if source_path else []
    return run_json_script(_script_for_manifest(manifest_path, manifest.entry.task_builder), args=args)


def run_agent_task(skill_id: str, rendered_input: RenderedTaskInput) -> dict:
    manifest = get_agent_manifest(skill_id)
    manifest_path = _manifest_path("agents", skill_id)
    if not manifest.entry.runner:
        raise ValueError(f"agent skill {skill_id} has no runner")
    return run_json_script(
        _script_for_manifest(manifest_path, manifest.entry.runner),
        stdin_payload=rendered_input.model_dump(),
    )
