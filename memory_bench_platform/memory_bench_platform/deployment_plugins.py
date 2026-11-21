"""Deployment bridge; provider-specific preparation stays in memory-plugin skills."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import socket

from .paths import SKILLS_ROOT
from .loader import load_all_skills


def deployment_adapter(agent: str, memory: str):
    matches = [m for m in load_all_skills(SKILLS_ROOT)["memory_plugins"]
               if m.agent == agent and m.memory == memory]
    if len(matches) != 1 or not matches[0].entry.deployer:
        raise ValueError(f"No deployment adapter declared for {agent} + {memory}; configure its memory-plugin entry.deployer")
    manifest = matches[0]
    root = (SKILLS_ROOT / "memory_plugins" / manifest.id).resolve()
    script = (root / manifest.entry.deployer).resolve()
    if not script.is_relative_to(root) or not script.is_file():
        raise ValueError(f"Invalid deployment adapter for {manifest.id}")
    adapter = runpy.run_path(str(script))
    for name in ("prepare", "environment", "issues"):
        if not callable(adapter.get(name)):
            raise ValueError(f"Deployment adapter {manifest.id} must implement {name}")
    return script, adapter


def _adapter(binding):
    script, adapter = deployment_adapter(binding["agent"], binding["memory"])
    if hashlib.sha256(script.read_bytes()).hexdigest() != binding["adapter_sha256"]:
        raise ValueError("Plugin deployment adapter changed; redeploy")
    return adapter


def prepare_plugin_runtimes(group, group_dir: Path, rendered: dict) -> dict:
    bindings = {}
    chosen_ports = set()
    for combo in group.combinations:
        if not combo.plugin_runtime:
            continue
        spec = combo.plugin_runtime
        agent, memory = group.agents[combo.id], group.memory_template
        script, adapter = deployment_adapter(agent.skill, memory.skill)
        directory = (group_dir / "plugin-runtimes" / combo.id).resolve()
        name = "agent_" + combo.id.replace("-", "_")
        context = {"directory": str(directory), "gateway_port": agent.container_port,
                   "memory_port": memory.container_port,
                   "memory_url": f"{memory.endpoint_scheme}://{'127.0.0.1' if memory.placement == 'agent' else 'memory'}:{memory.container_port}" if memory.container_port else ""}
        sources = {"memory_config": str((group.source_root / spec.memory_config).resolve()) if spec.memory_config else None,
                   "source_config": str((group.source_root / spec.source_config).resolve()),
                   "upstream_plugin": str((group.source_root / spec.upstream_plugin).resolve()) if spec.upstream_plugin else None,
                   "options": spec.options}
        if spec.expand_env:
            from .deployment_env import expand_environment
            import yaml
            inputs = group_dir / "plugin-inputs" / combo.id
            inputs.mkdir(parents=True, mode=0o700)
            for key in ("source_config", "memory_config"):
                if not sources[key]:
                    continue
                source = Path(sources[key])
                value = (json.loads(source.read_text()) if source.suffix == ".json"
                         else yaml.safe_load(source.read_text()))
                value = expand_environment(value)
                target = inputs / (key + source.suffix)
                with os.fdopen(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
                    output.write(json.dumps(value) if source.suffix == ".json" else yaml.safe_dump(value))
                sources[key] = str(target)
        result = adapter["prepare"](sources, context)
        patch = result["service"]
        if set(patch) - {"volumes", "labels", "environment", "user"}:
            raise ValueError("Plugin deployment can only add runtime mounts, labels, environment and user")
        service = rendered["services"][name]
        # Docker can reallocate a published port of 0 on restart. Native phase
        # switching must keep the URL supplied to the workflow stable.
        for port in service.get("ports", []):
            if not port.get("published"):
                while True:
                    with socket.socket() as reservation:
                        reservation.bind(("127.0.0.1", 0))
                        selected = reservation.getsockname()[1]
                    if selected not in chosen_ports:
                        chosen_ports.add(selected)
                        port["published"] = selected
                        break
        service.setdefault("volumes", []).extend(patch.get("volumes", []))
        for key in ("labels", "environment"):
            service.setdefault(key, {}).update(patch.get(key, {}))
        if "user" in patch:
            service["user"] = patch["user"]
        bindings[combo.id] = {"agent": agent.skill, "memory": memory.skill,
                              "directory": str(directory), "service": name, "group": group.project_key,
                              "adapter_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                              "metadata": result.get("metadata", {})}
    return bindings


def plugin_environment(binding: dict, state, gateway_url: str, memory_url: str) -> dict:
    endpoints = {"gateway_url": gateway_url, "memory_url": memory_url,
                 "container": state.containers[binding["group"]][binding["service"]]}
    result = _adapter(binding)["environment"](binding, endpoints)
    if not isinstance(result, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in result.items()):
        raise ValueError("Plugin deployment environment must contain string keys and values")
    return result


def runtime_issues(binding: dict) -> list[str]:
    issues = _adapter(binding)["issues"](binding)
    if not isinstance(issues, list) or any(not isinstance(issue, str) for issue in issues):
        raise ValueError("Plugin deployment issues must be a list of strings")
    return issues
