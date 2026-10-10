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

