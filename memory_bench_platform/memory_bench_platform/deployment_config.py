"""Strict deployment description and physical-instance ownership resolution."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .integration import resolve_benchmark_entrypoint
from .paths import SKILLS_ROOT


_ID = re.compile(r"^[a-z][a-z0-9-]{0,47}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Image(StrictModel):
    name: str
    archive: Path | None = None


class Parameter(StrictModel):
    pointer: str
    type: Literal["string", "integer", "number", "boolean"]


class ConfigFile(StrictModel):
    source: Path
    target: str
    format: Literal["json", "yaml"]
    parameters: dict[str, Parameter]


class Mount(StrictModel):
    source: Path | None = None
    target: str
    host_visible: bool = False


class Client(StrictModel):
    endpoint_env: str
    environment_from: dict[str, str] = Field(default_factory=dict)


class Verify(StrictModel):
    command: list[str]
    expected_version: str


class Resources(StrictModel):
    cpus: float | None = None
    memory_mb: int | None = None
    numa_node: int | None = None


class Endpoint(StrictModel):
    container_port: int
    endpoint_scheme: str = "http"
    model_mode: str | None = None


class Service(StrictModel):
    skill: str | None = None
    image: Image
    command: list[str] | None = None
    container_port: int | None = None
    endpoint_scheme: str = "http"
    endpoints: dict[str, Endpoint] = Field(default_factory=dict)
    healthcheck: list[str]
    environment: dict[str, str] = Field(default_factory=dict)
    environment_from: dict[str, str] = Field(default_factory=dict)
    config_mounts: list[Mount] = Field(default_factory=list)
    config_files: dict[str, ConfigFile] = Field(default_factory=dict)
    state_mounts: list[Mount] = Field(default_factory=list)
    service_endpoints: dict[str, str] = Field(default_factory=dict)
    client: Client | None = None
    verify: Verify | None = None
    resources: Resources | None = None
    allow_agent_storage_access: bool = False


class PreparationStep(StrictModel):
    enabled: bool = False
    target: str | None = None
    command: list[str] | None = None
    timeout_seconds: int = 300
    consecutive_successes: int = 1

    @model_validator(mode="after")
    def require_command(self):
        if self.enabled and (not self.target or not self.command):
            raise ValueError("enabled preparation needs target and command")
        return self


class Preparation(StrictModel):
    seed: PreparationStep = Field(default_factory=PreparationStep)
    readiness: PreparationStep = Field(default_factory=PreparationStep)
    warmup: PreparationStep = Field(default_factory=PreparationStep)
    seed_resource_boost: Resources | None = None

    @model_validator(mode="after")
    def seed_needs_readiness(self):
        if self.seed.enabled and not self.readiness.enabled:
            raise ValueError("seed requires readiness")
        return self


class SharedMemory(StrictModel):
    memory: str
    vector: str | None = None
    graph: str | None = None
    mock: str | None = None
    preparation: Preparation = Field(default_factory=Preparation)


class Combination(StrictModel):
    id: str
    benchmark: str
    agent: str
    memory: str | None = None
    memory_instance: str | None = None
    vector: str | None = None
    graph: str | None = None
    mock: str | None = None
    memory_integration: Literal["backend_direct", "agent_plugin"]
    data_path: Path
    entrypoint: str | None = None
    preparation: Preparation = Field(default_factory=Preparation)

    @model_validator(mode="after")
    def one_memory(self):
        if (self.memory is None) == (self.memory_instance is None):
            raise ValueError("exactly one of memory and memory_instance is required")
        if self.memory_instance and (self.vector or self.graph):
            raise ValueError("shared memory combination cannot override vector or graph")
        return self


class DeploymentConfig(StrictModel):
    schema_: Literal["deployment/1"] = Field(alias="schema")
    name: str
    agents: dict[str, Service]
    memories: dict[str, Service]
    vectors: dict[str, Service] = Field(default_factory=dict)
    graphs: dict[str, Service] = Field(default_factory=dict)
    mocks: dict[str, Service] = Field(default_factory=dict)
    shared_memory_instances: dict[str, SharedMemory] = Field(default_factory=dict)
    combinations: list[Combination]
    pressure_tests: list[dict] = Field(default_factory=list)
    source_path: Path | None = Field(default=None, exclude=True)
    agent_config_overrides: dict[str, dict[str, object]] = Field(default_factory=dict, exclude=True)
    memory_config_overrides: dict[str, dict[str, object]] = Field(default_factory=dict, exclude=True)


class DeploymentGroup(StrictModel):
    project_key: str
    source_root: Path
    combination_ids: tuple[str, ...]
    shared_memory_instance_id: str | None
    combinations: tuple[Combination, ...]
    agents: dict[str, Service]
    memory_template: Service
    memory_instance_id: str
    vector_template: Service | None = None
    graph_template: Service | None = None
    memory_mock_template: Service | None = None
    agent_mocks: dict[str, Service] = Field(default_factory=dict)
    preparation: Preparation = Field(default_factory=Preparation)
    agent_config_overrides: dict[str, dict[str, object]] = Field(default_factory=dict)
    memory_config_overrides: dict[str, object] = Field(default_factory=dict)


class UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _pointer_value(data, pointer: str):
    if not pointer.startswith("/"):
        raise ValueError(f"invalid pointer: {pointer}")
    value = data
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError(f"missing pointer: {pointer}")
    return value


def _type_matches(value, kind: str) -> bool:
    return {"string": lambda: isinstance(value, str),
            "integer": lambda: type(value) is int,
            "number": lambda: type(value) in (int, float),
            "boolean": lambda: type(value) is bool}[kind]()


def _validate_service(service: Service, root: Path, role: str):
    if role in ("agent", "memory") and (not service.skill or not service.client or not service.verify):
        raise ValueError(f"{role} needs skill, client and verify")
    if not service.container_port and not service.endpoints:
        raise ValueError(f"{role} needs container_port or endpoints")
    targets = [m.target for m in service.config_mounts + service.state_mounts]
    names = set()
    for config_file in service.config_files.values():
        target = config_file.target
        if not target.startswith("/") or any(target == t or target.startswith(t.rstrip('/') + '/') or t.startswith(target.rstrip('/') + '/') for t in targets):
            raise ValueError(f"overlapping config target: {target}")
        targets.append(target)
        source = (root / config_file.source).resolve()
        if not source.is_file():
            raise ValueError(f"missing config source: {source}")
        try:
            data = json.loads(source.read_text()) if config_file.format == "json" else yaml.safe_load(source.read_text())
        except (ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"invalid config source: {source}") from exc
        for name, parameter in config_file.parameters.items():
            if name in names or not _ID.fullmatch(name.replace('_', '-')):
                raise ValueError(f"duplicate or invalid config parameter: {name}")
            names.add(name)
            value = _pointer_value(data, parameter.pointer)
            if not _type_matches(value, parameter.type):
                raise ValueError(f"config pointer type mismatch: {name}")
    if service.image.archive is not None and not (root / service.image.archive).is_file():
        raise ValueError(f"missing image archive: {service.image.archive}")


def load_deployment(path: Path) -> DeploymentConfig:
    path = path.resolve()
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    config = DeploymentConfig.model_validate(raw)
    config.source_path = path
    if not _ID.fullmatch(config.name) or not config.combinations:
        raise ValueError("invalid deployment name or empty combinations")
    root = path.parent
    for section, role in ((config.agents, "agent"), (config.memories, "memory"), (config.vectors, "vector"),
                          (config.graphs, "graph"), (config.mocks, "mock")):
        for name, service in section.items():
            if not _ID.fullmatch(name):
                raise ValueError(f"invalid service ID: {name}")
            _validate_service(service, root, role)
            if role in ("agent", "memory"):
                skill_dir = SKILLS_ROOT / ("agents" if role == "agent" else "memories") / service.skill
                if not (skill_dir / "manifest.yaml").is_file():
                    raise ValueError(f"unknown {role} skill: {service.skill}")
    ids = set()
    for combo in config.combinations:
        if not _ID.fullmatch(combo.id) or combo.id in ids:
            raise ValueError(f"duplicate or invalid combination ID: {combo.id}")
        ids.add(combo.id)
        for label, ref, section in (("agent", combo.agent, config.agents), ("memory", combo.memory, config.memories),
                                    ("memory_instance", combo.memory_instance, config.shared_memory_instances),
                                    ("vector", combo.vector, config.vectors), ("graph", combo.graph, config.graphs),
                                    ("mock", combo.mock, config.mocks)):
            if ref is not None and ref not in section:
                raise ValueError(f"unknown {label}: {ref}")
        if not (root / combo.data_path).is_file():
            raise ValueError(f"missing data_path: {combo.data_path}")
        entry = resolve_benchmark_entrypoint(combo.benchmark, combo.entrypoint)
        if entry.entrypoint_kind not in ("case_builder", "scenario_builder"):
            raise ValueError(f"external benchmark entrypoint: {combo.entrypoint}")
    for name, shared in config.shared_memory_instances.items():
        if not _ID.fullmatch(name) or shared.memory not in config.memories:
            raise ValueError(f"invalid shared memory instance: {name}")
        for label, ref, section in (("vector", shared.vector, config.vectors), ("graph", shared.graph, config.graphs),
                                    ("mock", shared.mock, config.mocks)):
            if ref and ref not in section:
                raise ValueError(f"unknown shared {label}: {ref}")
    return config


def apply_config_overrides(config: DeploymentConfig, agent_items: list[str], memory_items: list[str]) -> DeploymentConfig:
    result = config.model_copy(deep=True)
    combos = {c.id: c for c in result.combinations}
    memories = {c.id: result.memories[c.memory] for c in result.combinations if c.memory}
    memories.update({name: result.memories[shared.memory] for name, shared in result.shared_memory_instances.items()})
    for role, items, instances, output in (("agent", agent_items, {k: result.agents[v.agent] for k, v in combos.items()}, result.agent_config_overrides),
                                            ("memory", memory_items, memories, result.memory_config_overrides)):
        for item in items:
            left, sep, encoded = item.partition("=")
            instance, colon, name = left.partition(":")
            if not sep or not colon or instance not in instances:
                raise ValueError(f"invalid {role} config selector: {left}")
            if name in output.get(instance, {}):
                raise ValueError(f"duplicate {role} config parameter: {left}")
            params = {key: parameter for file in instances[instance].config_files.values() for key, parameter in file.parameters.items()}
            if name not in params:
                raise ValueError(f"unknown {role} config parameter: {left}")
            try:
                value = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON value for {left}") from exc
            if not _type_matches(value, params[name].type):
                raise ValueError(f"wrong value type for {left}")
            output.setdefault(instance, {})[name] = value
    return result


def resolve_groups(config: DeploymentConfig) -> tuple[DeploymentGroup, ...]:
    by_key: dict[str, list[Combination]] = {}
    for combo in config.combinations:
        by_key.setdefault(combo.memory_instance or combo.id, []).append(combo)
    groups = []
    for key, combos in by_key.items():
        shared = config.shared_memory_instances.get(key)
        memory_id = shared.memory if shared else combos[0].memory
        vector_id = shared.vector if shared else combos[0].vector
        graph_id = shared.graph if shared else combos[0].graph
        memory_mock_id = shared.mock if shared else combos[0].mock
        groups.append(DeploymentGroup(
            project_key=key, source_root=config.source_path.parent, combination_ids=tuple(c.id for c in combos),
            shared_memory_instance_id=key if shared else None, combinations=tuple(combos),
            agents={c.id: config.agents[c.agent] for c in combos}, memory_template=config.memories[memory_id],
            memory_instance_id=key, vector_template=config.vectors.get(vector_id) if vector_id else None,
            graph_template=config.graphs.get(graph_id) if graph_id else None,
            memory_mock_template=config.mocks.get(memory_mock_id) if memory_mock_id else None,
            agent_mocks={c.id: config.mocks[c.mock] for c in combos if c.mock and shared},
            preparation=shared.preparation if shared else combos[0].preparation,
            agent_config_overrides={c.id: config.agent_config_overrides.get(c.id, {}) for c in combos},
            memory_config_overrides=config.memory_config_overrides.get(key, {}),
        ))
    return tuple(groups)
