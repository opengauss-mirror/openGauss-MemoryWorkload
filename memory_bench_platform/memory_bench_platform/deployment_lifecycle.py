"""Deploy, verify and remove owned Docker Compose workload environments."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

from .deployment_compose import render_project, required_resources
from .deployment_config import (DeploymentConfig, DeploymentGroup, Service,
                                _pointer_value, apply_config_overrides, load_deployment, resolve_groups)
from .deployment_config_files import prepare_instance_configs
from .deployment_docker import DockerClient
from .deployment_golden import (classify_volume, expected_manifest,
                                validate_finalize_summary, validate_seed_summary)
from .deployment_store import DeploymentState, DeploymentStore


class DeploymentFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentStatus:
    ready: bool
    drift: tuple[str, ...]
    state: DeploymentState


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resources_preflight(groups: tuple[DeploymentGroup, ...], docker: DockerClient) -> None:
    request = required_resources(groups)
    info = docker.info()
    if request.cpus > float(info.get("NCPU", 0)) or request.memory_mb * 1024**2 > int(info.get("MemTotal", 0)):
        raise DeploymentFailed("insufficient Docker CPU or memory")
    for group in groups:
        services = [*group.agents.values(), *group.gateway_pools.values(),
                    group.memory_template, group.vector_template, group.graph_template,
                    group.memory_mock_template, *group.agent_mocks.values()]
        for service in services:
            if service and service.resources and service.resources.numa_node is not None:
                node = service.resources.numa_node
                if platform.system() != "Linux" or not Path(f"/sys/devices/system/node/node{node}").exists():
                    raise DeploymentFailed(f"NUMA node {node} is unavailable")


def _service_templates(group: DeploymentGroup, rendered: dict | None = None) -> dict[str, Service]:
    result = {"memory": group.memory_template}
    for combo in group.combinations:
        base = "agent_" + combo.id.replace("-", "_")
        names = [f"{base}_{n}" for n in range(1, combo.agent_replicas + 1)] if combo.gateway_pool else [base]
        result.update({name: group.agents[combo.id] for name in names})
        if combo.gateway_pool:
            result["gateway_pool_" + combo.id.replace("-", "_")] = group.gateway_pools[combo.id]
    for name, template in (("vector", group.vector_template), ("graph", group.graph_template)):
        if template:
            result[name] = template
    if group.memory_mock_template:
        result["mock_memory" if group.shared_memory_instance_id else "mock"] = group.memory_mock_template
    result.update({"mock_" + id_.replace("-", "_"): template for id_, template in group.agent_mocks.items()})
    if rendered is not None:
        result = {name: template for name, template in result.items() if name in rendered["services"]}
    return result


def _expected_parameters(template: Service, mounts: list[dict]) -> dict[str, object]:
    expected = {}
    by_target = {mount["target"]: Path(mount["source"]) for mount in mounts if mount["type"] == "bind"}
    for spec in template.config_files.values():
        path = by_target[spec.target]
        data = json.loads(path.read_text()) if spec.format == "json" else yaml.safe_load(path.read_text())
        for name, parameter in spec.parameters.items():
            expected[name] = _pointer_value(data, parameter.pointer)
    return expected


def _verify_service(docker: DockerClient, container_id: str, template: Service,
                    rendered: dict, expected_image: str) -> list[str]:
    issues = []
    inspect = docker.inspect(container_id)
    if inspect.get("Id") != container_id or not inspect.get("State", {}).get("Running"):
        issues.append("container missing or stopped")
    if inspect.get("State", {}).get("Health", {}).get("Status") != "healthy":
        issues.append("container unhealthy")
    if inspect.get("Image") != expected_image:
        issues.append("image changed")
    limits = rendered.get("deploy", {}).get("resources", {}).get("limits", {})
    host_config = inspect.get("HostConfig", {})
    if "cpus" in limits and host_config.get("NanoCpus") != round(float(limits["cpus"]) * 1_000_000_000):
        issues.append("CPU limit changed")
    if "memory" in limits and host_config.get("Memory") != int(str(limits["memory"]).rstrip("M")) * 1024**2:
        issues.append("memory limit changed")
    actual_mounts = {(Path(m["Source"]).resolve(), m["Destination"]) for m in inspect.get("Mounts", []) if "Source" in m}
    for mount in rendered.get("volumes", []):
        if mount["type"] == "bind" and (Path(mount["source"]).resolve(), mount["target"]) not in actual_mounts:
            issues.append(f"mount changed: {mount['target']}")
    if template.verify:
        result = docker.exec_json(container_id, template.verify.command)
        if result.get("version") != template.verify.expected_version:
            issues.append("version changed")
        for key, value in rendered.get("environment", {}).items():
            if key in template.service_endpoints and result.get("endpoints", {}).get(key) != value:
                issues.append(f"endpoint changed: {key}")
        for dependency in rendered.get("depends_on", {}):
            if result.get("reachable", {}).get(dependency) is not True:
                issues.append(f"dependency unreachable: {dependency}")
        expected = _expected_parameters(template, rendered.get("volumes", []))
        for name, value in expected.items():
            if result.get("effective_parameters", {}).get(name) != value:
                issues.append(f"config parameter changed: {name}")
    return issues


def _ports(inspect: dict) -> dict[str, int]:
    result = {}
    for container_port, bindings in inspect.get("NetworkSettings", {}).get("Ports", {}).items():
        if bindings:
            binding = bindings[0]
            if binding.get("HostIp") not in ("127.0.0.1", "::1"):
                raise DeploymentFailed("published port is not loopback-only")
            result[container_port] = int(binding["HostPort"])
    return result


def _pool_status(url: str) -> dict:
    with urllib.request.urlopen(url + "/pool/status", timeout=5) as response:
        return json.load(response)


def _pool_issues(group: DeploymentGroup, ports: dict[str, dict[str, int]]) -> list[str]:
    issues = []
    for combo in group.combinations:
        if not combo.gateway_pool:
            continue
        name = "gateway_pool_" + combo.id.replace("-", "_")
        pool = group.gateway_pools[combo.id]
        port = ports[name][f"{pool.container_port}/tcp"]
        expected = {f"http://agent_{combo.id.replace('-', '_')}_{n}:{group.agents[combo.id].container_port}"
                    for n in range(1, combo.agent_replicas + 1)}
        actual = _pool_status(f"http://127.0.0.1:{port}").get("upstreams")
        if not isinstance(actual, list) or len(actual) != len(expected) or set(actual) != expected:
            issues.append(f"{name}: upstream mismatch")
    return issues


def _execute_preparation(group: DeploymentGroup, containers: dict[str, str], docker: DockerClient,
                         *, golden_manifest: dict | None = None, skip_seed: bool = False) -> dict:
    prep = group.preparation
    outcome = {"seed": False, "readiness": False}
    boost = prep.seed_resource_boost
    vector_id = containers.get("vector")
    boosted = False
    try:
        if prep.seed.enabled and not skip_seed:
            if boost:
                if not vector_id or not group.vector_template or not group.vector_template.resources:
                    raise DeploymentFailed("seed_resource_boost requires a vector with original resource limits")
                docker.update_resources(vector_id, cpus=boost.cpus, memory_mb=boost.memory_mb)
                boosted = True
            seed_summary = docker.exec_json(containers[_target_service(group, prep.seed.target)],
                                            prep.seed.command, timeout_seconds=prep.seed.timeout_seconds)
            if golden_manifest is not None:
                validate_seed_summary(seed_summary, golden_manifest)
            outcome["seed"] = True
        if prep.readiness.enabled:
            deadline = time.monotonic() + prep.readiness.timeout_seconds
            consecutive = 0
            while time.monotonic() < deadline:
                result = docker.exec_json(containers[_target_service(group, prep.readiness.target)], prep.readiness.command)
                consecutive = consecutive + 1 if result.get("ready") is True else 0
                if consecutive >= prep.readiness.consecutive_successes:
                    outcome["readiness"] = True
                    break
                time.sleep(1)
            if not outcome["readiness"]:
                raise DeploymentFailed("baseline readiness timed out")
    finally:
        if boosted:
            old = group.vector_template.resources
            docker.update_resources(vector_id, cpus=old.cpus, memory_mb=old.memory_mb)
    if boosted and prep.readiness.enabled:
        result = docker.exec_json(containers[_target_service(group, prep.readiness.target)], prep.readiness.command)
        if result.get("ready") is not True:
            raise DeploymentFailed("baseline not ready after resource restoration")
    return outcome


def _golden_volume_names(rendered: dict) -> tuple[str, str]:
    mount, = [item for item in rendered["services"]["vector"]["volumes"]
              if item["type"] == "volume"]
    key = mount["source"]
    return key, f"{rendered['name']}_{key}"


def _golden_mount_issue(inspect: dict, rendered: dict) -> str | None:
    _, name = _golden_volume_names(rendered)
    target = next(item["target"] for item in rendered["services"]["vector"]["volumes"]
                  if item["type"] == "volume")
    if not any(item.get("Name") == name and item.get("Destination") == target
               for item in inspect.get("Mounts", [])):
        return "Runtime volume mount changed"
    return None


def _golden_before_deploy(group: DeploymentGroup, rendered: dict,
                          docker: DockerClient) -> tuple[dict, str, float]:
    spec = group.preparation.golden_volume
    manifest = expected_manifest(spec, "vector", docker.inspect_image(group.vector_template.image.name)["Id"])
    key, runtime = _golden_volume_names(rendered)
    if docker.volume_exists(runtime):
        raise DeploymentFailed("Runtime volume already exists")
    if not docker.volume_exists(spec.name):
        return manifest, "build", 0.0
    if classify_volume(docker.volume_entries(spec.name, spec.helper_image), manifest,
                       docker.volume_digest(spec.name, spec.helper_image)) != "ready":
        raise DeploymentFailed("Golden volume is not ready")
    if docker.attached_volume_containers(spec.name):
        raise DeploymentFailed("Golden volume is attached to a container")
    started = time.monotonic()
    docker.create_volume(runtime, project=rendered["name"], volume_key=key)
    try:
        if docker.volume_entries(runtime, spec.helper_image):
            raise DeploymentFailed("Runtime volume already contains data")
        docker.copy_volume(spec.name, runtime, spec.helper_image)
        classify_volume(docker.volume_entries(runtime, spec.helper_image), manifest,
                        docker.volume_digest(runtime, spec.helper_image))
    except BaseException:
        docker.remove_volume(runtime)
        raise
    return manifest, "reuse", time.monotonic() - started


def _golden_after_seed(group: DeploymentGroup, rendered: dict, containers: dict[str, str],
                       compose_file: Path, docker: DockerClient, manifest: dict) -> None:
    spec = group.preparation.golden_volume
    result = docker.exec_json(containers[_target_service(group, spec.finalize.target)],
                              spec.finalize.command,
                              timeout_seconds=spec.finalize.timeout_seconds)
    validate_finalize_summary(result, manifest)
    docker.stop_container(containers["memory"])
    docker.stop_container(containers["vector"])
    _, runtime = _golden_volume_names(rendered)
    if docker.running_volume_containers(runtime):
        raise DeploymentFailed("Runtime volume is still in use")
    runtime_digest = docker.volume_digest(runtime, spec.helper_image)
    if docker.volume_exists(spec.name):
        raise DeploymentFailed("Golden volume appeared during baseline build")
    docker.create_volume(spec.name)
    if docker.volume_entries(spec.name, spec.helper_image):
        raise DeploymentFailed("Golden volume already contains data")
    docker.copy_volume(runtime, spec.name, spec.helper_image)
    if docker.volume_digest(spec.name, spec.helper_image) != runtime_digest:
        raise DeploymentFailed("Golden volume data digest mismatch after copy")
    docker.write_volume_manifest(spec.name, {**manifest, "data_sha256": runtime_digest}, spec.helper_image)
    classify_volume(docker.volume_entries(spec.name, spec.helper_image), manifest, runtime_digest)
    docker.compose_up(rendered["name"], compose_file, env=os.environ.copy())
    for service_name in ("vector", "memory"):
        if docker.compose_ps(rendered["name"], compose_file, service_name) != containers[service_name]:
            raise DeploymentFailed(f"{service_name} was replaced after Golden seal")
    readiness = group.preparation.readiness
    if docker.exec_json(containers[_target_service(group, readiness.target)],
                        readiness.command).get("ready") is not True:
        raise DeploymentFailed("baseline not ready after Golden seal")


def _target_service(group: DeploymentGroup, target: str) -> str:
    if target == "agent":
        return "agent_" + group.combination_ids[0].replace("-", "_")
    if target == "mock":
        return "mock_memory" if group.shared_memory_instance_id else "mock"
    return target


def deploy(config_path: Path, cwd: Path, docker: DockerClient,
           agent_items: list[str] | None = None, memory_items: list[str] | None = None) -> DeploymentState:
    config = apply_config_overrides(load_deployment(config_path), agent_items or [], memory_items or [])
    if config.pressure_tests:
        raise DeploymentFailed("pressure_tests need the separate multi-agent pressure implementation")
    groups = resolve_groups(config)
    _resources_preflight(groups, docker)
    for group in groups:
        planned = render_project(group, config.name, {})
        for service in _service_templates(group, planned).values():
            for var in [*service.environment_from.values(), *(service.client.environment_from.values() if service.client else ())]:
                if var not in os.environ:
                    raise DeploymentFailed(f"missing host environment variable: {var}")
            if service.image.archive:
                docker.load_image((group.source_root / service.image.archive).resolve(), service.image.name)
            # Compose may pull named images that are absent from the local cache.
    store = DeploymentStore(cwd)
    directory = store.create(config.name)
    state = DeploymentState(name=config.name, config_path=str(config.source_path), config_sha256=_hash(config.source_path))
    started: list[tuple[str, Path]] = []
    try:
        for group in groups:
            group_dir = directory / group.project_key
            group_dir.mkdir(mode=0o700)
            for service in _service_templates(group).values():
                for config_file in service.config_files.values():
                    source = (group.source_root / config_file.source).resolve()
                    state.source_hashes[str(source)] = _hash(source)
            mounts = prepare_instance_configs(group, group_dir / "configs")
            rendered = render_project(group, config.name, mounts, directory)
            for rendered_service in rendered["services"].values():
                for mount in rendered_service.get("volumes", []):
                    if mount["type"] == "bind" and Path(mount["source"]).is_relative_to(group_dir / "state"):
                        Path(mount["source"]).mkdir(parents=True, exist_ok=True, mode=0o700)
            compose_file = group_dir / "compose.yaml"
            compose_file.write_text(yaml.safe_dump(rendered, sort_keys=False))
            project = rendered["name"]
            state.projects[group.project_key] = project
            state.containers[group.project_key] = {}
            state.ports[group.project_key] = {}
            state.owned_volumes.extend(f"{project}_{name}" for name in rendered["volumes"])
            state.owned_directories.append(str(group_dir))
            for service_name, mount_list in mounts.items():
                for mount in mount_list:
                    state.config_hashes[mount["source"]] = _hash(Path(mount["source"]))
                instance = next((combo.id for combo in group.combinations
                                 if service_name == "agent_" + combo.id.replace("-", "_") or
                                 service_name.startswith("agent_" + combo.id.replace("-", "_") + "_")),
                                group.memory_instance_id)
                names = group.agent_config_overrides.get(instance, {}) if service_name.startswith("agent_") else group.memory_config_overrides
                state.config_origins[f"{group.project_key}/{service_name}"] = sorted(names)
            store.write_atomic(state)
            golden_manifest = None
            golden_mode = None
            clone_seconds = 0.0
            if group.preparation.golden_volume:
                golden_manifest, golden_mode, clone_seconds = _golden_before_deploy(group, rendered, docker)
            started.append((project, compose_file))
            docker.compose_up(project, compose_file, env=os.environ.copy())
            for service_name, template in _service_templates(group, rendered).items():
                cid = docker.compose_ps(project, compose_file, service_name)
                if not cid:
                    raise DeploymentFailed(f"missing container: {service_name}")
                inspect = docker.inspect(cid)
                state.containers[group.project_key][service_name] = cid
                state.ports[group.project_key][service_name] = _ports(inspect)
                expected_image = docker.inspect_image(template.image.name)["Id"]
                state.image_ids[cid] = expected_image
                if service_name.startswith(("agent_", "gateway_pool_")):
                    state.restart_counts[cid] = int(inspect.get("RestartCount", 0))
                    state.start_times[cid] = str(inspect.get("State", {}).get("StartedAt", ""))
                problems = _verify_service(docker, cid, template, rendered["services"][service_name], expected_image)
                if service_name == "vector" and group.preparation.golden_volume:
                    issue = _golden_mount_issue(inspect, rendered)
                    if issue:
                        problems.append(issue)
                if problems:
                    raise DeploymentFailed(f"{service_name}: {', '.join(problems)}")
            pool_problems = _pool_issues(group, state.ports[group.project_key])
            if pool_problems:
                raise DeploymentFailed("; ".join(pool_problems))
            prepared = _execute_preparation(
                group, state.containers[group.project_key], docker,
                golden_manifest=golden_manifest, skip_seed=golden_mode == "reuse")
            if golden_manifest is not None:
                if golden_mode == "build":
                    _golden_after_seed(group, rendered, state.containers[group.project_key],
                                       compose_file, docker, golden_manifest)
                    for service_name in ("vector", "memory"):
                        cid = state.containers[group.project_key][service_name]
                        state.ports[group.project_key][service_name] = _ports(docker.inspect(cid))
                prepared["golden_volume"] = {
                    "name": group.preparation.golden_volume.name,
                    "mode": "built" if golden_mode == "build" else "reused",
                    "runtime_volume": _golden_volume_names(rendered)[1],
                    "clone_seconds": round(clone_seconds, 3),
                }
            state.preparation[group.project_key] = prepared
            store.write_atomic(state)
        state.ready = True
        store.write_atomic(state)
        return state
    except BaseException as exc:
        rollback_errors = {}
        for project, file in reversed(started):
            try:
                docker.compose_down(project, file)
            except Exception as cleanup_exc:
                rollback_errors[project] = str(cleanup_exc)
        if rollback_errors:
            state.ready = False
            state.rollback_errors = rollback_errors
            try:
                store.write_atomic(state)
            except Exception as save_exc:
                raise DeploymentFailed(
                    f"deployment failed; rollback failed for {', '.join(rollback_errors)}; "
                    f"could not update retained state ({type(save_exc).__name__}). "
                    f"Retry with memory-bench undeploy --deployment {config.name}"
                ) from exc
            raise DeploymentFailed(
                f"deployment failed: {exc}; rollback failed for {', '.join(rollback_errors)}. "
                f"Retry with memory-bench undeploy --deployment {config.name}"
            ) from exc
        shutil.rmtree(directory)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise DeploymentFailed(str(exc)) from exc


def status(name: str, cwd: Path, docker: DockerClient) -> DeploymentStatus:
    store = DeploymentStore(cwd)
    state = store.read(name)
    drift = [f"rollback failed for {project}: {error}"
             for project, error in state.rollback_errors.items()]
    config_path = Path(state.config_path)
    if not config_path.is_file() or _hash(config_path) != state.config_sha256:
        drift.append("deployment YAML changed")
    for path, expected in state.config_hashes.items():
        if not Path(path).is_file() or _hash(Path(path)) != expected:
            drift.append(f"generated config changed: {Path(path).name}")
    for path, expected in state.source_hashes.items():
        if not Path(path).is_file() or _hash(Path(path)) != expected:
            drift.append(f"source config changed: {Path(path).name}")
    try:
        config = load_deployment(config_path)
        groups = {g.project_key: g for g in resolve_groups(config)}
        for group_key, project in state.projects.items():
            group = groups[group_key]
            rendered = yaml.safe_load((store.directory(name) / group_key / "compose.yaml").read_text())
            for service_name, template in _service_templates(group, rendered).items():
                cid = state.containers[group_key][service_name]
                if docker.compose_ps(project, store.directory(name) / group_key / "compose.yaml", service_name) != cid:
                    drift.append(f"container replaced: {service_name}")
                    continue
                drift.extend(f"{service_name}: {issue}" for issue in _verify_service(
                    docker, cid, template, rendered["services"][service_name], state.image_ids[cid]))
                if service_name == "vector" and group.preparation.golden_volume:
                    issue = _golden_mount_issue(docker.inspect(cid), rendered)
                    if issue:
                        drift.append(f"vector: {issue}")
                if _ports(docker.inspect(cid)) != state.ports[group_key][service_name]:
                    drift.append(f"published ports changed: {service_name}")
                if cid in state.restart_counts and int(docker.inspect(cid).get("RestartCount", 0)) != state.restart_counts[cid]:
                    drift.append(f"container restarted: {service_name}")
                if cid in state.start_times and docker.inspect(cid).get("State", {}).get("StartedAt", "") != state.start_times[cid]:
                    drift.append(f"container start changed: {service_name}")
            drift.extend(_pool_issues(group, state.ports[group_key]))
            if group.preparation.readiness.enabled:
                step = group.preparation.readiness
                result = docker.exec_json(state.containers[group_key][_target_service(group, step.target)], step.command)
                if result.get("ready") is not True:
                    drift.append(f"baseline not ready: {group_key}")
    except Exception as exc:
        drift.append(f"status inspection failed: {type(exc).__name__}")
    return DeploymentStatus(state.ready and not drift, tuple(drift), state)


def undeploy(name: str, cwd: Path, docker: DockerClient) -> None:
    store = DeploymentStore(cwd)
    store.acquire_lock(name)
    try:
        state = store.read(name)
        for group_key, project in state.projects.items():
            compose_file = store.directory(name) / group_key / "compose.yaml"
            for service_name, expected_id in state.containers[group_key].items():
                actual_id = docker.compose_ps(project, compose_file, service_name)
                if actual_id and (actual_id != expected_id or docker.inspect(actual_id).get("Id") != expected_id):
                    raise DeploymentFailed(f"resource ownership changed: {service_name}")
        for group_key, project in reversed(list(state.projects.items())):
            compose_file = store.directory(name) / group_key / "compose.yaml"
            docker.compose_down(project, compose_file)
        shutil.rmtree(store.directory(name))
    finally:
        store.release_lock(name)
