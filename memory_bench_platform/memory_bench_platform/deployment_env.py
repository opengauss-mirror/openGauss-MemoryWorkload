"""Explicit dotenv inputs and scalar-safe configuration interpolation."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import hashlib
import json

import yaml
from dotenv import dotenv_values

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
_TYPED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):(int|float|bool)\}")


def expand_environment(value):
    """Expand values after JSON/YAML parsing: credentials cannot become syntax."""
    if isinstance(value, dict):
        return {key: expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_environment(item) for item in value]
    if not isinstance(value, str):
        return value
    typed = _TYPED.fullmatch(value)
    if typed:
        name, kind = typed.groups()
        raw = os.environ.get(name)
        try:
            if raw is None:
                raise ValueError()
            if kind == "bool":
                return {"true": True, "false": False}[raw.lower()]
            return int(raw) if kind == "int" else float(raw)
        except (KeyError, ValueError):
            raise ValueError(f"environment variable {name} must be {kind}") from None

    def replace(match):
        name, default = match.groups()
        result = os.environ.get(name)
        if not result:
            if default is not None:
                return default
            raise ValueError(f"missing environment variable: {name}")
        return result

    # A doubled dollar sign is literal, matching Compose's escape convention.
    return "$".join(_VARIABLE.sub(replace, part) for part in value.split("$$"))


def environment_file(config_path: Path) -> Path | None:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    name = raw.get("env_file") if isinstance(raw, dict) else None
    if name is None:
        return None
    if not isinstance(name, str) or not name:
        raise ValueError("env_file must be a nonempty path")
    path = (config_path.resolve().parent / name).resolve()
    if not path.is_file():
        raise ValueError(f"missing env_file: {path}")
    return path


def input_fingerprint(config):
    """Track effective inputs without storing credentials in deployment state."""
    paths = [config.source_path]
    names = set()
    for section in (config.agents, config.memories, config.vectors, config.graphs,
                    config.mocks, config.gateway_pools):
        for service in section.values():
            names.update(service.environment_from.values())
            if service.client:
                names.update(service.client.environment_from.values())
            paths.extend(config.source_path.parent / f.source for f in service.config_files.values() if f.expand_env)
    for combo in config.combinations:
        if combo.plugin_runtime and combo.plugin_runtime.expand_env:
            paths.extend(config.source_path.parent / p for p in
                         (combo.plugin_runtime.source_config, combo.plugin_runtime.memory_config) if p)
    for path in paths:
        names.update(m[0] for m in _VARIABLE.findall(path.read_text()))
        names.update(m[0] for m in _TYPED.findall(path.read_text()))
    # Include the dotenv names consumed directly by a native plugin preparer.
    path = environment_file(config.source_path)
    if path:
        names.update(dotenv_values(path, interpolate=False))
    values = {name: os.environ.get(name) for name in sorted(names)}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


@contextmanager
def deployment_environment(config_path: Path | None):
    """Load only the explicitly selected file; shell values take precedence."""
    path = environment_file(config_path) if config_path else None
    values = dotenv_values(path, interpolate=False) if path else {}
    added = []
    try:
        for key, value in values.items():
            if value is not None and key not in os.environ:
                os.environ[key] = value
                added.append(key)
        yield
    finally:
        for key in added:
            os.environ.pop(key, None)
