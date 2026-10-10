"""Atomic, owned-resource deployment state and local operation lock."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class DeploymentLocked(RuntimeError):
    pass


class DeploymentState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    config_path: str
    config_sha256: str
    ready: bool = False
    projects: dict[str, str] = Field(default_factory=dict)
    containers: dict[str, dict[str, str]] = Field(default_factory=dict)
    image_ids: dict[str, str] = Field(default_factory=dict)
    restart_counts: dict[str, int] = Field(default_factory=dict)
    start_times: dict[str, str] = Field(default_factory=dict)
    ports: dict[str, dict[str, dict[str, int]]] = Field(default_factory=dict)
    config_hashes: dict[str, str] = Field(default_factory=dict)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    config_origins: dict[str, list[str]] = Field(default_factory=dict)
    owned_volumes: list[str] = Field(default_factory=list)
    owned_directories: list[str] = Field(default_factory=list)
    preparation: dict[str, dict] = Field(default_factory=dict)


class DeploymentStore:
    def __init__(self, root: Path):
        self.root = root / ".memory-bench" / "deployments"
        self._locks: dict[str, object] = {}

    def directory(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", name):
            raise ValueError("invalid deployment name")
        return self.root / name

    def create(self, name: str) -> Path:
        directory = self.directory(name)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        directory.chmod(0o700)
        return directory

    def read(self, name: str) -> DeploymentState:
        return DeploymentState.model_validate_json((self.directory(name) / "state.json").read_text())

    def write_atomic(self, state: DeploymentState) -> None:
        directory = self.directory(state.name)
        descriptor, temp_name = tempfile.mkstemp(prefix=".state-", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(state.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, directory / "state.json")
        finally:
            Path(temp_name).unlink(missing_ok=True)

    def acquire_lock(self, name: str) -> None:
        if name in self._locks:
            raise DeploymentLocked(name)
        self.root.mkdir(parents=True, exist_ok=True)
        handle = (self.root / f".{name}.lock").open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise DeploymentLocked(name) from exc
        self._locks[name] = handle

    def release_lock(self, name: str) -> None:
        handle = self._locks.pop(name, None)
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
