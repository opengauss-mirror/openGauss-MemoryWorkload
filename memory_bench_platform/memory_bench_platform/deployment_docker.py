"""Small injectable Docker CLI boundary for deployment operations."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable


class DockerClient:
    def __init__(self, command: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.command = command

    def _run(self, args: list[str], *, input_text: str | None = None,
             env: dict[str, str] | None = None, timeout: int = 300) -> str:
        result = self.command(args, input=input_text, text=True, capture_output=True,
                              check=False, timeout=timeout, env=env)
        if result.returncode:
            raise RuntimeError(f"Docker command failed ({result.returncode}): {' '.join(args[:5])}: {result.stderr.strip()}")
        return result.stdout

    def load_image(self, archive: Path, name: str) -> str:
        self._run(["docker", "load", "-i", str(archive)])
        return self.inspect_image(name)["Id"]

    def inspect_image(self, name: str) -> dict:
        return json.loads(self._run(["docker", "image", "inspect", name]))[0]

    def info(self) -> dict:
        return json.loads(self._run(["docker", "info", "--format", "{{json .}}"] ))

    def compose_up(self, project: str, file: Path, env: dict[str, str] | None = None) -> None:
        self._run(["docker", "compose", "-p", project, "-f", str(file), "up", "-d", "--wait"], env=env)

    def compose_down(self, project: str, file: Path) -> None:
        self._run(["docker", "compose", "-p", project, "-f", str(file), "down", "-v", "--remove-orphans"])

    def compose_ps(self, project: str, file: Path, service: str) -> str:
        return self._run(["docker", "compose", "-p", project, "-f", str(file), "ps", "-q", service]).strip()

    def inspect(self, container: str) -> dict:
        return json.loads(self._run(["docker", "inspect", container]))[0]

    def exec_json(self, container: str, command: list[str], input_data: object | None = None,
                  *, timeout_seconds: int = 300) -> dict:
        raw = self._run(["docker", "exec", "-i", container, *command],
                        input_text=json.dumps(input_data) if input_data is not None else None,
                        timeout=timeout_seconds)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("container verification did not return JSON") from exc
        if not isinstance(result, dict):
            raise ValueError("container verification did not return an object")
        return result

    def update_resources(self, container: str, *, cpus: float | None = None,
                         memory_mb: int | None = None) -> None:
        args = ["docker", "update"]
        if cpus is not None:
            args += ["--cpus", str(cpus)]
        if memory_mb is not None:
            args += ["--memory", f"{memory_mb}m"]
        self._run([*args, container])

    def volume_exists(self, name: str) -> bool:
        names = self._run(["docker", "volume", "ls", "-q", "--filter", f"name=^{name}$"])
        return name in names.splitlines()

    def volume_entries(self, name: str, helper_image: str) -> dict:
        script = (
            "import json,pathlib; p=pathlib.Path('/data'); "
            "m=p/'.memory-bench-golden.json'; "
            "print(json.dumps({x.name:json.loads(x.read_text()) if x==m else None "
            "for x in p.iterdir()}))"
        )
        return json.loads(self._run(["docker", "run", "--rm", "-v", f"{name}:/data:ro",
                                     "--entrypoint", "python3", helper_image, "-c", script]))

    def create_volume(self, name: str, *, project: str | None = None,
                      volume_key: str | None = None) -> None:
        args = ["docker", "volume", "create"]
        if project and volume_key:
            args += ["--label", f"com.docker.compose.project={project}",
                     "--label", f"com.docker.compose.volume={volume_key}"]
        self._run([*args, name])

    def running_volume_containers(self, name: str) -> list[str]:
        return self._run(["docker", "ps", "-q", "--filter", f"volume={name}"]).splitlines()

    def attached_volume_containers(self, name: str) -> list[str]:
        return self._run(["docker", "ps", "-aq", "--filter", f"volume={name}"]).splitlines()

    def copy_volume(self, source: str, target: str, helper_image: str) -> None:
        script = (
            "set -eu; "
            "need=$(du -sk /source | cut -f1); "
            "free=$(df -Pk /target | tail -1 | awk '{print $4}'); "
            "test \"$free\" -ge \"$((need * 11 / 10))\" || "
            "{ echo 'not enough disk space to clone Golden volume' >&2; exit 1; }; "
            "cp -a /source/. /target/; sync"
        )
        self._run(["docker", "run", "--rm", "-v", f"{source}:/source:ro",
                   "-v", f"{target}:/target", "--entrypoint", "sh", helper_image, "-c", script],
                  timeout=3600)

    def write_volume_manifest(self, name: str, manifest: dict,
                              helper_image: str) -> None:
        script = ("import json,pathlib,sys; "
                  "pathlib.Path('/data/.memory-bench-golden.json').write_text("
                  "json.dumps(json.load(sys.stdin),sort_keys=True)+'\\n')")
        self._run(["docker", "run", "--rm", "-i", "-v", f"{name}:/data",
                   "--entrypoint", "python3", helper_image, "-c", script],
                  input_text=json.dumps(manifest))

    def stop_container(self, container: str) -> None:
        self._run(["docker", "stop", "-t", "120", container])

    def remove_volume(self, name: str) -> None:
        self._run(["docker", "volume", "rm", name])
