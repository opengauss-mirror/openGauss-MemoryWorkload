from __future__ import annotations

from pathlib import Path

import yaml

from .manifests import (
    AgentManifest,
    BenchmarkManifest,
    MemoryManifest,
    MemoryPluginManifest,
    SmokeManifest,
)


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_benchmark_skill(skills_root: Path, benchmark_id: str) -> BenchmarkManifest:
    manifest_path = skills_root / "benchmarks" / benchmark_id / "manifest.yaml"
    return BenchmarkManifest.model_validate(_load_yaml(manifest_path))


def load_agent_skill(skills_root: Path, agent_id: str) -> AgentManifest:
    manifest_path = skills_root / "agents" / agent_id / "manifest.yaml"
    return AgentManifest.model_validate(_load_yaml(manifest_path))


def load_smoke_skill(skills_root: Path, smoke_id: str) -> SmokeManifest:
    manifest_path = skills_root / "smoke" / smoke_id / "manifest.yaml"
    return SmokeManifest.model_validate(_load_yaml(manifest_path))


def load_memory_skill(skills_root: Path, memory_id: str) -> MemoryManifest:
    manifest_path = skills_root / "memories" / memory_id / "manifest.yaml"
    return MemoryManifest.model_validate(_load_yaml(manifest_path))


def load_memory_plugin_skill(skills_root: Path, plugin_id: str) -> MemoryPluginManifest:
    manifest_path = skills_root / "memory_plugins" / plugin_id / "manifest.yaml"
    return MemoryPluginManifest.model_validate(_load_yaml(manifest_path))


def load_all_skills(skills_root: Path) -> dict[str, list]:
    benchmarks = []
    agents = []
    memories = []
    memory_plugins = []
    smokes = []
    for manifest_path in sorted((skills_root / "benchmarks").glob("*/manifest.yaml")):
        benchmarks.append(BenchmarkManifest.model_validate(_load_yaml(manifest_path)))
    for manifest_path in sorted((skills_root / "agents").glob("*/manifest.yaml")):
        agents.append(AgentManifest.model_validate(_load_yaml(manifest_path)))
    memory_root = skills_root / "memories"
    if memory_root.exists():
        for manifest_path in sorted(memory_root.glob("*/manifest.yaml")):
            memories.append(MemoryManifest.model_validate(_load_yaml(manifest_path)))
    memory_plugin_root = skills_root / "memory_plugins"
    if memory_plugin_root.exists():
        for manifest_path in sorted(memory_plugin_root.glob("*/manifest.yaml")):
            memory_plugins.append(MemoryPluginManifest.model_validate(_load_yaml(manifest_path)))
    smoke_root = skills_root / "smoke"
    if smoke_root.exists():
        for manifest_path in sorted(smoke_root.glob("*/manifest.yaml")):
            smokes.append(SmokeManifest.model_validate(_load_yaml(manifest_path)))
    return {
        "benchmarks": benchmarks,
        "agents": agents,
        "memories": memories,
        "memory_plugins": memory_plugins,
        "smokes": smokes,
    }
