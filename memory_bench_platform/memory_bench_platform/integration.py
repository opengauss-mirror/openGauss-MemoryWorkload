from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .loader import load_all_skills
from .manifests import (
    AgentManifest,
    BenchmarkManifest,
    MemoryManifest,
    MemoryPluginManifest,
    SmokeManifest,
)
from .paths import PROJECT_ROOT, SKILLS_ROOT
from .protocol import (
    EntryPointRecord,
    MemoryPluginTaskInput,
    MemoryPluginTaskOutput,
    MemoryTaskInput,
    MemoryTaskOutput,
    RenderedTaskInput,
)


@dataclass(frozen=True)
class RunSkillBundle:
    skills_root: Path
    benchmark: BenchmarkManifest
    agent: AgentManifest
    memory: MemoryManifest | None = None
    memory_plugin: MemoryPluginManifest | None = None

    @property
    def memory_id(self) -> str | None:
        return self.memory.id if self.memory is not None else None

    @property
    def memory_plugin_id(self) -> str | None:
        return self.memory_plugin.id if self.memory_plugin is not None else None


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


def get_memory_manifest(skill_id: str) -> MemoryManifest:
    loaded = load_all_skills(SKILLS_ROOT)
    for manifest in loaded["memories"]:
        if manifest.id == skill_id:
            return manifest
    raise FileNotFoundError(f"memory skill not found: {skill_id}")


def get_memory_plugin_manifest(skill_id: str) -> MemoryPluginManifest:
    loaded = load_all_skills(SKILLS_ROOT)
    for manifest in loaded["memory_plugins"]:
        if manifest.id == skill_id:
            return manifest
    raise FileNotFoundError(f"memory plugin skill not found: {skill_id}")


def get_smoke_manifest(skill_id: str) -> SmokeManifest:
    loaded = load_all_skills(SKILLS_ROOT)
    for manifest in loaded["smokes"]:
        if manifest.id == skill_id:
            return manifest
    raise FileNotFoundError(f"smoke skill not found: {skill_id}")


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


def validate_smoke(skill_id: str) -> dict:
    manifest = get_smoke_manifest(skill_id)
    manifest_path = _manifest_path("smoke", skill_id)
    probe = run_json_script(_script_for_manifest(manifest_path, manifest.entry.probe_builder))
    payload = run_json_script(
        _script_for_manifest(manifest_path, manifest.entry.validator),
        stdin_payload=probe,
    )
    return {"probe": probe, "validation": payload}


def execute_smoke_skill(skill_id: str, run_dir: Path) -> dict[str, Any]:
    manifest = get_smoke_manifest(skill_id)
    manifest_path = _manifest_path("smoke", skill_id)
    probe = run_json_script(
        _script_for_manifest(manifest_path, manifest.entry.probe_builder),
        args=[str(run_dir)],
    )
    validation = run_json_script(
        _script_for_manifest(manifest_path, manifest.entry.validator),
        args=[str(run_dir)],
        stdin_payload=probe,
    )
    report = run_json_script(
        _script_for_manifest(manifest_path, manifest.entry.reporter),
        stdin_payload={
            "probe": probe,
            "validation": validation,
            "manifest": manifest.model_dump(mode="json"),
            "run_dir": str(run_dir),
        },
    )
    return {
        "manifest": manifest.model_dump(mode="json"),
        "probe": probe,
        "validation": validation,
        "report": report,
    }


def validate_benchmark(skill_id: str, source_path: str | None = None) -> dict:
    manifest = get_benchmark_manifest(skill_id)
    manifest_path = _manifest_path("benchmarks", skill_id)
    script = manifest.entry.validator or manifest.entry.case_builder or manifest.entry.task_builder
    if not script:
        raise ValueError(f"benchmark skill {skill_id} has no validator or case_builder/task_builder")
    args = [source_path] if source_path else []
    return run_json_script(_script_for_manifest(manifest_path, script), args=args)


def validate_agent(skill_id: str) -> dict:
    manifest = get_agent_manifest(skill_id)
    manifest_path = _manifest_path("agents", skill_id)
    script = manifest.entry.healthcheck or manifest.entry.runner
    if not script:
        raise ValueError(f"agent skill {skill_id} has no healthcheck or runner")
    return run_json_script(_script_for_manifest(manifest_path, script))


def resolve_run_skill_bundle(
    benchmark_id: str,
    agent_id: str,
    memory_id: str | None = None,
    memory_integration: str = "backend_direct",
    *,
    skills_root: Path = SKILLS_ROOT,
) -> RunSkillBundle:
    loaded = load_all_skills(skills_root)
    benchmark = _find_manifest_by_id(loaded["benchmarks"], benchmark_id, kind="benchmark")
    agent = _find_manifest_by_id(loaded["agents"], agent_id, kind="agent")
    resolved_memory_id = memory_id or str(agent.runtime.get("default_memory_skill", "") or "").strip() or None
    memory = None
    if resolved_memory_id:
        memory = _find_manifest_by_id(loaded["memories"], resolved_memory_id, kind="memory")
    memory_plugin = None
    if memory_integration == "agent_plugin":
        if memory is None:
            raise ValueError("agent_plugin integration requires a memory backend")
        matches = [
            item
            for item in loaded["memory_plugins"]
            if item.agent == agent.id and item.memory == memory.id
        ]
        if not matches:
            raise ValueError(
                f"no memory plugin integration for agent={agent.id!r}, memory={memory.id!r}"
            )
        memory_plugin = matches[0]
    elif memory_integration != "backend_direct":
        raise ValueError(f"unsupported memory integration: {memory_integration}")
    bundle = RunSkillBundle(
        skills_root=skills_root,
        benchmark=benchmark,
        agent=agent,
        memory=memory,
        memory_plugin=memory_plugin,
    )
    _validate_run_skill_bundle(bundle)
    return bundle


def build_run_contract(
    benchmark_id: str,
    agent_id: str,
    memory_id: str | None = None,
    memory_integration: str = "backend_direct",
    *,
    skills_root: Path = SKILLS_ROOT,
) -> dict[str, Any]:
    bundle = resolve_run_skill_bundle(
        benchmark_id,
        agent_id,
        memory_id,
        memory_integration,
        skills_root=skills_root,
    )
    benchmark = bundle.benchmark
    agent = bundle.agent
    memory = bundle.memory
    memory_plugin = bundle.memory_plugin
    benchmark_execution = benchmark.execution or {}
    memory_runtime = memory.runtime if memory is not None else {}
    memory_ingest = memory.ingest if memory is not None else {}
    memory_recall = memory.recall if memory is not None else {}
    memory_completion = memory.completion if memory is not None else {}

    return {
        "selection": {
            "benchmark_id": benchmark.id,
            "agent_id": agent.id,
            "memory_id": bundle.memory_id,
            "memory_integration": memory_integration,
            "memory_plugin_id": bundle.memory_plugin_id,
        },
        "execution": {
            "benchmark_mode": benchmark_execution.get("mode"),
            "task_isolation": benchmark_execution.get("task_isolation"),
            "requires_stateful_agent": bool(benchmark_execution.get("requires_stateful_agent")),
            "benchmark_ingest_unit": benchmark_execution.get("ingest_unit"),
            "entrypoints": benchmark_execution.get("entrypoints", {}),
        },
        "agent_runtime": {
            "mode": agent.runtime.get("mode"),
            "protocol_mode": agent.io.get("protocol_mode"),
            "startup_required": bool(agent.lifecycle.get("startup_required")),
            "default_memory_skill": agent.runtime.get("default_memory_skill"),
        },
        "memory_runtime": {
            "enabled": memory is not None,
            "runner": memory.entry.runner if memory is not None else None,
            "supported_actions": memory_runtime.get("actions", []),
            "benchmark_unit": memory_runtime.get("benchmark_unit"),
            "ingest_benchmark_unit": memory_ingest.get("benchmark_unit"),
            "recall_mode": memory_recall.get("mode"),
            "accept_signal": memory_completion.get("accept_signal"),
            "complete_signal": memory_completion.get("complete_signal"),
        },
        "memory_plugin_runtime": {
            "enabled": memory_plugin is not None,
            "runner": memory_plugin.entry.runner if memory_plugin is not None else None,
            "actions": memory_plugin.runtime.get("actions", []) if memory_plugin is not None else [],
            "capabilities": memory_plugin.capabilities if memory_plugin is not None else {},
            "phases": memory_plugin.phases if memory_plugin is not None else {},
        },
        "version_targets": {
            "benchmark": [target.model_dump(mode="json") for target in benchmark.version_policy.targets],
            "agent": [target.model_dump(mode="json") for target in agent.version_policy.targets],
            "memory": [target.model_dump(mode="json") for target in memory.version_policy.targets] if memory else [],
        },
    }


def build_benchmark_tasks(
    skill_id: str,
    source_path: str | None = None,
    memory_integration: str = "backend_direct",
    session_namespace: str | None = None,
) -> dict:
    manifest = get_benchmark_manifest(skill_id)
    manifest_path = _manifest_path("benchmarks", skill_id)
    builder = manifest.entry.case_builder or manifest.entry.task_builder
    if not builder:
        raise ValueError(f"benchmark skill {skill_id} has no case_builder or task_builder")
    args = [source_path] if source_path else []
    if memory_integration != "backend_direct":
        args.extend(["--memory-integration", memory_integration])
        if session_namespace:
            args.extend(["--session-namespace", session_namespace])
    return run_json_script(_script_for_manifest(manifest_path, builder), args=args)


def build_cases_from_source(
    skill_id: str,
    source_path: str | None = None,
    memory_integration: str = "backend_direct",
    session_namespace: str | None = None,
) -> dict:
    return build_benchmark_tasks(
        skill_id,
        source_path,
        memory_integration,
        session_namespace,
    )


def run_agent_task(skill_id: str, rendered_input: RenderedTaskInput) -> dict:
    manifest = get_agent_manifest(skill_id)
    manifest_path = _manifest_path("agents", skill_id)
    if not manifest.entry.runner:
        raise ValueError(f"agent skill {skill_id} has no runner")
    return run_json_script(
        _script_for_manifest(manifest_path, manifest.entry.runner),
        stdin_payload=rendered_input.model_dump(),
    )


def run_memory_task(skill_id: str, request: MemoryTaskInput) -> MemoryTaskOutput:
    manifest = get_memory_manifest(skill_id)
    manifest_path = _manifest_path("memories", skill_id)
    if not manifest.entry.runner:
        raise ValueError(f"memory skill {skill_id} has no runner")
    try:
        payload = run_json_script(
            _script_for_manifest(manifest_path, manifest.entry.runner),
            stdin_payload=request.model_dump(mode="json"),
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"memory skill {skill_id} runner returned invalid JSON") from exc
    try:
        return MemoryTaskOutput.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"memory skill {skill_id} runner returned invalid response") from exc


def run_memory_plugin_task(
    skill_id: str,
    request: MemoryPluginTaskInput,
) -> MemoryPluginTaskOutput:
    manifest = get_memory_plugin_manifest(skill_id)
    manifest_path = _manifest_path("memory_plugins", skill_id)
    if not manifest.entry.runner:
        raise ValueError(f"memory plugin skill {skill_id} has no runner")
    try:
        payload = run_json_script(
            _script_for_manifest(manifest_path, manifest.entry.runner),
            stdin_payload=request.model_dump(mode="json"),
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"memory plugin skill {skill_id} runner returned invalid JSON") from exc
    try:
        return MemoryPluginTaskOutput.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"memory plugin skill {skill_id} runner returned invalid response") from exc


def score_benchmark_run(skill_id: str, run_dir: Path, source_path: str | None = None) -> dict:
    manifest = get_benchmark_manifest(skill_id)
    manifest_path = _manifest_path("benchmarks", skill_id)
    if not manifest.entry.scorer:
        raise ValueError(f"benchmark skill {skill_id} has no scorer")
    args = [str(run_dir)]
    if source_path:
        args.append(source_path)
    return run_json_script(_script_for_manifest(manifest_path, manifest.entry.scorer), args=args)


def classify_entrypoint(entry: dict[str, Any]) -> str:
    if entry.get("external_runner"):
        return "external_runner"
    if entry.get("case_builder") or entry.get("task_builder"):
        return "case_builder"
    return "unknown"


def resolve_benchmark_entrypoint(skill_id: str, entrypoint_id: str | None = None) -> EntryPointRecord:
    manifest = get_benchmark_manifest(skill_id)
    manifest_path = _manifest_path("benchmarks", skill_id)

    if entrypoint_id:
        configured = manifest.execution.get("entrypoints", {}).get(entrypoint_id, {})
        kind = classify_entrypoint(configured)
        if kind == "external_runner":
            script_path = manifest_path.parent / configured["external_runner"]
            command = _command_for_script(script_path)
            return EntryPointRecord(
                entrypoint_id=entrypoint_id,
                entrypoint_kind="external_runner",
                command=command,
                metadata=configured,
            )
        raise ValueError(f"unsupported benchmark entrypoint: {skill_id}:{entrypoint_id}")

    builder = manifest.entry.case_builder or manifest.entry.task_builder
    if not builder:
        raise ValueError(f"benchmark skill {skill_id} has no case_builder/task_builder")
    return EntryPointRecord(
        entrypoint_id="default",
        entrypoint_kind="case_builder",
        command=[sys.executable, str(_script_for_manifest(manifest_path, builder))],
    )


def _command_for_script(script_path: Path) -> list[str]:
    if script_path.suffix == ".py":
        return [sys.executable, str(script_path)]
    if script_path.suffix == ".sh":
        return ["bash", str(script_path)]
    return [str(script_path)]


def execute_external_runner(
    entrypoint: EntryPointRecord,
    *,
    env: dict[str, str],
    cwd: Path | None = None,
) -> dict[str, Any]:
    proc = subprocess.run(
        entrypoint.command,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(cwd or PROJECT_ROOT),
        check=False,
    )
    return {
        "status": "passed" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _find_manifest_by_id(manifests: list[Any], skill_id: str, *, kind: str) -> Any:
    for manifest in manifests:
        if manifest.id == skill_id:
            return manifest
    raise FileNotFoundError(f"{kind} skill not found: {skill_id}")


def _validate_run_skill_bundle(bundle: RunSkillBundle) -> None:
    benchmark_execution = bundle.benchmark.execution or {}
    requires_stateful_agent = bool(benchmark_execution.get("requires_stateful_agent"))
    protocol_mode = str(bundle.agent.io.get("protocol_mode", "") or "")
    if requires_stateful_agent and protocol_mode != "stateful_session":
        raise ValueError(
            f"benchmark {bundle.benchmark.id} requires stateful agent, "
            f"but agent {bundle.agent.id} protocol_mode={protocol_mode!r}"
        )

    if bundle.memory is None:
        return

    benchmark_ingest_unit = str(benchmark_execution.get("ingest_unit", "") or "").strip()
    memory_runtime_unit = str(bundle.memory.runtime.get("benchmark_unit", "") or "").strip()
    memory_ingest_unit = str(bundle.memory.ingest.get("benchmark_unit", "") or "").strip()

    if benchmark_ingest_unit and memory_runtime_unit and benchmark_ingest_unit != memory_runtime_unit:
        raise ValueError(
            f"benchmark {bundle.benchmark.id} ingest_unit={benchmark_ingest_unit!r} "
            f"but memory {bundle.memory.id} runtime.benchmark_unit={memory_runtime_unit!r}"
        )
    if benchmark_ingest_unit and memory_ingest_unit and benchmark_ingest_unit != memory_ingest_unit:
        raise ValueError(
            f"benchmark {bundle.benchmark.id} ingest_unit={benchmark_ingest_unit!r} "
            f"but memory {bundle.memory.id} ingest.benchmark_unit={memory_ingest_unit!r}"
        )
