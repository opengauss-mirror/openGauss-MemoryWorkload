"""Bind deployed physical services to ordinary native workflow runs."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .deployment_config import load_deployment, resolve_groups
from .deployment_docker import DockerClient
from . import deployment_lifecycle
from .deployment_lifecycle import status
from .deployment_store import DeploymentStore


def _child_url(group_ports: dict[str, dict[str, int]], service_name: str, template) -> str:
    port = group_ports[service_name][f"{template.container_port}/tcp"]
    return f"{template.endpoint_scheme}://127.0.0.1:{port}"


def _invoke_native(argv: list[str], *, env: dict[str, str], cwd: Path) -> int:
    result = subprocess.run([sys.executable, "-m", "memory_bench_platform.cli", *argv],
                            env=env, cwd=cwd, check=False)
    if result.returncode:
        return result.returncode
    # The native CLI archives failed/invalid evaluations without always exiting
    # nonzero. An incorrect answer (partial) is still a valid benchmark run;
    # missing evidence, failed setup or an unavailable judge is not.
    try:
        run_id = argv[argv.index("--run-id") + 1]
        summary = json.loads((cwd / "runs" / run_id / "reports" / "summary.json").read_text())
        valid = (summary.get("status") in {"passed", "partial"}
                 and summary.get("run_validity", {}).get("valid", True) is True)
    except (OSError, ValueError, IndexError, AttributeError):
        valid = False
    if not valid:
        print("Native run has a missing, failed or invalid summary; see its run archive.", file=sys.stderr)
        return 1
    return 0


def run_deployment(name: str, cwd: Path, combination_id: str | None, docker: DockerClient,
                   invoke: Callable[..., int] = _invoke_native,
                   profiling_config: Path | None = None) -> int:
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
                for key in ("OPENCLAW_POOL_ENABLED", "OPENCLAW_POOL_ROUTE_PREFIX"):
                    env.pop(key, None)
                for template in (agent, memory):
                    for target, source in template.client.environment_from.items():
                        env[target] = os.environ[source]
                memory_service = "agent_" + combo.id.replace("-", "_") if memory.placement == "agent" else "memory"
                if memory.container_port:
                    env[memory.client.endpoint_env] = _child_url(group_ports, memory_service, memory)
                if combo.gateway_pool:
                    pool = group.gateway_pools[combo.id]
                    pool_name = "gateway_pool_" + combo.id.replace("-", "_")
                    env[agent.client.endpoint_env] = _child_url(group_ports, pool_name, pool)
                    env["OPENCLAW_TRANSPORT"] = "http"
                    env["OPENCLAW_POOL_ENABLED"] = "1"
                    env["OPENCLAW_POOL_ROUTE_PREFIX"] = f"{suite_id}:{combo.id}"
                else:
                    env[agent.client.endpoint_env] = _child_url(group_ports, "agent_" + combo.id.replace("-", "_"), agent)
                env.pop("MEMORY_BENCH_DEPLOYED_SAMPLE_GATEWAYS", None)
                binding = state.plugin_runtimes.get(combo.id)
                bindings = [binding] if binding else []
                for name in combo.agent_services()[1:] if binding else ():
                    bindings.append(state.plugin_runtimes[name])
                if binding:
                    from .deployment_plugins import plugin_environment
                    gateways = [plugin_environment(item, state, _child_url(group_ports, item["service"], agent),
                                                   _child_url(group_ports, item["service"], memory)
                                                   if memory.placement == "agent" and memory.container_port
                                                   else env.get(memory.client.endpoint_env, "")) for item in bindings]
                    if len(gateways) > 1:
                        if memory.skill == "openviking":
                            for gateway in gateways:
                                gateway["OPENVIKING_MANAGED_SAMPLE_USER"] = "1"
                        env.update({k: v for k, v in gateways[0].items() if k in ("OGMEM_API_URL", "OGMEM_API_KEY")})
                        if memory.skill == "ogmemory" and env.get("MEMORY_BENCH_OGMEMORY_EXCLUSIVE") == "1":
                            env["MEMORY_BENCH_OGMEMORY_CONTAINER"] = state.containers[group.project_key]["memory"]
                        env["MEMORY_BENCH_DEPLOYED_SAMPLE_GATEWAYS"] = json.dumps(gateways)
                    else:
                        env.update(gateways[0])
                argv = ["run", "--benchmark", combo.benchmark, "--agent", agent.skill,
                        "--memory-backend", memory.skill, "--memory-integration", combo.memory_integration,
                        "--data-path", str((group.source_root / combo.data_path).resolve()),
                        "--run-id", f"{suite_id}-{combo.id}"]
                sample_limit = (combo.execution.sample_concurrency if combo.memory_integration == "backend_direct"
                                else len(bindings))
                if sample_limit > 1:
                    if profiling_config:
                        raise ValueError("Phase profiling requires sample_concurrency=1")
                    argv += ["--sample-concurrency", str(sample_limit)]
                if combo.entrypoint:
                    argv += ["--entrypoint", combo.entrypoint]
                if combo.execution.qa_concurrency is not None:
                    qa_limit = combo.execution.qa_concurrency
                    if combo.memory_integration == "agent_plugin" and combo.execution.sample_concurrency > 1:
                        qa_limit = min(qa_limit, len(bindings))
                    argv += ["--qa-concurrency", str(qa_limit)]
                if combo.execution.judge_concurrency is not None:
                    argv += ["--judge-concurrency", str(combo.execution.judge_concurrency)]
                if profiling_config is not None:
                    from .profiling_config import load_profiling_config
                    from .profiling_targets import resolve_targets
                    profile = load_profiling_config(profiling_config)
                    scoped = state.model_copy(update={"containers": {group.project_key: state.containers[group.project_key]}})
                    targets = resolve_targets(profile, scoped, [], docker=docker)
                    env["MEMORY_BENCH_PROFILE_TARGETS"] = json.dumps([asdict(target) for target in targets])
                    argv += ["--profiling-config", str(profiling_config)]
                record = {"combination": combo.id, "memory_instance": group.memory_instance_id,
                          "run_id": f"{suite_id}-{combo.id}", "status": "pending"}
                summary["combinations"].append(record)
                try:
                    warmup = combo.preparation.warmup
                    if warmup.enabled:
                        results = {}
                        for name in deployment_lifecycle._target_services(group, warmup.target, combo):
                            target_id = state.containers[group.project_key].get(name)
                            if not target_id:
                                raise ValueError(f"unknown warmup target: {name}")
                            results[name] = docker.exec_json(target_id, warmup.command)
                        record["warmup"] = next(iter(results.values())) if len(results) == 1 else results
                    try:
                        rc = invoke(argv, env=env, cwd=cwd)
                    finally:
                        for binding in bindings:
                            from .deployment_plugins import runtime_issues
                            issues = runtime_issues(binding)
                            cid = state.containers[binding["group"]][binding["service"]]
                            inspect = docker.inspect(cid)
                            deadline = time.monotonic() + max(60, (agent.healthcheck_interval_seconds or 0) + 30)
                            while (inspect.get("State", {}).get("Running") and
                                   inspect.get("State", {}).get("Health", {}).get("Status") == "starting" and
                                   time.monotonic() < deadline):
                                time.sleep(1)
                                inspect = docker.inspect(cid)
                            if inspect.get("State", {}).get("Health", {}).get("Status") != "healthy":
                                issues.append("plugin Agent is not healthy after phase restoration")
                            if inspect.get("Id") != cid or not inspect.get("State", {}).get("Running"):
                                issues.append("plugin Agent container stopped or replaced")
                            if issues:
                                raise ValueError("plugin runtime not restored: " + "; ".join(issues))
                            # Native lifecycle phase switches may restart this owned Agent.
                            # Accept only a surviving container with a clean, unchanged runtime.
                            state.restart_counts[cid] = int(inspect.get("RestartCount", 0))
                            state.start_times[cid] = inspect.get("State", {}).get("StartedAt", "")
                            store.write_atomic(state)
                    if combo.gateway_pool:
                        names = [f"agent_{combo.id.replace('-', '_')}_{n}"
                                 for n in range(1, combo.agent_replicas + 1)]
                        names.append(pool_name)
                        for name in names:
                            cid = state.containers[group.project_key][name]
                            inspect = docker.inspect(cid)
                            if (inspect.get("Id") != cid or
                                int(inspect.get("RestartCount", 0)) != state.restart_counts[cid] or
                                inspect.get("State", {}).get("StartedAt", "") != state.start_times[cid]):
                                raise ValueError(f"pooled service changed during run: {name}")
                        snapshot = deployment_lifecycle._pool_status(env[agent.client.endpoint_env])
                        expected_upstreams = {f"http://{name}:{agent.container_port}" for name in names[:-1]}
                        upstreams = snapshot.get("upstreams")
                        if not isinstance(upstreams, list) or len(upstreams) != len(expected_upstreams) or set(upstreams) != expected_upstreams:
                            raise ValueError("gateway pool upstreams changed during run")
                        salt = os.urandom(16)
                        record["gateway_pool"] = {
                            "assigned_users": {hashlib.sha256(salt + user.encode()).hexdigest()[:16]: upstream
                                               for user, upstream in snapshot.get("assignments", {}).items()},
                            "request_counts": snapshot.get("request_counts", {}),
                        }
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
