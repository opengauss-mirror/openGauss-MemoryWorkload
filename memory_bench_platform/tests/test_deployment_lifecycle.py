import json
from pathlib import Path

import pytest
import yaml

from memory_bench_platform.deployment_lifecycle import DeploymentFailed, deploy, status, undeploy
from memory_bench_platform.deployment_store import DeploymentStore
from test_deployment_config import fixture_config, golden_fixture
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
                "State": {"Running": True, "Health": {"Status": "healthy"},
                          "StartedAt": "2026-10-10T00:00:00Z"},
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

    def exec_json(self, container, command, input_data=None, **kwargs):
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


class GoldenRecordingDocker(RecordingDocker):
    def __init__(self):
        super().__init__()
        self.volumes = {}
        self.golden_events = []

    def compose_up(self, project, file, env=None):
        super().compose_up(project, file, env)
        for name in self.projects[project]["volumes"]:
            self.volumes.setdefault(f"{project}_{name}", {})
        for service_name, spec in self.projects[project]["services"].items():
            container = self.containers[f"{project}_{service_name}"]
            container["Mounts"] = [item for item in container["Mounts"] if "Name" not in item]
            for mount in spec.get("volumes", []):
                if mount["type"] == "volume":
                    container["Mounts"].append({
                        "Name": f"{project}_{mount['source']}",
                        "Destination": mount["target"],
                    })
        self.golden_events.append("compose_up")

    def compose_down(self, project, file):
        super().compose_down(project, file)
        for name in list(self.volumes):
            if name.startswith(project + "_"):
                del self.volumes[name]

    def exec_json(self, container, command, input_data=None, **kwargs):
        if command == ["seed"]:
            self.golden_events.append("seed")
            project = container.rsplit("_memory", 1)[0]
            self.volumes[f"{project}_vector_0"]["etcd"] = None
            return {"inserted": 20, "deleted_dynamic": 0, "deleted_seed": 0,
                    "results": [{"user_id": user, "validated_count": 10, "reused": False}
                                for user in ("user-001", "user-002")]}
        if command == ["compact"]:
            self.golden_events.append("compact")
            return {"status": "complete", "collections": [
                {"collection": "memory_001", "state": "Completed"}]}
        return super().exec_json(container, command, input_data)

    def volume_exists(self, name):
        return name in self.volumes

    def volume_entries(self, name, helper_image):
        return dict(self.volumes[name])

    def create_volume(self, name, *, project=None, volume_key=None):
        assert name not in self.volumes
        self.volumes[name] = {}

    def copy_volume(self, source, target, helper_image):
        assert not self.volumes[target]
        self.volumes[target] = dict(self.volumes[source])
        self.golden_events.append(f"copy:{source}->{target}")

    def write_volume_manifest(self, name, manifest, helper_image):
        from memory_bench_platform.deployment_golden import MARKER
        self.volumes[name][MARKER] = manifest
        self.golden_events.append("seal")

    def running_volume_containers(self, name):
        return []

    def attached_volume_containers(self, name):
        return []

    def stop_container(self, container):
        self.golden_events.append(f"stop:{container.rsplit('_', 1)[-1]}")

    def remove_volume(self, name):
        self.volumes.pop(name, None)


def test_golden_volume_build_then_reuse_skips_seed_and_keeps_source_unchanged(tmp_path):
    path, raw = golden_fixture(tmp_path)
    docker = GoldenRecordingDocker()
    first = deploy(path, tmp_path, docker)
    assert first.ready
    golden = dict(docker.volumes["milvus_locomo_golden"])
    assert first.preparation["mem0-common"]["golden_volume"]["mode"] == "built"
    assert docker.golden_events.index("seed") < docker.golden_events.index("compact")
    assert docker.golden_events.index("compact") < docker.golden_events.index("seal")

    raw["name"] = "suite2"
    raw["vectors"]["milvus"]["image"]["name"] = "example/milvus:2"
    path.write_text(yaml.safe_dump(raw))
    docker.golden_events.clear()
    second = deploy(path, tmp_path, docker)
    assert second.ready
    assert second.preparation["mem0-common"]["golden_volume"]["mode"] == "reused"
    assert "seed" not in docker.golden_events
    assert "compact" not in docker.golden_events
    assert any(event.startswith("copy:milvus_locomo_golden->") for event in docker.golden_events)
    assert docker.volumes["milvus_locomo_golden"] == golden


def test_golden_build_tracks_ports_reassigned_after_seal(tmp_path):
    path, _ = golden_fixture(tmp_path)

    class ReassignedPortsDocker(GoldenRecordingDocker):
        def compose_up(self, project, file, env=None):
            super().compose_up(project, file, env)
            if self.golden_events.count("compose_up") == 2:
                for name in ("memory", "vector"):
                    ports = self.containers[f"{project}_{name}"]["NetworkSettings"]["Ports"]
                    for bindings in ports.values():
                        bindings[0]["HostPort"] = str(int(bindings[0]["HostPort"]) + 1)

    docker = ReassignedPortsDocker()
    deployed = deploy(path, tmp_path, docker)

    assert deployed.ports["mem0-common"]["memory"]["8080/tcp"] == 28081
    assert status("suite", tmp_path, docker).ready


def test_golden_volume_rejects_changed_baseline_before_compose(tmp_path):
    path, raw = golden_fixture(tmp_path)
    docker = GoldenRecordingDocker()
    deploy(path, tmp_path, docker)
    raw["name"] = "suite2"
    raw["shared_memory_instances"]["mem0-common"]["preparation"]["golden_volume"]["users"] = ["other"]
    path.write_text(yaml.safe_dump(raw))
    before = len(docker.golden_events)
    with pytest.raises(DeploymentFailed, match="does not match"):
        deploy(path, tmp_path, docker)
    assert "compose_up" not in docker.golden_events[before:]


def test_golden_volume_rejects_incomplete_source_before_compose(tmp_path):
    path, _ = golden_fixture(tmp_path)
    docker = GoldenRecordingDocker()
    docker.volumes["milvus_locomo_golden"] = {"etcd": None}
    with pytest.raises(DeploymentFailed, match="incomplete"):
        deploy(path, tmp_path, docker)
    assert "compose_up" not in docker.golden_events


def test_golden_volume_rejects_attached_source_before_clone(tmp_path):
    path, raw = golden_fixture(tmp_path)
    docker = GoldenRecordingDocker()
    deploy(path, tmp_path, docker)
    raw["name"] = "suite2"
    path.write_text(yaml.safe_dump(raw))
    docker.attached_volume_containers = lambda name: ["old-container"]
    before = len(docker.golden_events)
    with pytest.raises(DeploymentFailed, match="attached"):
        deploy(path, tmp_path, docker)
    assert "compose_up" not in docker.golden_events[before:]


def test_golden_volume_preserves_preexisting_runtime_volume_on_preflight_failure(tmp_path):
    from memory_bench_platform.deployment_config import load_deployment, resolve_groups
    from memory_bench_platform.deployment_compose import render_project

    path, raw = golden_fixture(tmp_path)
    docker = GoldenRecordingDocker()
    deploy(path, tmp_path, docker)
    raw["name"] = "suite2"
    path.write_text(yaml.safe_dump(raw))
    group, = resolve_groups(load_deployment(path))
    project = render_project(group, "suite2", {}, tmp_path / ".memory-bench/deployments/suite2")["name"]
    runtime = f"{project}_vector_0"
    docker.volumes[runtime] = {"existing": None}
    with pytest.raises(DeploymentFailed, match="Runtime volume already exists"):
        deploy(path, tmp_path, docker)
    assert docker.volumes[runtime] == {"existing": None}


def test_golden_volume_clone_failure_removes_only_new_runtime_volume(tmp_path):
    path, raw = golden_fixture(tmp_path)

    class FailedCloneDocker(GoldenRecordingDocker):
        fail_copy = False

        def copy_volume(self, source, target, helper_image):
            if self.fail_copy:
                self.volumes[target]["partial"] = None
                raise RuntimeError("copy interrupted")
            super().copy_volume(source, target, helper_image)

    docker = FailedCloneDocker()
    deploy(path, tmp_path, docker)
    golden = dict(docker.volumes["milvus_locomo_golden"])
    raw["name"] = "suite2"
    path.write_text(yaml.safe_dump(raw))
    docker.fail_copy = True
    with pytest.raises(DeploymentFailed, match="copy interrupted"):
        deploy(path, tmp_path, docker)
    assert docker.volumes["milvus_locomo_golden"] == golden
    assert not any(name.startswith("mb_suite2_") for name in docker.volumes)


def test_golden_volume_does_not_seal_unvalidated_seed(tmp_path):
    path, _ = golden_fixture(tmp_path)

    class BadSeedDocker(GoldenRecordingDocker):
        def exec_json(self, container, command, input_data=None, **kwargs):
            result = super().exec_json(container, command, input_data, **kwargs)
            if command == ["seed"]:
                result["deleted_dynamic"] = 1
            return result

    docker = BadSeedDocker()
    with pytest.raises(DeploymentFailed, match="clean baseline"):
        deploy(path, tmp_path, docker)
    assert "milvus_locomo_golden" not in docker.volumes


def test_golden_volume_does_not_seal_incomplete_compaction(tmp_path):
    path, _ = golden_fixture(tmp_path)

    class BadCompactionDocker(GoldenRecordingDocker):
        def exec_json(self, container, command, input_data=None, **kwargs):
            result = super().exec_json(container, command, input_data, **kwargs)
            if command == ["compact"]:
                result["collections"] = []
            return result

    docker = BadCompactionDocker()
    with pytest.raises(DeploymentFailed, match="compaction"):
        deploy(path, tmp_path, docker)
    assert "milvus_locomo_golden" not in docker.volumes


def test_golden_volume_status_rejects_replaced_runtime_mount(tmp_path):
    path, _ = golden_fixture(tmp_path)
    docker = GoldenRecordingDocker()
    state = deploy(path, tmp_path, docker)
    assert status("suite", tmp_path, docker).ready
    vector_id = state.containers["mem0-common"]["vector"]
    docker.containers[vector_id]["Mounts"][-1]["Name"] = "different-volume"
    checked = status("suite", tmp_path, docker)
    assert not checked.ready
    assert any("Runtime volume mount changed" in item for item in checked.drift)


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


def test_false_readiness_receipt_does_not_mark_deployment_ready(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["shared_memory_instances"]["mem0-common"]["preparation"] = {
        "readiness": {"enabled": True, "target": "memory", "command": ["ready"],
                      "timeout_seconds": 1}}
    path.write_text(yaml.safe_dump(raw))

    class NotReadyDocker(RecordingDocker):
        def exec_json(self, container, command, input_data=None):
            if command == ["ready"]:
                self.exec_calls.append((container, command))
                return {"ready": False}
            return super().exec_json(container, command, input_data)

    docker = NotReadyDocker()
    with pytest.raises(DeploymentFailed, match="baseline readiness timed out"):
        deploy(path, tmp_path, docker)
    assert any(command == ["ready"] for _, command in docker.exec_calls)
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
    assert not (tmp_path / ".memory-bench/deployments/suite").exists()


def test_failed_rollback_keeps_manifest_for_safe_undeploy_retry(tmp_path):
    path, _ = fixture_config(tmp_path)

    class IntermittentDocker(RecordingDocker):
        down_available = False

        def compose_up(self, project, file, env=None):
            super().compose_up(project, file, env)
            raise RuntimeError("one service failed to start")

        def compose_down(self, project, file):
            if not self.down_available:
                raise RuntimeError("Docker daemon unavailable")
            super().compose_down(project, file)

    docker = IntermittentDocker()
    with pytest.raises(DeploymentFailed, match="rollback failed"):
        deploy(path, tmp_path, docker)
    directory = tmp_path / ".memory-bench/deployments/suite"
    assert (directory / "state.json").is_file()
    assert (directory / "mem0-common/compose.yaml").is_file()
    state = DeploymentStore(tmp_path).read("suite")
    assert not state.ready
    assert state.rollback_errors
    assert set(state.projects) == {"mem0-common"}
    checked = status("suite", tmp_path, docker)
    assert not checked.ready
    assert any("rollback failed" in item for item in checked.drift)
    with pytest.raises(FileExistsError):
        deploy(path, tmp_path, docker)
    docker.down_available = True
    undeploy("suite", tmp_path, docker)
    assert not directory.exists()
    assert docker.removed_projects == set(state.projects.values())


def test_failed_rollback_preserves_all_project_ownership(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw.pop("shared_memory_instances")
    for combo in raw["combinations"]:
        combo.pop("memory_instance")
        combo["memory"] = "mem0"
    path.write_text(yaml.safe_dump(raw))

    class PartialDocker(RecordingDocker):
        up_count = 0
        fail_down = True

        def compose_up(self, project, file, env=None):
            super().compose_up(project, file, env)
            self.up_count += 1
            if self.up_count == 2:
                raise RuntimeError("second project failed")

        def compose_down(self, project, file):
            if self.fail_down and "worker_common" in project:
                raise RuntimeError("Docker temporarily unavailable")
            super().compose_down(project, file)

        def compose_ps(self, project, file, service):
            if project in self.removed_projects:
                return ""
            return super().compose_ps(project, file, service)

    docker = PartialDocker()
    with pytest.raises(DeploymentFailed, match="rollback failed"):
        deploy(path, tmp_path, docker)
    state = DeploymentStore(tmp_path).read("suite")
    assert len(state.projects) == 2
    assert len(docker.removed_projects) == 1
    assert set(state.rollback_errors) == set(state.projects.values()) - docker.removed_projects
    docker.fail_down = False
    undeploy("suite", tmp_path, docker)
    assert docker.removed_projects == set(state.projects.values())


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
    docker.containers[cid]["RestartCount"] = 0
    docker.containers[cid]["State"]["StartedAt"] = "2026-10-10T01:00:00Z"
    assert not status("suite", tmp_path, docker).ready


def test_pool_wrong_upstreams_rolls_back(tmp_path, monkeypatch):
    path, _ = pooled_fixture(tmp_path)
    monkeypatch.setattr("memory_bench_platform.deployment_lifecycle._pool_status",
                        lambda url: {"upstreams": []})
    docker = RecordingDocker()
    with pytest.raises(DeploymentFailed, match="upstream"):
        deploy(path, tmp_path, docker)
    assert docker.removed_projects
