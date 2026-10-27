import json
import subprocess

import pytest

from memory_bench_platform.deployment_docker import DockerClient


def test_docker_adapter_uses_argv_and_parses_json(tmp_path):
    calls = []

    def recorder(args, **kwargs):
        calls.append(list(args))
        if args[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, '[{"Id":"sha256:abc"}]', "")
        if args[1] == "exec":
            return subprocess.CompletedProcess(args, 0, '{"ready":true}', "")
        return subprocess.CompletedProcess(args, 0, "", "")

    docker = DockerClient(recorder)
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"test")
    assert docker.load_image(archive, "example/app:1") == "sha256:abc"
    assert docker.exec_json("container", ["check"])["ready"] is True
    docker.compose_up("project", tmp_path / "compose.yaml")
    assert ["docker", "compose", "-p", "project", "-f", str(tmp_path / "compose.yaml"), "up", "-d", "--wait"] in calls


def test_exec_rejects_invalid_json():
    docker = DockerClient(lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "invalid", ""))
    with pytest.raises(ValueError):
        docker.exec_json("container", ["check"])


def test_golden_volume_docker_copy_mounts_source_read_only_and_writes_manifest_via_stdin():
    calls = []

    def recorder(args, **kwargs):
        calls.append((args, kwargs.get("input"), kwargs.get("timeout")))
        stdout = '{"etcd":null}' if args[1:3] == ["run", "--rm"] and "/data:ro" in " ".join(args) else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    docker = DockerClient(recorder)
    assert docker.volume_entries("golden", "python:3.11-alpine") == {"etcd": None}
    docker.copy_volume("golden", "runtime", "python:3.11-alpine")
    docker.write_volume_manifest("golden", {"users": ["user-001"]}, "python:3.11-alpine")
    copy_args, _, copy_timeout = calls[1]
    assert "golden:/source:ro" in copy_args
    assert "runtime:/target" in copy_args
    assert "runtime:/target:ro" not in copy_args
    assert copy_timeout == 3600
    assert json.loads(calls[2][1]) == {"users": ["user-001"]}


def test_golden_finalize_docker_exec_honors_compaction_timeout():
    calls = []

    def recorder(args, **kwargs):
        calls.append((args, kwargs["timeout"]))
        return subprocess.CompletedProcess(args, 0, '{"status":"complete"}', "")

    DockerClient(recorder).exec_json("milvus", ["compact"], timeout_seconds=3600)
    assert calls == [(["docker", "exec", "-i", "milvus", "compact"], 3600)]


def test_volume_digest_mounts_data_and_helper_read_only():
    calls = []

    def recorder(args, **kwargs):
        calls.append((args, kwargs["timeout"]))
        return subprocess.CompletedProcess(args, 0, "a" * 64 + "\n", "")

    assert DockerClient(recorder).volume_digest("golden", "python:3.11-alpine") == "a" * 64
    args, timeout = calls[0]
    assert "golden:/data:ro" in args
    assert any(value.endswith("/deployment_volume_digest.py:/opt/volume_digest.py:ro") for value in args)
    assert args[-3:] == ["/opt/volume_digest.py", "/data", ".memory-bench-golden.json"]
    assert timeout == 3600
