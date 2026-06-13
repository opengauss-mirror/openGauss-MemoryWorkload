from __future__ import annotations

from pathlib import Path

import yaml

from .manifests import AgentManifest, BenchmarkManifest


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_all_skills(skills_root: Path) -> dict[str, list]:
    benchmarks = []
    agents = []
    for manifest_path in sorted((skills_root / "benchmarks").glob("*/manifest.yaml")):
        benchmarks.append(BenchmarkManifest.model_validate(_load_yaml(manifest_path)))
    for manifest_path in sorted((skills_root / "agents").glob("*/manifest.yaml")):
        agents.append(AgentManifest.model_validate(_load_yaml(manifest_path)))
    return {"benchmarks": benchmarks, "agents": agents}
