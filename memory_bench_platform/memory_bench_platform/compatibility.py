from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .benchmark_scenario import BenchmarkScenario, RunBinding
from .manifests import AgentManifest, MemoryManifest, MemoryPluginManifest


class CompatibilityResult(BaseModel):
    status: str
    missing_capabilities: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    resolved_capabilities: dict[str, Any] = Field(default_factory=dict)

    @property
    def compatible(self) -> bool:
        return self.status == "compatible"


def _actions(manifest: MemoryManifest | None) -> set[str]:
    if manifest is None:
        return set()
    declared = manifest.capabilities.get("actions", manifest.runtime.get("actions", []))
    return {str(item) for item in declared if item}


def resolve_compatibility(
    scenario: BenchmarkScenario,
    binding: RunBinding,
    *,
    agent: AgentManifest,
    memory: MemoryManifest | None,
    memory_plugin: MemoryPluginManifest | None,
) -> CompatibilityResult:
    missing: list[str] = []
    required_agent = scenario.requirements.get("agent", {})
    agent_capabilities = {
        **agent.capabilities,
        "stateful_session": agent.capabilities.get(
            "stateful_session", agent.io.get("protocol_mode") == "stateful_session"
        ),
    }
    for name, required in required_agent.items():
        if required is True and not agent_capabilities.get(name):
            missing.append(f"agent.{name}")

    required_memory = scenario.requirements.get("memory", {})
    available_actions = _actions(memory)
    for action in required_memory.get("actions", []):
        if str(action) not in available_actions:
            missing.append(f"memory.actions.{action}")

    if binding.memory_integration == "agent_plugin" and memory_plugin is None:
        missing.append("memory_plugin.binding")

    targets = {
        event.evaluation.target
        for sample in scenario.samples
        for event in sample.timeline
        if event.evaluation is not None
    }
    if "qa_answer" in targets and not agent.entry.runner:
        missing.append("agent.runner")
    if "retrieval" in targets and "recall" not in available_actions:
        missing.append("memory.actions.recall")

    return CompatibilityResult(
        status="compatible" if not missing else "incompatible",
        missing_capabilities=sorted(set(missing)),
        suggestions=[] if not missing else ["选择满足缺失能力的 Runtime Adapter，或修正 Manifest 能力声明"],
        resolved_capabilities={
            "agent": agent_capabilities,
            "memory_actions": sorted(available_actions),
            "memory_plugin": memory_plugin.capabilities if memory_plugin else {},
            "evaluation_targets": sorted(targets),
        },
    )
