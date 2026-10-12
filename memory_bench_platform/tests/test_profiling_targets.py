from memory_bench_platform.deployment_store import DeploymentState
from memory_bench_platform.profiling_config import ProfilingConfig
from memory_bench_platform.profiling_targets import TargetIdentity, probe_target, resolve_targets


def _config(**kwargs):
    return ProfilingConfig.model_validate({"schema": "profiling/1", "targets": ["memory"], **kwargs})


def test_shared_memory_resolves_once():
    state = DeploymentState(name="demo", config_path="config.yaml", config_sha256="x",
        containers={"group": {"memory": "cid", "agent_a": "a", "agent_b": "b"}},
        restart_counts={"cid": 0}, start_times={"cid": "t"}, image_ids={"memory": "sha256:one"})
    result = resolve_targets(_config(), state, [])
    assert len(result) == 1
    assert result[0].container_id == "cid"
    assert result[0].physical_instance_id == "cid"


def test_external_target_without_verified_pid_or_cgroup_skips(monkeypatch):
    target = TargetIdentity(target_id="memory-i", role="memory", physical_instance_id="i",
                            deployment_kind="external")
    monkeypatch.setattr("memory_bench_platform.profiling_targets.platform.system", lambda: "Linux")
    result = probe_target(target, _config(), None)
    assert result.state == "skipped"
    assert result.reason == "missing_target_identity"


def test_container_restart_drift_is_detected(monkeypatch):
    target = TargetIdentity(target_id="memory-i", role="memory", physical_instance_id="i",
        deployment_kind="compose", container_id="cid", restart_count=0, start_time="before")
    class Docker:
        def inspect(self, cid):
            return {"Id": cid, "RestartCount": 1, "State": {"StartedAt": "after"}}
    monkeypatch.setattr("memory_bench_platform.profiling_targets.platform.system", lambda: "Linux")
    result = probe_target(target, _config(), Docker())
    assert result.state == "invalid" and result.reason == "target_drift"


def test_non_linux_probe_is_unsupported_platform(monkeypatch):
    target = TargetIdentity(target_id="memory-i", role="memory", physical_instance_id="i",
                            deployment_kind="external", cgroup_path="/sys/fs/cgroup/test")
    monkeypatch.setattr("memory_bench_platform.profiling_targets.platform.system", lambda: "Darwin")
    result = probe_target(target, _config(), None)
    assert result.state == "skipped" and result.reason == "unsupported_platform"


def test_deployed_target_records_live_cgroup_identity(monkeypatch):
    state = DeploymentState(name="demo", config_path="config.yaml", config_sha256="x",
        containers={"group": {"memory": "cid"}}, restart_counts={"cid": 0},
        start_times={"cid": "before"})
    class Docker:
        def inspect(self, cid):
            return {"Id": cid, "Image": "sha256:image", "RestartCount": 0,
                    "State": {"StartedAt": "before", "Pid": 42},
                    "HostConfig": {"CpusetCpus": "0-3"}}
    monkeypatch.setattr("memory_bench_platform.profiling_targets._cgroup_for_pid",
                        lambda pid: "/sys/fs/cgroup/docker/cid")
    target = resolve_targets(_config(), state, [], docker=Docker())[0]
    assert target.cgroup_path == "/sys/fs/cgroup/docker/cid"
    assert target.host_pid == 42 and target.cpu_scope == "0-3"
    assert target.image_digest == "sha256:image"


def test_external_pid_must_belong_to_declared_cgroup(monkeypatch):
    target = TargetIdentity("memory-i", "memory", "i", "external", host_pid=42,
                            cgroup_path="/sys/fs/cgroup/declared")
    monkeypatch.setattr("memory_bench_platform.profiling_targets.platform.system", lambda: "Linux")
    monkeypatch.setattr("memory_bench_platform.profiling_targets._cgroup_for_pid",
                        lambda pid: "/sys/fs/cgroup/actual")
    result = probe_target(target, _config(), None)
    assert result.state == "invalid" and result.reason == "target_identity_mismatch"
