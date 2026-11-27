"""Strict deployment description and physical-instance ownership resolution."""
from __future__ import annotations

import json
import ipaddress
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from .integration import MANAGED_GATEWAYS, resolve_benchmark_entrypoint, resolve_run_skill_bundle
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
    mode: Literal["0600", "0644"] = "0600"
    parameters: dict[str, Parameter] = Field(default_factory=dict)
    expand_env: bool = False


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
    privileged: bool = False
    skill: str | None = None
    image: Image | None = None
    placement: Literal["service", "agent"] = "service"
    command: list[str] | None = None
    container_port: int | None = None
    endpoint_scheme: str = "http"
    endpoints: dict[str, Endpoint] = Field(default_factory=dict)
    healthcheck: list[str] = Field(default_factory=list)
    healthcheck_interval_seconds: int | None = Field(default=None, gt=0)
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


class GatewayPool(Service):
    kind: Literal["openclaw_http_pool_v1"]
    command: list[str]
    container_port: int

    @model_validator(mode="after")
    def pool_only_fields(self):
        if not self.command or any((self.skill, self.client, self.verify, self.config_files,
                                    self.service_endpoints, self.endpoints)):
            raise ValueError("gateway pool needs a command and cannot use Agent/Memory fields")
        return self


class PreparationStep(StrictModel):
    enabled: bool = False
    target: str | None = None
    command: list[str] | None = None
    timeout_seconds: int = 300
    consecutive_successes: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def require_command(self):
        if self.enabled and (not self.target or not self.command):
            raise ValueError("enabled preparation needs target and command")
        return self


class GoldenIndex(StrictModel):
    type: Literal["HNSW"]
    metric: Literal["L2"]
    M: int = Field(gt=0)
    ef_construction: int = Field(alias="efConstruction", gt=0)


class GoldenVolume(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    helper_image: str = Field(min_length=1)
    collection: str = Field(min_length=1)
    users: list[str] = Field(min_length=1)
    count_per_user: int = Field(gt=0)
    dimensions: int = Field(gt=0)
    seed_version: str = Field(min_length=1)
    index: GoldenIndex
    finalize: PreparationStep

    @model_validator(mode="after")
    def unique_users(self):
        if any(not user for user in self.users) or len(set(self.users)) != len(self.users):
            raise ValueError("Golden volume users must be nonempty and unique")
        return self


class Preparation(StrictModel):
    seed: PreparationStep = Field(default_factory=PreparationStep)
    readiness: PreparationStep = Field(default_factory=PreparationStep)
    warmup: PreparationStep = Field(default_factory=PreparationStep)
    seed_resource_boost: Resources | None = None
    golden_volume: GoldenVolume | None = None

    @model_validator(mode="after")
    def seed_needs_readiness(self):
        if self.seed.enabled and not self.readiness.enabled:
            raise ValueError("seed requires readiness")
        if self.golden_volume and not (self.seed.enabled and self.readiness.enabled
                                       and self.golden_volume.finalize.enabled):
            raise ValueError("Golden volume requires seed, readiness and finalize")
        return self


class SharedMemory(StrictModel):
    memory: str
    vector: str | None = None
    graph: str | None = None
    mock: str | None = None
    preparation: Preparation = Field(default_factory=Preparation)


class PluginRuntime(StrictModel):
    expand_env: bool = False
    memory_config: Path | None = None
    source_config: Path
    upstream_plugin: Path | None = None
    options: dict = Field(default_factory=dict)


class DeploymentExecution(StrictModel):
    sample_concurrency: int = Field(default=1, gt=0, strict=True)
    qa_concurrency: int | None = Field(default=None, gt=0, strict=True)
    judge_concurrency: int | None = Field(default=None, gt=0, strict=True)


class Combination(StrictModel):
    id: str
    benchmark: str
    agent: str
    gateway_pool: str | None = None
    agent_replicas: int = 1
    memory: str | None = None
    memory_instance: str | None = None
    vector: str | None = None
    graph: str | None = None
    mock: str | None = None
    memory_integration: Literal["backend_direct", "agent_plugin"]
    data_path: Path
    entrypoint: str | None = None
    plugin_runtime: PluginRuntime | None = None
    execution: DeploymentExecution = Field(default_factory=DeploymentExecution)
    preparation: Preparation = Field(default_factory=Preparation)

    _sample_gateways: int = PrivateAttr(default=1)

    def agent_services(self):
        base = "agent_" + self.id.replace("-", "_")
        if self.gateway_pool:
            return [f"{base}_{i}" for i in range(1, self.agent_replicas + 1)]
        return [base, *(f"{base}_lane_{i}" for i in range(2, self._sample_gateways + 1))]

    @model_validator(mode="after")
    def one_memory(self):
        if (self.memory is None) == (self.memory_instance is None):
            raise ValueError("exactly one of memory and memory_instance is required")
        if self.memory_instance and (self.vector or self.graph):
            raise ValueError("shared memory combination cannot override vector or graph")
        if (self.memory_integration == "agent_plugin" and self.execution.sample_concurrency == 1
                and (self.execution.qa_concurrency or 1) > 1):
            raise ValueError("agent_plugin deployment requires execution.qa_concurrency=1")
        return self


class ExperimentOptions(StrictModel):
    fresh: bool = False
    cleanup_owned_deployment: bool = False


class DeploymentConfig(StrictModel):
    experiment: ExperimentOptions = Field(default_factory=ExperimentOptions)
    env_file: Path | None = None
    local_images_only: bool = False
    schema_: Literal["deployment/1"] = Field(alias="schema")
    name: str
    agents: dict[str, Service]
    memories: dict[str, Service]
    vectors: dict[str, Service] = Field(default_factory=dict)
    graphs: dict[str, Service] = Field(default_factory=dict)
    mocks: dict[str, Service] = Field(default_factory=dict)
    gateway_pools: dict[str, GatewayPool] = Field(default_factory=dict)
    shared_memory_instances: dict[str, SharedMemory] = Field(default_factory=dict)
    combinations: list[Combination]
    network_subnets: dict[str, dict[str, str]] = Field(default_factory=dict)
    pressure_tests: list[dict] = Field(default_factory=list)
    source_path: Path | None = Field(default=None, exclude=True)
    agent_config_overrides: dict[str, dict[str, object]] = Field(default_factory=dict, exclude=True)
    memory_config_overrides: dict[str, dict[str, object]] = Field(default_factory=dict, exclude=True)


class DeploymentGroup(StrictModel):
    project_key: str
    source_root: Path
    combination_ids: tuple[str, ...]
    network_subnets: dict[str, str] = Field(default_factory=dict)
    shared_memory_instance_id: str | None
    combinations: tuple[Combination, ...]
    agents: dict[str, Service]
    gateway_pools: dict[str, GatewayPool] = Field(default_factory=dict)
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
    if service.placement == "agent":
        if role != "memory" or not service.skill or not service.client:
            raise ValueError("Agent-hosted memory requires a memory skill and client")
        if any((service.image, service.command, service.healthcheck, service.healthcheck_interval_seconds, service.verify, service.config_files,
                service.config_mounts, service.state_mounts, service.resources, service.environment,
                service.environment_from, service.service_endpoints, service.endpoints)):
            raise ValueError("Agent-hosted memory is configured by plugin_runtime, not a separate service")
        if service.container_port is not None and not 1 <= service.container_port <= 65535:
            raise ValueError("Invalid Agent-hosted memory port")
        return
    if service.image is None or not service.healthcheck:
        raise ValueError(f"{role} service needs image and healthcheck")
    if role in ("agent", "memory") and (not service.skill or not service.client or not service.verify):
        raise ValueError(f"{role} needs skill, client and verify")
    if role in ("agent", "memory") and service.container_port is None:
        raise ValueError(f"{role} client needs container_port")
    if not service.container_port and not service.endpoints:
        raise ValueError(f"{role} needs container_port or endpoints")
    if service.container_port is not None and not 1 <= service.container_port <= 65535:
        raise ValueError(f"invalid {role} port")
    if any(not 1 <= endpoint.container_port <= 65535 for endpoint in service.endpoints.values()):
        raise ValueError(f"invalid {role} endpoint port")
    targets = [m.target for m in service.config_mounts + service.state_mounts]
    names = set()
    for config_file in service.config_files.values():
        target = config_file.target
        if not config_file.parameters and not config_file.expand_env:
            raise ValueError("config_files parameters cannot be empty")
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
        if config_file.expand_env:
            from .deployment_env import expand_environment
            data = expand_environment(data)
        pointers = set()
        for name, parameter in config_file.parameters.items():
            if name in names or not _ID.fullmatch(name.replace('_', '-')):
                raise ValueError(f"duplicate or invalid config parameter: {name}")
            if parameter.pointer in pointers:
                raise ValueError(f"duplicate config pointer: {parameter.pointer}")
            pointers.add(parameter.pointer)
            names.add(name)
            value = _pointer_value(data, parameter.pointer)
            if not _type_matches(value, parameter.type):
                raise ValueError(f"config pointer type mismatch: {name}")
    if service.image.archive is not None and not (root / service.image.archive).is_file():
        raise ValueError(f"missing image archive: {service.image.archive}")
    for mount in service.config_mounts:
        if mount.source is None or not (root / mount.source).exists():
            raise ValueError(f"missing config mount source: {mount.source}")


def load_deployment(path: Path) -> DeploymentConfig:
    from .deployment_env import deployment_environment
    with deployment_environment(path):
        return _load_deployment(path)


def _load_deployment(path: Path) -> DeploymentConfig:
    path = path.resolve()
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    if isinstance(raw, dict) and raw.get("env_file"):
        from .deployment_env import expand_environment
        raw = expand_environment(raw)
    config = DeploymentConfig.model_validate(raw)
    config.source_path = path
    if not _ID.fullmatch(config.name) or not config.combinations:
        raise ValueError("invalid deployment name or empty combinations")
    root = path.parent
    for section, role in ((config.agents, "agent"), (config.memories, "memory"), (config.vectors, "vector"),
                          (config.graphs, "graph"), (config.mocks, "mock"),
                          (config.gateway_pools, "gateway pool")):
        for name, service in section.items():
            if not _ID.fullmatch(name):
                raise ValueError(f"invalid service ID: {name}")
            _validate_service(service, root, role)
            if role in ("agent", "memory"):
                skill_dir = SKILLS_ROOT / ("agents" if role == "agent" else "memories") / service.skill
                if not (skill_dir / "manifest.yaml").is_file():
                    raise ValueError(f"unknown {role} skill: {service.skill}")
    ids = set()
    shared_names = set(config.shared_memory_instances)
    for combo in config.combinations:
        if not _ID.fullmatch(combo.id) or combo.id in ids:
            raise ValueError(f"duplicate or invalid combination ID: {combo.id}")
        if combo.id in shared_names:
            raise ValueError(f"combination ID conflicts with shared memory ID: {combo.id}")
        ids.add(combo.id)
        for label, ref, section in (("agent", combo.agent, config.agents), ("memory", combo.memory, config.memories),
                                    ("memory_instance", combo.memory_instance, config.shared_memory_instances),
                                    ("vector", combo.vector, config.vectors), ("graph", combo.graph, config.graphs),
                                    ("mock", combo.mock, config.mocks),
                                    ("gateway_pool", combo.gateway_pool, config.gateway_pools)):
            if ref is not None and ref not in section:
                raise ValueError(f"unknown {label}: {ref}")
        if not (root / combo.data_path).is_file():
            raise ValueError(f"missing data_path: {combo.data_path}")
        memory_id = combo.memory or config.shared_memory_instances[combo.memory_instance].memory
        bundle = resolve_run_skill_bundle(combo.benchmark, config.agents[combo.agent].skill,
                                          config.memories[memory_id].skill, combo.memory_integration)
        if combo.memory_integration == "agent_plugin" and combo.execution.sample_concurrency > 1:
            if (not combo.plugin_runtime or not bundle.benchmark.entry.scenario_builder or combo.entrypoint
                    or config.agents[combo.agent].skill != "openclaw" or not memory_id
                    or bundle.memory_plugin_id not in MANAGED_GATEWAYS or combo.gateway_pool):
                raise ValueError("Pooled sample execution requires the scenario-based OpenClaw deployment with a supported memory plugin")
            from .integration import build_benchmark_scenario
            samples = build_benchmark_scenario(combo.benchmark, str(root / combo.data_path)).samples
            if not isinstance(samples, list) or not samples:
                raise ValueError("Expected a nonempty benchmark sample list")
            combo._sample_gateways = min(len(samples), combo.execution.sample_concurrency)
        if combo.agent_replicas < 1 or (not combo.gateway_pool and combo.agent_replicas != 1):
            raise ValueError("agent_replicas requires gateway_pool and must be positive")
        if combo.gateway_pool:
            agent = config.agents[combo.agent]
            if agent.skill != "openclaw" or agent.client.endpoint_env != "OPENCLAW_GATEWAY_URL":
                raise ValueError("gateway_pool requires OpenClaw HTTP client")
            if agent.environment.get("OPENCLAW_TRANSPORT") == "cli":
                raise ValueError("gateway_pool conflicts with CLI transport")
            if any(step.enabled and step.target == "agent" for step in
                   (combo.preparation.seed, combo.preparation.readiness, combo.preparation.warmup)):
                raise ValueError("agent preparation bypasses gateway_pool")
        entry = resolve_benchmark_entrypoint(combo.benchmark, combo.entrypoint)
        if entry.entrypoint_kind not in ("case_builder", "scenario_builder"):
            raise ValueError(f"external benchmark entrypoint: {combo.entrypoint}")

    group_combos: dict[str, list[Combination]] = {}
    for combo in config.combinations:
        group_combos.setdefault(combo.memory_instance or combo.id, []).append(combo)
    selected_subnets: list[ipaddress.IPv4Network] = []
    for group_id, networks in config.network_subnets.items():
        if group_id not in group_combos:
            raise ValueError(f"unknown network subnet group: {group_id}")
        allowed = {"storage_net", *("agent_net_" + combo.id.replace("-", "_") for combo in group_combos[group_id])}
        for name, value in networks.items():
            if name not in allowed:
                raise ValueError(f"unknown network subnet: {group_id}/{name}")
            try:
                subnet = ipaddress.ip_network(value, strict=True)
            except ValueError as exc:
                raise ValueError(f"invalid network subnet: {group_id}/{name}") from exc
            if not isinstance(subnet, ipaddress.IPv4Network):
                raise ValueError(f"invalid network subnet: {group_id}/{name}")
            if any(subnet.overlaps(other) for other in selected_subnets):
                raise ValueError("overlapping network subnets")
            selected_subnets.append(subnet)
    physical_names: dict[str, set[str]] = {}
    for combo in config.combinations:
        group_key = combo.memory_instance or combo.id
        names = physical_names.setdefault(group_key, set())
        planned = combo.agent_services()
        if any(name in names for name in planned):
            raise ValueError(f"service name collision in {group_key}")
        names.update(planned)
    for name, shared in config.shared_memory_instances.items():
        if not _ID.fullmatch(name) or shared.memory not in config.memories:
            raise ValueError(f"invalid shared memory instance: {name}")
        for label, ref, section in (("vector", shared.vector, config.vectors), ("graph", shared.graph, config.graphs),
                                    ("mock", shared.mock, config.mocks)):
            if ref and ref not in section:
                raise ValueError(f"unknown shared {label}: {ref}")
    for combo in config.combinations:
        shared = config.shared_memory_instances.get(combo.memory_instance)
        if shared and (combo.preparation.seed.enabled or combo.preparation.readiness.enabled):
            raise ValueError("shared memory combination preparation allows only warmup")
        vector = shared.vector if shared else combo.vector
        graph = shared.graph if shared else combo.graph
        memory = config.memories[shared.memory if shared else combo.memory]
        agent = config.agents[combo.agent]
        if memory.placement == "agent":
            if (not combo.plugin_runtime or combo.memory_instance or combo.mock
                    or any(step.enabled for step in (combo.preparation.seed, combo.preparation.readiness, combo.preparation.warmup))):
                raise ValueError("Agent-hosted memory requires a dedicated plugin runtime without shared services or preparation steps")
            if memory.container_port is None and "memory" in agent.service_endpoints.values():
                raise ValueError("In-process SDK has no memory HTTP endpoint; remove the Agent memory service reference")
            if memory.container_port == agent.container_port:
                raise ValueError("Agent-hosted memory endpoint must differ from Gateway port")
        if combo.plugin_runtime:
            if combo.memory_integration != "agent_plugin" or combo.gateway_pool:
                raise ValueError("plugin_runtime requires agent_plugin with a dedicated Agent (no gateway pool)")
            from .deployment_plugins import deployment_adapter
            _, adapter = deployment_adapter(agent.skill, memory.skill)
            if adapter.get("MAX_SAMPLES"):
                from .integration import build_benchmark_scenario
                scenario = build_benchmark_scenario(combo.benchmark, str((root / combo.data_path).resolve()))
                if len(scenario.samples) > adapter["MAX_SAMPLES"]:
                    raise ValueError("This plugin requires a fresh deployment per sample; split the dataset before deployment")
            if adapter.get("MEMORY_PLACEMENT", "service") != memory.placement:
                raise ValueError("Memory placement does not match the plugin deployment adapter")
            if agent.config_files or agent.config_mounts or agent.state_mounts:
                raise ValueError("plugin_runtime owns the Agent runtime mounts")
            for source in (combo.plugin_runtime.source_config, combo.plugin_runtime.upstream_plugin, combo.plugin_runtime.memory_config):
                if source is not None and not (config.source_path.parent / source).exists():
                    raise ValueError(f"plugin runtime source missing: {source}")
        if agent.client.endpoint_env == memory.client.endpoint_env:
            raise ValueError(f"client endpoint_env collision in {combo.id}")
        storage_consumer = agent if memory.placement == "agent" else memory
        if vector and "vector" not in storage_consumer.service_endpoints.values():
            raise ValueError(f"memory {memory.skill} does not reference configured vector")
        if graph and "graph" not in storage_consumer.service_endpoints.values():
            raise ValueError(f"memory {memory.skill} does not reference configured graph")
        if combo.memory_integration == "agent_plugin" and memory.placement == "service" and "memory" not in agent.service_endpoints.values():
            raise ValueError(f"agent {combo.agent} does not reference configured memory")
        for consumer, role, mock_ref in ((agent, "agent", combo.mock), (memory, "memory", shared.mock if shared else combo.mock)):
            for ref in consumer.service_endpoints.values():
                if ref.startswith("mock."):
                    if not mock_ref or ref[5:] not in config.mocks[mock_ref].endpoints:
                        raise ValueError(f"missing mock endpoint: {ref}")
                elif ref == "memory" and role == "agent":
                    pass
                elif ref == "vector" and vector and (role == "memory" or agent.allow_agent_storage_access):
                    pass
                elif ref == "graph" and graph and (role == "memory" or agent.allow_agent_storage_access):
                    pass
                else:
                    raise ValueError(f"invalid {role} service endpoint: {ref}")
        available = {"memory"}
        if vector:
            available.add("vector")
        if graph:
            available.add("graph")
        memory_mock = shared.mock if shared else combo.mock
        if memory_mock or combo.mock:
            available.add("mock")
        preparations = [shared.preparation if shared else combo.preparation]
        if shared:
            preparations.append(combo.preparation)
        for preparation in preparations:
            if preparation.golden_volume:
                mounts = config.vectors[vector].state_mounts if vector else []
                if len(mounts) != 1 or mounts[0].host_visible:
                    raise ValueError("Golden volume requires one named vector state mount")
            steps = (preparation.seed, preparation.readiness, preparation.warmup)
            if preparation.golden_volume:
                steps += (preparation.golden_volume.finalize,)
            for step in steps:
                if not step.enabled:
                    continue
                if step.target not in available | {"agent"}:
                    raise ValueError(f"invalid preparation target: {step.target}")
                if shared and preparation is shared.preparation and step.target == "agent":
                    raise ValueError("shared preparation target cannot be agent")
                if shared and preparation is shared.preparation and step.target == "mock" and not shared.mock:
                    raise ValueError("shared preparation target requires shared mock")
    if config.network_subnets:
        from .deployment_compose import render_project
        for group in resolve_groups(config):
            if not group.network_subnets:
                continue
            services = render_project(group, config.name, {})["services"]
            for name, value in group.network_subnets.items():
                members = sum(name in service["networks"] for service in services.values())
                if ipaddress.ip_network(value).num_addresses - 3 < members:
                    raise ValueError(f"network subnet too small: {group.project_key}/{name}")
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
            network_subnets=config.network_subnets.get(key, {}),
            shared_memory_instance_id=key if shared else None, combinations=tuple(combos),
            agents={c.id: config.agents[c.agent] for c in combos},
            gateway_pools={c.id: config.gateway_pools[c.gateway_pool] for c in combos if c.gateway_pool},
            memory_template=config.memories[memory_id],
            memory_instance_id=key, vector_template=config.vectors.get(vector_id) if vector_id else None,
            graph_template=config.graphs.get(graph_id) if graph_id else None,
            memory_mock_template=config.mocks.get(memory_mock_id) if memory_mock_id else None,
            agent_mocks={c.id: config.mocks[c.mock] for c in combos if c.mock and shared},
            preparation=shared.preparation if shared else combos[0].preparation,
            agent_config_overrides={c.id: config.agent_config_overrides.get(c.id, {}) for c in combos},
            memory_config_overrides=config.memory_config_overrides.get(key, {}),
        ))
    return tuple(groups)
