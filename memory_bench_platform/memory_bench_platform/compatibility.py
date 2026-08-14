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


def _declared_actions(payload: dict[str, Any]) -> set[str]:
    return {str(item) for item in payload.get("actions", []) if item}


def _nested_bool(payload: dict[str, Any], section: str, name: str) -> bool:
    nested = payload.get(section, {})
    return bool(nested.get(name)) if isinstance(nested, dict) else False


def _declared_output_fields(capabilities: dict[str, Any], action: str) -> set[str]:
    outputs = capabilities.get("outputs", {})
    declared = outputs.get(action, []) if isinstance(outputs, dict) else []
    return {str(item) for item in declared if item}


def _require_output_fields(
    missing: list[str], capabilities: dict[str, Any], action: str, fields: set[str], prefix: str
) -> None:
    declared = _declared_output_fields(capabilities, action)
    for field in sorted(fields - declared):
        missing.append(f"{prefix}.outputs.{action}.{field}")


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
    required_memory_actions = {str(action) for action in required_memory.get("actions", [])}

    targets = {
        event.evaluation.target
        for sample in scenario.samples
        for event in sample.timeline
        if event.evaluation is not None
    }
    if "qa_answer" in targets and not agent.entry.runner:
        missing.append("agent.runner")
    memory_capabilities = memory.capabilities if memory else {}
    plugin_capabilities = memory_plugin.capabilities if memory_plugin else {}
    plugin_lifecycle = memory_plugin.lifecycle if memory_plugin else {}
    plugin_actions = _declared_actions(plugin_lifecycle)
    plugin_phases = _declared_actions({"actions": plugin_lifecycle.get("phases", [])})

    if binding.memory_integration == "backend_direct":
        for action in required_memory_actions:
            if action not in available_actions:
                missing.append(f"memory.actions.{action}")
        if targets & {"qa_answer", "retrieval"} and "recall" not in available_actions:
            missing.append("memory.actions.recall")
        if memory is None or not memory.entry.runner:
            missing.append("memory.runner")
        if not _nested_bool(memory_capabilities, "scope", "supported"):
            missing.append("memory.scope.supported")
        if not _nested_bool(memory_capabilities, "scope", "scoped_ingest"):
            missing.append("memory.scope.scoped_ingest")
        if not _nested_bool(memory_capabilities, "scope", "scoped_recall"):
            missing.append("memory.scope.scoped_recall")
        commit_required = _nested_bool(memory_capabilities, "commit", "required_after_ingest")
        if commit_required and not _nested_bool(memory_capabilities, "commit", "supported"):
            missing.append("memory.commit.supported")
        async_ingest = bool(memory_capabilities.get("async_ingest"))
        if async_ingest:
            if not _nested_bool(memory_capabilities, "readiness", "supported"):
                missing.append("memory.readiness.supported")
            if not _nested_bool(memory_capabilities, "readiness", "scoped_by_operation"):
                missing.append("memory.readiness.scoped_by_operation")
        if targets & {"memory_extraction", "memory_update"} and "inspect_memory" not in available_actions:
            missing.append("memory.actions.inspect_memory")
        if "agent_action" in targets:
            missing.append("runtime.evaluation_targets.agent_action")
        protocol_version = str((memory.integration if memory else {}).get("protocol_version") or "")
        if protocol_version != "memory/1":
            missing.append("memory.integration.protocol_version.memory/1")
        if "ingest" in required_memory_actions:
            _require_output_fields(
                missing, memory_capabilities, "ingest", {"operation.session_id"}, "memory"
            )
        if commit_required:
            _require_output_fields(
                missing, memory_capabilities, "flush", {"operation.task_id"}, "memory"
            )
        if async_ingest:
            _require_output_fields(
                missing, memory_capabilities, "status", {"state"}, "memory"
            )
        if targets & {"qa_answer", "retrieval"}:
            _require_output_fields(
                missing, memory_capabilities, "recall", {"output.evidence_text"}, "memory"
            )
    else:
        if memory_plugin is None:
            missing.append("memory_plugin.binding")
        else:
            if memory_plugin.agent != binding.agent_id:
                missing.append("memory_plugin.agent_binding")
            if memory is None or memory_plugin.memory != memory.id:
                missing.append("memory_plugin.memory_binding")
            for action in ("validate", "prepare", "set_phase", "finalize"):
                if action not in plugin_actions:
                    missing.append(f"memory_plugin.lifecycle.actions.{action}")
            for phase in ("ingest", "qa"):
                if phase not in plugin_phases:
                    missing.append(f"memory_plugin.lifecycle.phases.{phase}")
            plugin_commit_required = _nested_bool(
                plugin_capabilities, "commit", "required_after_ingest"
            )
            plugin_readiness_supported = _nested_bool(
                plugin_capabilities, "readiness", "supported"
            )
            if plugin_commit_required:
                if not _nested_bool(plugin_capabilities, "commit", "supported"):
                    missing.append("memory_plugin.commit.supported")
                if "commit" not in plugin_actions:
                    missing.append("memory_plugin.lifecycle.actions.commit")
            if plugin_readiness_supported:
                if "wait_ready" not in plugin_actions:
                    missing.append("memory_plugin.lifecycle.actions.wait_ready")
                if not _nested_bool(plugin_capabilities, "readiness", "scoped_by_operation"):
                    missing.append("memory_plugin.readiness.scoped_by_operation")
            if not plugin_capabilities.get("namespace_isolation"):
                missing.append("memory_plugin.namespace_isolation")
            if "qa_answer" in targets and not plugin_capabilities.get("qa_read_only"):
                missing.append("memory_plugin.qa_read_only")
            if "ingest" in required_memory_actions and not plugin_capabilities.get("auto_capture"):
                missing.append("memory_plugin.auto_capture")
            if "recall" in required_memory_actions and not plugin_capabilities.get("auto_recall"):
                missing.append("memory_plugin.auto_recall")
            if str(memory_plugin.integration.get("protocol_version") or "") != "memory-plugin/1":
                missing.append("memory_plugin.integration.protocol_version.memory-plugin/1")
            if plugin_commit_required:
                _require_output_fields(
                    missing,
                    plugin_capabilities,
                    "commit",
                    {"operation.task_id"},
                    "memory_plugin",
                )
            if plugin_readiness_supported:
                _require_output_fields(
                    missing,
                    plugin_capabilities,
                    "wait_ready",
                    {"state"},
                    "memory_plugin",
                )
        unsupported_plugin_targets = targets - {"qa_answer"}
        for target in sorted(unsupported_plugin_targets):
            missing.append(f"memory_plugin.evaluation_targets.{target}")

    return CompatibilityResult(
        status="compatible" if not missing else "incompatible",
        missing_capabilities=sorted(set(missing)),
        suggestions=[] if not missing else ["选择满足缺失能力的 Runtime Adapter，或修正 Manifest 能力声明"],
        resolved_capabilities={
            "agent": agent_capabilities,
            "memory": memory_capabilities,
            "memory_actions": sorted(available_actions),
            "memory_plugin": plugin_capabilities,
            "memory_plugin_lifecycle": plugin_lifecycle,
            "evaluation_targets": sorted(targets),
        },
    )
