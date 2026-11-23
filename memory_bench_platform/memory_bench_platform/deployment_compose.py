"""Render one isolated Docker Compose project from a resolved group."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from .deployment_config import DeploymentGroup, Service
from .paths import SKILLS_ROOT


_ESCAPED_LITERALS = "x-workload-escaped-literals"
_OPENCLAW_DNS_SCRIPT = "/opt/memory-bench/ipv4_provider_dns.cjs"


@dataclass(frozen=True)
class ResourceRequest:
    cpus: float
    memory_mb: int


def required_resources(groups: tuple[DeploymentGroup, ...]) -> ResourceRequest:
    cpu = 0.0
    memory = 0
    for group in groups:
        services = [*(group.agents[combo.id] for combo in group.combinations for _ in range(combo.agent_replicas)),
                    *group.gateway_pools.values(), group.memory_template, group.vector_template, group.graph_template,
                    group.memory_mock_template, *group.agent_mocks.values()]
        for service in services:
            if service and service.resources:
                cpu += service.resources.cpus or 0.0
                memory += service.resources.memory_mb or 0
    return ResourceRequest(cpu, memory)


def _port(port: int) -> dict[str, object]:
    return {"target": port, "host_ip": "127.0.0.1", "protocol": "tcp"}


def _service(template: Service, root: Path, mounts: list[dict[str, object]], networks: list[str],
             endpoint_vars: dict[str, str], dependencies: list[str], volume_prefix: str,
             deployment_root: Path | None = None) -> dict[str, object]:
    result: dict[str, object] = {"image": template.image.name, "networks": networks,
                                  "healthcheck": {"test": template.healthcheck},
                                  "environment": {**template.environment,
                                                  **{name: "${" + source + "}" for name, source in template.environment_from.items()},
                                                  **endpoint_vars}}
    if template.healthcheck_interval_seconds is not None:
        result["healthcheck"]["interval"] = f"{template.healthcheck_interval_seconds}s"
    if template.command:
        result["command"] = template.command
    ports = [_port(template.container_port)] if template.container_port else []
    ports.extend(_port(endpoint.container_port) for endpoint in template.endpoints.values())
    if ports:
        result["ports"] = ports
    volumes = list(mounts)
    if template.privileged:
        result["privileged"] = True
    if template.skill == "openclaw" and result["environment"].get("MEMORY_BENCH_IPV4_ONLY_HOSTS"):
        script = SKILLS_ROOT / "agents/openclaw/scripts/ipv4_provider_dns.cjs"
        volumes.append({"type": "bind", "source": str(script.resolve()),
                        "target": _OPENCLAW_DNS_SCRIPT, "read_only": True})
        options = result["environment"].get("NODE_OPTIONS", "").strip()
        result["environment"]["NODE_OPTIONS"] = f"{options} --require={_OPENCLAW_DNS_SCRIPT}".strip()
    for mount in template.config_mounts:
        volumes.append({"type": "bind", "source": str((root / mount.source).resolve()),
                        "target": mount.target, "read_only": True})
    for index, mount in enumerate(template.state_mounts):
        if mount.host_visible:
            source = str(((deployment_root or root) / "state" / volume_prefix / str(index)).resolve())
            volumes.append({"type": "bind", "source": source, "target": mount.target})
        else:
            volumes.append({"type": "volume", "source": f"{volume_prefix}_{index}", "target": mount.target})
    if volumes:
        result["volumes"] = volumes
    if dependencies:
        result["depends_on"] = {name: {"condition": "service_healthy"} for name in dependencies}
    if template.resources:
        limits = {}
        if template.resources.cpus is not None:
            limits["cpus"] = template.resources.cpus
        if template.resources.memory_mb is not None:
            limits["memory"] = f"{template.resources.memory_mb}M"
        if limits:
            result["deploy"] = {"resources": {"limits": limits}}
    return result


def _replace_in_values(value, old: str, new: str):
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace_in_values(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_in_values(item, old, new) for key, item in value.items()}
    return value


def compose_document(rendered: dict, templates: dict[str, Service]) -> dict:
    """Protect Workload-expanded literals from Compose interpolation."""
    document = _replace_in_values(rendered, "$", "$$")
    document[_ESCAPED_LITERALS] = True
    for name, template in templates.items():
        original = rendered["services"][name]["environment"]
        environment = document["services"][name]["environment"]
        for target, source in template.environment_from.items():
            reference = "$" + "{" + source + "}"
            value = original.get(target)
            if value == reference or (target == "NODE_OPTIONS" and template.skill == "openclaw"
                                      and value == f"{reference} --require={_OPENCLAW_DNS_SCRIPT}"):
                environment[target] = value
    return document


def compose_expectations(document: dict) -> dict:
    """Recover literal paths and other values for local status checks."""
    if document.get(_ESCAPED_LITERALS) is True:
        return _replace_in_values(document, "$$", "$")
    return document


def _url(template: Service, name: str, endpoint_name: str | None = None) -> str:
    endpoint = template.endpoints[endpoint_name] if endpoint_name else None
    scheme = endpoint.endpoint_scheme if endpoint else template.endpoint_scheme
    port = endpoint.container_port if endpoint else template.container_port
    return f"{scheme}://{name}:{port}"


def render_project(group: DeploymentGroup, deployment_name: str,
                   generated_mounts: dict[str, list[dict[str, object]]],
                   deployment_root: Path | None = None) -> dict[str, object]:
    project_hash = hashlib.sha256(f"{(deployment_root or group.source_root).resolve()}:{deployment_name}:{group.project_key}".encode()).hexdigest()[:10]
    project = f"mb_{deployment_name[:18]}_{group.project_key[:18]}_{project_hash}".replace("-", "_")
    services: dict[str, object] = {}
    networks: dict[str, object] = {"storage_net": {}}
    volumes: dict[str, object] = {}
    agent_networks = {id_: "agent_net_" + id_.replace("-", "_") for id_ in group.combination_ids}
    networks.update({network: {} for network in agent_networks.values()})
    for name, subnet in group.network_subnets.items():
        networks[name] = {"ipam": {"config": [{"subnet": subnet}]}}
    memory_networks = [*agent_networks.values(), "storage_net"]
    independent_mock = group.memory_mock_template if not group.shared_memory_instance_id else None
    mock_memory_name = "mock_memory" if group.shared_memory_instance_id else "mock"

    def endpoint_vars(template: Service, combo_id: str | None = None) -> dict[str, str]:
        result = {}
        for env, target in template.service_endpoints.items():
            if target == "memory":
                result[env] = _url(group.memory_template, "127.0.0.1" if group.memory_template.placement == "agent" else "memory")
            elif target == "vector" and group.vector_template:
                result[env] = _url(group.vector_template, "vector")
            elif target == "graph" and group.graph_template:
                result[env] = _url(group.graph_template, "graph")
            elif target.startswith("mock."):
                mock = group.agent_mocks.get(combo_id) if combo_id else group.memory_mock_template
                if not mock:
                    mock = independent_mock
                name = ("mock_" + combo_id.replace("-", "_")) if combo_id and group.shared_memory_instance_id else mock_memory_name
                result[env] = _url(mock, name, target[5:])
        return result

    state_root = (deployment_root / group.project_key) if deployment_root else (group.source_root / group.project_key)
    if group.vector_template:
        services["vector"] = _service(group.vector_template, group.source_root, [], ["storage_net"], {}, [], "vector", state_root)
    if group.graph_template:
        services["graph"] = _service(group.graph_template, group.source_root, [], ["storage_net"], {}, [], "graph", state_root)
    if group.memory_mock_template:
        mock_networks = ["storage_net"]
        if independent_mock:
            mock_networks += list(agent_networks.values())
        services[mock_memory_name] = _service(group.memory_mock_template, group.source_root, [], mock_networks, {}, [], mock_memory_name, state_root)
    for combo_id, mock in group.agent_mocks.items():
        name = "mock_" + combo_id.replace("-", "_")
        services[name] = _service(mock, group.source_root, [], [agent_networks[combo_id]], {}, [], name, state_root)
    memory_deps = [name for name in ("vector", "graph") if name in services]
    if group.memory_mock_template and any(v.startswith("mock.") for v in group.memory_template.service_endpoints.values()):
        memory_deps.append(mock_memory_name)
    if group.memory_template.placement != "agent":
        services["memory"] = _service(group.memory_template, group.source_root, generated_mounts.get("memory", []),
                                   memory_networks, endpoint_vars(group.memory_template), memory_deps, "memory", state_root)
    for combo in group.combinations:
        base = "agent_" + combo.id.replace("-", "_")
        template = group.agents[combo.id]
        deps = ["memory"] if combo.memory_integration == "agent_plugin" and group.memory_template.placement != "agent" else []
        storage_refs = {ref for ref in template.service_endpoints.values() if ref in ("vector", "graph")}
        deps.extend(sorted(storage_refs))
        if any(v.startswith("mock.") for v in template.service_endpoints.values()):
            deps.append("mock_" + combo.id.replace("-", "_") if combo.id in group.agent_mocks else mock_memory_name)
        agent_network_membership = [agent_networks[combo.id]]
        if template.allow_agent_storage_access and storage_refs:
            agent_network_membership.append("storage_net")
        names = [f"{base}_{index}" for index in range(1, combo.agent_replicas + 1)] if combo.gateway_pool else [base]
        for name in names:
            services[name] = _service(template, group.source_root, generated_mounts.get(name, []),
                                      agent_network_membership, endpoint_vars(template, combo.id), deps, name, state_root)
            if group.memory_template.placement == "agent" and group.memory_template.container_port:
                services[name].setdefault("ports", []).append(_port(group.memory_template.container_port))
            if combo.gateway_pool:
                services[name].pop("ports", None)
        if combo.gateway_pool:
            pool = group.gateway_pools[combo.id]
            pool_name = "gateway_pool_" + combo.id.replace("-", "_")
            pool_service = _service(pool, group.source_root, [], [agent_networks[combo.id]], {}, names,
                                    pool_name, state_root)
            pool_service["command"] = [*pool.command, "--host", "0.0.0.0", "--port", str(pool.container_port),
                                       *(part for name in names for part in ("--upstream", _url(template, name))),
                                       "--expected", str(combo.agent_replicas)]
            services[pool_name] = pool_service
    for name, service in services.items():
        for mount in service.get("volumes", []):
            if mount["type"] == "volume":
                volumes[mount["source"]] = {}
    return {"name": project, "services": services, "networks": networks, "volumes": volumes}
