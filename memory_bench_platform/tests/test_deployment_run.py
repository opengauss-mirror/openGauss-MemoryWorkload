import json

import pytest
import yaml

from memory_bench_platform.deployment_lifecycle import deploy
from memory_bench_platform.deployment_run import run_deployment
from test_deployment_config import fixture_config
from test_deployment_lifecycle import RecordingDocker
from test_deployment_compose import pooled_fixture


def test_run_deployment_routes_each_combination_and_summarizes(tmp_path, monkeypatch):
    path, raw = fixture_config(tmp_path)
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    deploy(path, tmp_path, docker)
    monkeypatch.setenv("MEMORY_URL", "http://wrong.invalid")
    seen = []

    def invoke(argv, *, env, cwd):
        seen.append((argv, env.copy(), cwd))
        return 0

    rc = run_deployment("suite", tmp_path, None, docker, invoke)
    assert rc == 0
    assert len(seen) == 2
    assert seen[0][1]["MEMORY_URL"] == "http://127.0.0.1:28080"
    assert seen[0][1]["SERVICE_URL"] == "http://127.0.0.1:28080"
    assert "WORKER_URL" not in seen[0][1]
    assert seen[1][1]["WORKER_URL"] == "http://127.0.0.1:28080"
    assert "SERVICE_URL" not in seen[1][1]
    summaries = list((tmp_path / "runs").glob("suite-*/suite_summary.json"))
    assert len(summaries) == 1
    assert len(json.loads(summaries[0].read_text())["combinations"]) == 2


def test_deployment_run_forwards_profile_to_each_child(tmp_path):
    path, raw = fixture_config(tmp_path)
    path.write_text(yaml.safe_dump(raw))
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump({"schema": "profiling/1", "targets": ["memory"]}))
    docker = RecordingDocker()
    deploy(path, tmp_path, docker)
    seen = []
    run_deployment("suite", tmp_path, None, docker,
        lambda argv, *, env, cwd: seen.append((argv, env)) or 0, profiling_config=profile)
    assert len(seen) == 2
    assert all(argv[-2:] == ["--profiling-config", str(profile)] for argv, _ in seen)
    assert all(len(json.loads(env["MEMORY_BENCH_PROFILE_TARGETS"])) == 1 for _, env in seen)


def test_deployment_drift_blocks_profiled_child(tmp_path):
    path, raw = fixture_config(tmp_path)
    path.write_text(yaml.safe_dump(raw))
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump({"schema": "profiling/1", "targets": ["memory"]}))
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    container = next(iter(state.restart_counts))
    docker.containers[container]["RestartCount"] = 1
    seen = []
    with pytest.raises(ValueError, match="not ready"):
        run_deployment("suite", tmp_path, None, docker,
                       lambda argv, *, env, cwd: seen.append(argv) or 0,
                       profiling_config=profile)
    assert seen == []


def test_shared_second_agent_warmup_targets_its_own_container(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["combinations"][1]["preparation"] = {"warmup": {"enabled": True, "target": "agent", "command": ["warmup"]}}
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    deploy(path, tmp_path, docker)
    docker.exec_calls.clear()
    assert run_deployment("suite", tmp_path, "worker-common", docker,
                          lambda argv, *, env, cwd: 0) == 0
    warmup_calls = [container for container, command in docker.exec_calls if command == ["warmup"]]
    assert len(warmup_calls) == 1
    assert warmup_calls[0].endswith("agent_worker_common")


def test_pooled_run_uses_pool_url_and_detects_restart(tmp_path, monkeypatch):
    path, _ = pooled_fixture(tmp_path)
    upstreams = [f"http://agent_claw_common_{n}:8080" for n in range(1, 4)]
    monkeypatch.setattr("memory_bench_platform.deployment_lifecycle._pool_status",
                        lambda url: {"upstreams": upstreams, "assignments": {}, "request_counts": {}})
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    seen = []

    def invoke(argv, *, env, cwd):
        seen.append(env.copy())
        return 0

    assert run_deployment("suite", tmp_path, "claw-common", docker, invoke) == 0
    assert seen[0]["OPENCLAW_GATEWAY_URL"] == "http://127.0.0.1:49789"
    assert seen[0]["OPENCLAW_TRANSPORT"] == "http"
    assert seen[0]["OPENCLAW_POOL_ENABLED"] == "1"
    assert seen[0]["OPENCLAW_POOL_ROUTE_PREFIX"].endswith(":claw-common")

    cid = state.containers[next(iter(state.projects))]["gateway_pool_claw_common"]

    def restart(argv, *, env, cwd):
        docker.containers[cid]["RestartCount"] = 1
        return 0

    assert run_deployment("suite", tmp_path, "claw-common", docker, restart) == 1


def test_pooled_run_imports_cannot_override_mandatory_routing(tmp_path, monkeypatch):
    path, raw = pooled_fixture(tmp_path)
    raw["agents"]["claw"]["client"]["environment_from"] = {
        "OPENCLAW_POOL_ENABLED": "HOST_POOL_ENABLED", "OPENCLAW_TRANSPORT": "HOST_TRANSPORT",
        "OPENCLAW_GATEWAY_URL": "HOST_GATEWAY_URL"}
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("HOST_POOL_ENABLED", "0")
    monkeypatch.setenv("HOST_TRANSPORT", "cli")
    monkeypatch.setenv("HOST_GATEWAY_URL", "http://wrong.invalid")
    upstreams = [f"http://agent_claw_common_{n}:8080" for n in range(1, 4)]
    monkeypatch.setattr("memory_bench_platform.deployment_lifecycle._pool_status",
                        lambda url: {"upstreams": upstreams, "assignments": {}, "request_counts": {}})
    docker = RecordingDocker()
    deploy(path, tmp_path, docker)
    seen = []
    assert run_deployment("suite", tmp_path, "claw-common", docker,
                          lambda argv, *, env, cwd: seen.append(env.copy()) or 0) == 0
    assert seen[0]["OPENCLAW_POOL_ENABLED"] == "1"
    assert seen[0]["OPENCLAW_TRANSPORT"] == "http"
    assert seen[0]["OPENCLAW_GATEWAY_URL"] == "http://127.0.0.1:49789"
