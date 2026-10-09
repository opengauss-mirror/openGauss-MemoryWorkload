"""Bind deployed physical services to ordinary native workflow runs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .deployment_config import load_deployment, resolve_groups
from .deployment_docker import DockerClient
from .deployment_lifecycle import status
from .deployment_store import DeploymentStore


def _child_url(group_ports: dict[str, dict[str, int]], service_name: str, template) -> str:
    port = group_ports[service_name][f"{template.container_port}/tcp"]
    return f"{template.endpoint_scheme}://127.0.0.1:{port}"


def _invoke_native(argv: list[str], *, env: dict[str, str], cwd: Path) -> int:
    result = subprocess.run([sys.executable, "-m", "memory_bench_platform.cli", *argv],
                            env=env, cwd=cwd, check=False)
    return result.returncode


def run_deployment(name: str, cwd: Path, combination_id: str | None, docker: DockerClient,
                   invoke: Callable[..., int] = _invoke_native) -> int:
    store = DeploymentStore(cwd)
    store.acquire_lock(name)
    try:
        checked = status(name, cwd, docker)
        if not checked.ready:
            raise ValueError("deployment is not ready: " + "; ".join(checked.drift))
        state = checked.state
        config = load_deployment(Path(state.config_path))
        groups = {combo.id: group for group in resolve_groups(config) for combo in group.combinations}
        if combination_id and combination_id not in groups:
            raise ValueError(f"unknown combination: {combination_id}")
        combos = [combo for combo in config.combinations if not combination_id or combo.id == combination_id]
        suite_id = f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        suite_dir = cwd / "runs" / suite_id
        suite_dir.mkdir(parents=True)
        summary = {"deployment": name, "suite_id": suite_id, "combinations": []}
        all_endpoint_names = {service.client.endpoint_env for service in [*config.agents.values(), *config.memories.values()]
                              if service.client}
        result_code = 0
        try:
            for combo in combos:
                group = groups[combo.id]
                agent = config.agents[combo.agent]
                memory = group.memory_template
                group_ports = state.ports[group.project_key]
                env = os.environ.copy()
                for endpoint in all_endpoint_names:
                    env.pop(endpoint, None)
                env[agent.client.endpoint_env] = _child_url(group_ports, "agent_" + combo.id.replace("-", "_"), agent)
                env[memory.client.endpoint_env] = _child_url(group_ports, "memory", memory)
                for template in (agent, memory):
                    for target, source in template.client.environment_from.items():
                        env[target] = os.environ[source]
                argv = ["run", "--benchmark", combo.benchmark, "--agent", agent.skill,
                        "--memory-backend", memory.skill, "--memory-integration", combo.memory_integration,
                        "--data-path", str((group.source_root / combo.data_path).resolve()),
                        "--run-id", f"{suite_id}-{combo.id}"]
                if combo.entrypoint:
                    argv += ["--entrypoint", combo.entrypoint]
                record = {"combination": combo.id, "memory_instance": group.memory_instance_id,
                          "run_id": f"{suite_id}-{combo.id}", "status": "pending"}
                summary["combinations"].append(record)
                try:
                    warmup = combo.preparation.warmup
                    if warmup.enabled:
                        target_name = warmup.target
                        if target_name == "agent":
                            target_name = "agent_" + combo.id.replace("-", "_")
                        elif target_name == "mock":
                            if group.shared_memory_instance_id:
                                target_name = ("mock_" + combo.id.replace("-", "_")) if combo.mock else "mock_memory"
                        target_id = state.containers[group.project_key].get(target_name)
                        if not target_id:
                            raise ValueError(f"unknown warmup target: {warmup.target}")
                        record["warmup"] = docker.exec_json(target_id, warmup.command)
                    rc = invoke(argv, env=env, cwd=cwd)
                    record["status"] = "passed" if rc == 0 else "failed"
                    record["return_code"] = rc
                    if rc:
                        result_code = 1
                except KeyboardInterrupt:
                    record["status"] = "interrupted"
                    result_code = 130
                    break
                except Exception as exc:
                    record["status"] = "failed"
                    record["error_type"] = type(exc).__name__
                    result_code = 1
        finally:
            (suite_dir / "suite_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return result_code
    finally:
        store.release_lock(name)
