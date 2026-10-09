"""Render one isolated Docker Compose project from a resolved group."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .deployment_config import DeploymentGroup, Service


@dataclass(frozen=True)
class ResourceRequest:
    cpus: float
    memory_mb: int


def required_resources(groups: tuple[DeploymentGroup, ...]) -> ResourceRequest:
    cpu = 0.0
    memory = 0
    for group in groups:
        services = [*group.agents.values(), group.memory_template, group.vector_template, group.graph_template,
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
    if template.command:
        result["command"] = template.command
    ports = [_port(template.container_port)] if template.container_port else []
    ports.extend(_port(endpoint.container_port) for endpoint in template.endpoints.values())
    if ports:
        result["ports"] = ports
    volumes = list(mounts)
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


def _url(template: Service, name: str, endpoint_name: str | None = None) -> str:
    endpoint = template.endpoints[endpoint_name] if endpoint_name else None
    scheme = endpoint.endpoint_scheme if endpoint else template.endpoint_scheme
    port = endpoint.container_port if endpoint else template.container_port
    return f"{scheme}://{name}:{port}"


def render_project(group: DeploymentGroup, deployment_name: str,
                   generated_mounts: dict[str, list[dict[str, object]]],
                   deployment_root: Path | None = None) -> dict[str, object]:
    project = f"mb_{deployment_name}_{group.project_key}".replace("-", "_")
    services: dict[str, object] = {}
    networks: dict[str, object] = {"storage_net": {}}
    volumes: dict[str, object] = {}
    agent_networks = {id_: "agent_net_" + id_.replace("-", "_") for id_ in group.combination_ids}
    networks.update({network: {} for network in agent_networks.values()})
    memory_networks = [*agent_networks.values(), "storage_net"]
    independent_mock = group.memory_mock_template if not group.shared_memory_instance_id else None
    mock_memory_name = "mock_memory" if group.shared_memory_instance_id else "mock"

    def endpoint_vars(template: Service, combo_id: str | None = None) -> dict[str, str]:
        result = {}
        for env, target in template.service_endpoints.items():
            if target == "memory":
                result[env] = _url(group.memory_template, "memory")
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

    if group.vector_template:
        services["vector"] = _service(group.vector_template, group.source_root, [], ["storage_net"], {}, [], "vector", deployment_root)
    if group.graph_template:
        services["graph"] = _service(group.graph_template, group.source_root, [], ["storage_net"], {}, [], "graph", deployment_root)
    if group.memory_mock_template:
        mock_networks = ["storage_net"]
        if independent_mock:
            mock_networks += list(agent_networks.values())
        services[mock_memory_name] = _service(group.memory_mock_template, group.source_root, [], mock_networks, {}, [], mock_memory_name, deployment_root)
    for combo_id, mock in group.agent_mocks.items():
        name = "mock_" + combo_id.replace("-", "_")
        services[name] = _service(mock, group.source_root, [], [agent_networks[combo_id]], {}, [], name, deployment_root)
    memory_deps = [name for name in ("vector", "graph") if name in services]
    if group.memory_mock_template and any(v.startswith("mock.") for v in group.memory_template.service_endpoints.values()):
        memory_deps.append(mock_memory_name)
    services["memory"] = _service(group.memory_template, group.source_root, generated_mounts.get("memory", []),
                                   memory_networks, endpoint_vars(group.memory_template), memory_deps, "memory", deployment_root)
    for combo in group.combinations:
        name = "agent_" + combo.id.replace("-", "_")
        template = group.agents[combo.id]
        deps = ["memory"] if combo.memory_integration == "agent_plugin" else []
        if any(v.startswith("mock.") for v in template.service_endpoints.values()):
            deps.append("mock_" + combo.id.replace("-", "_") if combo.id in group.agent_mocks else mock_memory_name)
        services[name] = _service(template, group.source_root, generated_mounts.get(name, []),
                                  [agent_networks[combo.id]], endpoint_vars(template, combo.id), deps, name, deployment_root)
    for name, service in services.items():
        for mount in service.get("volumes", []):
            if mount["type"] == "volume":
                volumes[mount["source"]] = {}
    return {"name": project, "services": services, "networks": networks, "volumes": volumes}
