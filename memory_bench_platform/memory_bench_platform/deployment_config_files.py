"""Create immutable-per-deployment configuration copies for physical services."""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from .deployment_config import DeploymentGroup, Service, _pointer_value, _type_matches


def _set_pointer(data: object, pointer: str, value: object) -> None:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]
    owner = data
    for part in parts[:-1]:
        owner = owner[int(part)] if isinstance(owner, list) else owner[part]
    leaf = parts[-1]
    if isinstance(owner, list):
        owner[int(leaf)] = value
    else:
        owner[leaf] = value


def _copy_service(service: Service, root: Path, destination: Path, overrides: dict[str, object]) -> list[dict[str, object]]:
    mounts = []
    for key, config_file in service.config_files.items():
        source = (root / config_file.source).resolve()
        content = source.read_text(encoding="utf-8")
        data = json.loads(content) if config_file.format == "json" else yaml.safe_load(content)
        for name, parameter in config_file.parameters.items():
            if name in overrides:
                old = _pointer_value(data, parameter.pointer)
                if not _type_matches(old, parameter.type):
                    raise ValueError(f"config pointer type mismatch: {name}")
                _set_pointer(data, parameter.pointer, overrides[name])
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.chmod(0o700)
        output = destination / f"{key}.{config_file.format}"
        serialized = (json.dumps(data, ensure_ascii=False, indent=2) + "\n") if config_file.format == "json" else yaml.safe_dump(data, allow_unicode=True)
        descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        mounts.append({"type": "bind", "source": str(output), "target": config_file.target, "read_only": True})
    return mounts


def prepare_instance_configs(group: DeploymentGroup, root: Path) -> dict[str, list[dict[str, object]]]:
    result = {}
    for combo in group.combinations:
        template = group.agents[combo.id]
        base = "agent_" + combo.id.replace("-", "_")
        names = [f"{base}_{index}" for index in range(1, combo.agent_replicas + 1)] if combo.gateway_pool else [base]
        for service_name in names:
            result[service_name] = _copy_service(template, group.source_root, root / service_name,
                                                  group.agent_config_overrides.get(combo.id, {}))
    result["memory"] = _copy_service(group.memory_template, group.source_root, root / "memory",
                                      group.memory_config_overrides)
    return result
