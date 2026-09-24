"""Validated multi-user bindings. No services are contacted during parsing."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .paths import SKILLS_ROOT

Alias = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Command = Annotated[list[str], Field(min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Controls(StrictModel):
    add_rate: Nonnegative = 0
    search_rate: Nonnegative = 0
    request_timeout_seconds: Positive = 120
    drain_timeout_seconds: Positive = 600
    poll_interval_seconds: Nonnegative = 1


class Workload(StrictModel):
    format: Literal["openmem-v1", "openmem-agent/1"]
    add_file: Path
    search_file: Path


class Instance(StrictModel):
    kind: Literal["memory", "openclaw"]
    deployment: Literal["external", "managed"] = "external"
    memory_skill: str | None = None
    plugin_id: str = "openclaw-openviking"
    environment: dict[str, str] = Field(default_factory=dict)
    environment_from: dict[str, str] = Field(default_factory=dict)
    healthcheck_command: Command
    start_command: Command | None = None
    isolation_probe_command: Command | None = None
    startup_timeout_seconds: Positive = 120
    max_inflight: Annotated[int, Field(ge=1, le=256, strict=True)] = 8
    cwd: Path = Path(".")
    perf_trace_path: Path | None = None
    exclusive_resources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_instance(self):
        if self.kind == "memory" and not self.memory_skill:
            raise ValueError("memory instance requires memory_skill")
        if self.deployment == "managed" and not self.start_command:
            raise ValueError("managed instance requires start_command")
        if self.deployment == "managed" and not self.exclusive_resources:
            raise ValueError("managed instance requires exclusive_resources")
        if self.deployment == "external" and self.start_command:
            raise ValueError("external instance cannot have start_command")
        if self.kind == "openclaw" and not self.isolation_probe_command:
            raise ValueError("OpenClaw requires isolation_probe_command")
        if set(self.environment) & set(self.environment_from):
            raise ValueError("ambiguous instance environment")
        return self

    def resolve_environment(self) -> dict[str, str]:
        if any(name not in os.environ for name in self.environment_from.values()):
            raise ValueError("missing instance environment reference")
        return {**self.environment, **{key: os.environ[name] for key, name in self.environment_from.items()}}


class User(StrictModel):
    id: Alias
    workload: Alias
    instance: Alias
    agent_id: Alias | None = None
    controls: Controls | None = None


class MultiUserConfig(StrictModel):
    schema_version: Literal["multi-user-replay/1"] = Field(alias="schema")
    mode: Literal["backend_direct", "openclaw"]
    model_mode: Literal["real-model", "replay-zero-delay", "replay-with-delay", "mock-fixed"] = "real-model"
    defaults: Controls = Field(default_factory=Controls)
    workloads: dict[Alias, Workload]
    instances: dict[Alias, Instance]
    users: Annotated[list[User], Field(min_length=1)]
    max_workers: Annotated[int, Field(ge=1, le=256, strict=True)] = 32

    @model_validator(mode="after")
    def validate_bindings(self):
        ids = [user.id for user in self.users]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate user id")
        agents = set()
        for user in self.users:
            if user.workload not in self.workloads or user.instance not in self.instances:
                raise ValueError("unknown workload or instance")
            instance = self.instances[user.instance]
            workload = self.workloads[user.workload]
            if self.mode == "openclaw":
                if instance.kind != "openclaw" or workload.format != "openmem-agent/1" or not user.agent_id:
                    raise ValueError("OpenClaw requires agent workloads, instances and per-user agent_id")
                if (user.instance, user.agent_id) in agents:
                    raise ValueError("users on one OpenClaw instance require distinct agents")
                agents.add((user.instance, user.agent_id))
            elif instance.kind != "memory" or workload.format != "openmem-v1":
                raise ValueError("backend_direct requires memory instances and openmem-v1")
        return self

    def controls_for(self, user: User) -> Controls:
        overrides = user.controls.model_dump(exclude_unset=True) if user.controls else {}
        return Controls(**{**self.defaults.model_dump(), **overrides})


@lru_cache(maxsize=1)
def replay_module():
    """Reuse the installed single-user benchmark without changing its public script."""
    path = SKILLS_ROOT / "benchmarks/production-http-replay/scripts/run_replay.py"
    spec = importlib.util.spec_from_file_location("_multi_user_replay_core", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def identity(run_id: str, user_id: str) -> str:
    digest = hashlib.sha256(json.dumps([run_id, user_id]).encode()).hexdigest()[:32]
    return "replay-" + digest


def validate_agent_request(request: dict, operation: str) -> None:
    allowed = {"user_id", "conversation_id", "messages" if operation == "add" else "query"}
    if set(request) - allowed:
        raise ValueError("unsupported_field in openmem-agent/1 request")
    if "conversation_id" in request and not isinstance(request["conversation_id"], str):
        raise ValueError("invalid conversation_id")
    if operation == "search":
        if not isinstance(request.get("query"), str) or not request["query"].strip():
            raise ValueError("search requires nonempty query")
    else:
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("add requires messages")
        for message in messages:
            if (not isinstance(message, dict) or set(message) - {"role", "content"}
                    or message.get("role") not in {"user", "assistant", "system"}
                    or not isinstance(message.get("content"), str)):
                raise ValueError("unsupported_field or invalid message in openmem-agent/1")


def load_config(path: Path) -> MultiUserConfig:
    path = Path(path).resolve()
    try:
        config = MultiUserConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (ValidationError, yaml.YAMLError, OSError):
        # Pydantic/YAML exceptions include raw input, potentially credentials.
        raise ValueError("invalid multi-user workload configuration") from None
    for workload in config.workloads.values():
        for name in ("add_file", "search_file"):
            setattr(workload, name, (path.parent / getattr(workload, name)).resolve())
        for operation, file in (("add", workload.add_file), ("search", workload.search_file)):
            count = 0
            for record in replay_module().iter_replay_records(file, operation, "preflight"):
                count += 1
                if config.mode == "openclaw":
                    validate_agent_request(record.request, operation)
            if not count:
                raise ValueError("workload files must not be empty")
    resources = set()
    for instance in config.instances.values():
        instance.cwd = (path.parent / instance.cwd).resolve()
        normalized = []
        for resource in instance.exclusive_resources:
            if resource.startswith("path:"):
                resource = "path:" + str((instance.cwd / resource[5:]).resolve())
            elif not resource.startswith("tcp:"):
                raise ValueError("exclusive resource must use path: or tcp:")
            if resource in resources:
                raise ValueError("duplicate exclusive resource")
            resources.add(resource)
            normalized.append(resource)
        instance.exclusive_resources = normalized
        if instance.perf_trace_path:
            instance.perf_trace_path = (path.parent / instance.perf_trace_path).resolve()
    return config


def preflight(config: MultiUserConfig) -> dict[str, dict[str, str]]:
    """Resolve all references/capabilities before starting any instance."""
    from .integration import get_memory_manifest, get_memory_plugin_manifest
    environments = {}
    endpoints = set()
    for name in dict.fromkeys(user.instance for user in config.users):
        instance = config.instances[name]
        env = instance.resolve_environment()
        environments[name] = env
        # Distinct aliases must not independently mutate the same OpenClaw.
        if instance.kind == "openclaw":
            endpoint = env.get("OPENCLAW_GATEWAY_URL") or env.get("OPENCLAW_GATEWAY_BASE_URL")
            if not endpoint or not env.get("OPENCLAW_CONFIG_PATH") or not env.get("OPENCLAW_STATE_DIR"):
                raise ValueError("OpenClaw requires explicit endpoint, config path and state directory")
            # The runner prefers GATEWAY_URL over BASE_URL; pin both in the
            # child so an inherited parent setting cannot change the target.
            env["OPENCLAW_GATEWAY_URL"] = endpoint
            env["OPENCLAW_GATEWAY_BASE_URL"] = endpoint
            for key in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR"):
                env[key] = str((instance.cwd / env[key]).resolve())
            keys = (endpoint.rstrip("/"), env["OPENCLAW_CONFIG_PATH"], env["OPENCLAW_STATE_DIR"])
            if any(key in endpoints for key in keys):
                raise ValueError("OpenClaw instances share endpoint or state resource")
            endpoints.update(keys)
            manifest = get_memory_plugin_manifest(instance.plugin_id)
            required = {"prepare", "set_phase", "flush", "wait_settle", "finalize"}
        else:
            manifest = get_memory_manifest(instance.memory_skill)
            if "openmem-v1" not in manifest.capabilities.get("raw_request_protocols", []):
                raise ValueError("memory skill lacks openmem-v1 protocol")
            required = {"ingest", "status", "recall"}
        if not manifest.entry.runner or not required.issubset(manifest.runtime.get("actions", [])):
            raise ValueError("instance skill lacks required actions")
    return environments
