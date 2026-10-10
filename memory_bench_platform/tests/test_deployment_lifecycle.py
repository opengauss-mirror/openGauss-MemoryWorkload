import json
from pathlib import Path

import pytest
import yaml

from memory_bench_platform.deployment_lifecycle import DeploymentFailed, deploy, status, undeploy
from test_deployment_config import fixture_config
from test_deployment_compose import pooled_fixture


class RecordingDocker:
    def __init__(self):
        self.projects = {}
        self.containers = {}
        self.specs = {}
        self.removed_projects = set()
        self.boosts = []
        self.exec_calls = []
        self.fail_seed = False

    def info(self):
        return {"NCPU": 8, "MemTotal": 8 * 1024**3}

    def inspect_image(self, name):
        return {"Id": "sha256:" + name}

    def load_image(self, archive, name):
        return "sha256:" + name

    def compose_up(self, project, file, env=None):
        compose = yaml.safe_load(Path(file).read_text())
        self.projects[project] = compose
        for name, spec in compose["services"].items():
            container_id = project + "_" + name
            ports = {str(p["target"]) + "/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(20000 + p["target"])}]
                     for p in spec.get("ports", [])}
            self.containers[container_id] = {
                "Id": container_id, "Image": "sha256:" + spec["image"],
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "NetworkSettings": {"Ports": ports},
                "Mounts": [{"Source": v["source"], "Destination": v["target"]}
                           for v in spec.get("volumes", []) if v["type"] == "bind"],
                "Config": {"Env": [k + "=" + str(v) for k, v in spec.get("environment", {}).items()]},
                "HostConfig": {
                    "Memory": int(str(spec.get("deploy", {}).get("resources", {}).get("limits", {}).get("memory", "0M")).rstrip("M")) * 1024**2,
                    "NanoCpus": round(float(spec.get("deploy", {}).get("resources", {}).get("limits", {}).get("cpus", 0)) * 1_000_000_000),
                },
            }
            self.specs[container_id] = spec

    def compose_down(self, project, file):
        self.removed_projects.add(project)
        self.projects.pop(project, None)

    def compose_ps(self, project, file, service):
        return project + "_" + service

    def inspect(self, container):
        return self.containers[container]

    def exec_json(self, container, command, input_data=None):
        self.exec_calls.append((container, command))
        if command == ["seed"] and self.fail_seed:
            raise RuntimeError("seed failed")
        if command == ["ready"]:
            return {"ready": True, "details": {}}
        spec = self.specs[container]
        result = {"version": "1", "endpoints": spec.get("environment", {}),
                  "reachable": {name: True for name in spec.get("depends_on", {})},
                  "effective_parameters": {}}
        for mount in self.containers[container]["Mounts"]:
            source = Path(mount["Source"])
            if source.suffix == ".json":
                result["effective_parameters"]["log_level"] = json.loads(source.read_text())["logging"]["level"]
            if source.suffix == ".yaml":
                result["effective_parameters"]["workers"] = yaml.safe_load(source.read_text())["server"]["workers"]
        return result

    def update_resources(self, container, **kwargs):
        self.boosts.append((container, kwargs))


def test_deploy_status_detects_config_drift_and_undeploy(tmp_path):
    path, raw = fixture_config(tmp_path)
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker, ['claw-common:log_level="debug"'], ['mem0-common:workers=4'])
    assert state.ready
    assert status("suite", tmp_path, docker).ready
    config_copy = next(Path(p) for p in state.config_hashes if p.endswith("memory/runtime.yaml"))
    config_copy.write_text("server:\n  workers: 9\n")
    assert not status("suite", tmp_path, docker).ready
    undeploy("suite", tmp_path, docker)
    assert docker.removed_projects == set(state.projects.values())


def test_source_config_change_is_reported_as_drift(tmp_path):
    path, _ = fixture_config(tmp_path)
    docker = RecordingDocker()
    deploy(path, tmp_path, docker)
    (tmp_path / "memory.yaml").write_text("server:\n  workers: 8\n")
    checked = status("suite", tmp_path, docker)
    assert not checked.ready
    assert any("source config changed" in item for item in checked.drift)


def test_undeploy_refuses_replaced_container(tmp_path):
    path, _ = fixture_config(tmp_path)
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    group_key = next(iter(state.projects))
    docker.containers[state.containers[group_key]["memory"]]["Id"] = "replacement"
    with pytest.raises(DeploymentFailed, match="ownership"):
        undeploy("suite", tmp_path, docker)
    assert not docker.removed_projects


def test_seed_failure_restores_resources_and_rolls_back(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["vectors"] = {"v": {"image": {"name": "example/vector:1"}, "container_port": 1000,
                            "healthcheck": ["CMD", "true"], "resources": {"memory_mb": 100}}}
    raw["shared_memory_instances"]["mem0-common"]["vector"] = "v"
    raw["memories"]["mem0"].setdefault("service_endpoints", {})["VECTOR_URI"] = "vector"
    raw["shared_memory_instances"]["mem0-common"]["preparation"] = {
        "seed": {"enabled": True, "target": "memory", "command": ["seed"]},
        "readiness": {"enabled": True, "target": "memory", "command": ["ready"]},
        "seed_resource_boost": {"memory_mb": 200},
    }
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    docker.fail_seed = True
    with pytest.raises(DeploymentFailed):
        deploy(path, tmp_path, docker)
    assert len(docker.boosts) == 2
    assert docker.boosts[0][1]["memory_mb"] == 200
    assert docker.boosts[1][1]["memory_mb"] == 100
    assert docker.removed_projects


def test_partial_compose_up_is_rolled_back(tmp_path):
    path, raw = fixture_config(tmp_path)
    path.write_text(yaml.safe_dump(raw))

    class PartialDocker(RecordingDocker):
        def compose_up(self, project, file, env=None):
            super().compose_up(project, file, env)
            raise RuntimeError("one service failed to start")

    docker = PartialDocker()
    with pytest.raises(DeploymentFailed):
        deploy(path, tmp_path, docker)
    assert len(docker.removed_projects) == 1
    assert next(iter(docker.removed_projects)).startswith("mb_suite_mem0_common_")


def test_pool_deploy_checks_upstreams_and_restart_drift(tmp_path, monkeypatch):
    path, _ = pooled_fixture(tmp_path)
    expected = [f"http://agent_claw_common_{n}:8080" for n in range(1, 4)]
    monkeypatch.setattr("memory_bench_platform.deployment_lifecycle._pool_status",
                        lambda url: {"upstreams": expected})
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    group = next(iter(state.projects))
    assert all(f"agent_claw_common_{n}" in state.containers[group] for n in range(1, 4))
    assert "gateway_pool_claw_common" in state.containers[group]
    assert status("suite", tmp_path, docker).ready
    monkeypatch.setattr("memory_bench_platform.deployment_lifecycle._pool_status",
                        lambda url: {"upstreams": expected[:-1]})
    assert not status("suite", tmp_path, docker).ready
    monkeypatch.setattr("memory_bench_platform.deployment_lifecycle._pool_status",
                        lambda url: {"upstreams": expected})
    cid = state.containers[group]["gateway_pool_claw_common"]
    docker.containers[cid]["RestartCount"] = 1
    assert not status("suite", tmp_path, docker).ready


def test_pool_wrong_upstreams_rolls_back(tmp_path, monkeypatch):
    path, _ = pooled_fixture(tmp_path)
    monkeypatch.setattr("memory_bench_platform.deployment_lifecycle._pool_status",
                        lambda url: {"upstreams": []})
    docker = RecordingDocker()
    with pytest.raises(DeploymentFailed, match="upstream"):
        deploy(path, tmp_path, docker)
    assert docker.removed_projects
