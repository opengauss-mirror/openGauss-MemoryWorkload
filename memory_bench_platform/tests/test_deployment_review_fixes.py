from pathlib import Path

import pytest
import yaml

from memory_bench_platform.deployment_config import load_deployment, resolve_groups
from memory_bench_platform.deployment_compose import render_project
from memory_bench_platform.deployment_lifecycle import deploy, status
from test_deployment_config import fixture_config
from test_deployment_lifecycle import RecordingDocker


def test_independent_projects_have_distinct_host_visible_state_and_project_names(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw.pop("shared_memory_instances")
    for combo in raw["combinations"]:
        combo.pop("memory_instance")
        combo["memory"] = "mem0"
    raw["memories"]["mem0"]["state_mounts"] = [{"target": "/app/state", "host_visible": True}]
    path.write_text(yaml.safe_dump(raw))
    groups = resolve_groups(load_deployment(path))
    deployment_root = tmp_path / "deployment"
    projects = [render_project(group, "suite", {}, deployment_root) for group in groups]
    first = next(v["source"] for v in projects[0]["services"]["memory"]["volumes"] if v["target"] == "/app/state")
    second = next(v["source"] for v in projects[1]["services"]["memory"]["volumes"] if v["target"] == "/app/state")
    assert first != second
    assert projects[0]["name"] != projects[1]["name"]
    other = render_project(groups[0], "suite", {}, tmp_path / "other-deployment")
    assert other["name"] != projects[0]["name"]


@pytest.mark.parametrize("field,value,expected", [
    ("service_endpoints", {"MEMORY_URL": "memroy"}, "memory"),
    ("service_endpoints", {"VECTOR_URL": "vector", "MEMORY_URL": "memory"}, "vector"),
    ("client", {"endpoint_env": "MEMORY_URL"}, "endpoint_env"),
])
def test_invalid_agent_routing_rejected_before_docker(tmp_path, field, value, expected):
    path, raw = fixture_config(tmp_path)
    raw["agents"]["claw"][field] = value
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match=expected):
        load_deployment(path)


def test_invalid_preparation_target_rejected_before_docker(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["shared_memory_instances"]["mem0-common"]["preparation"] = {
        "readiness": {"enabled": True, "target": "missing", "command": ["ready"]}}
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="target"):
        load_deployment(path)


def test_agent_storage_access_joins_network_and_dependency(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["vectors"] = {"v": {"image": {"name": "example/vector:1"}, "container_port": 1000,
                            "healthcheck": ["CMD", "true"]}}
    raw["shared_memory_instances"]["mem0-common"]["vector"] = "v"
    raw["memories"]["mem0"].setdefault("service_endpoints", {})["VECTOR_URI"] = "vector"
    raw["agents"]["claw"]["allow_agent_storage_access"] = True
    raw["agents"]["claw"]["service_endpoints"]["VECTOR_URI"] = "vector"
    path.write_text(yaml.safe_dump(raw))
    group = resolve_groups(load_deployment(path))[0]
    agent = render_project(group, "suite", {})["services"]["agent_claw_common"]
    assert "storage_net" in agent["networks"]
    assert "vector" in agent["depends_on"]


def test_explicit_mock_selection_creates_service_without_endpoint_mapping(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["mocks"] = {"model": {"image": {"name": "example/mock:1"},
                               "healthcheck": ["CMD", "true"],
                               "endpoints": {"chat": {"container_port": 18094, "model_mode": "mock-fixed"}}}}
    raw["shared_memory_instances"]["mem0-common"]["mock"] = "model"
    path.write_text(yaml.safe_dump(raw))
    group = resolve_groups(load_deployment(path))[0]
    assert "mock_memory" in render_project(group, "suite", {})["services"]


def test_named_image_can_be_pulled_during_compose_up(tmp_path):
    path, _ = fixture_config(tmp_path)

    class PullingDocker(RecordingDocker):
        def inspect_image(self, name):
            if not self.projects:
                raise RuntimeError("image not cached")
            return super().inspect_image(name)

    state = deploy(path, tmp_path, PullingDocker())
    assert state.ready


def test_live_port_and_resource_changes_are_drift(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["memories"]["mem0"]["resources"] = {"memory_mb": 1024, "cpus": 1.5}
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    group_key = next(iter(state.projects))
    memory = docker.containers[state.containers[group_key]["memory"]]
    memory.setdefault("HostConfig", {})["Memory"] = 128 * 1024**2
    assert not status("suite", tmp_path, docker).ready
    memory["HostConfig"]["Memory"] = 1024 * 1024**2
    memory["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"] = "22222"
    assert not status("suite", tmp_path, docker).ready
