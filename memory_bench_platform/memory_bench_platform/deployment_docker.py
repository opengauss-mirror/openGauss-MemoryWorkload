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
             env: dict[str, str] | None = None) -> str:
        result = self.command(args, input=input_text, text=True, capture_output=True,
                              check=False, timeout=300, env=env)
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

    def exec_json(self, container: str, command: list[str], input_data: object | None = None) -> dict:
        raw = self._run(["docker", "exec", "-i", container, *command],
                        input_text=json.dumps(input_data) if input_data is not None else None)
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
