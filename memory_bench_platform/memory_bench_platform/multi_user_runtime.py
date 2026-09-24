"""Per-instance lifecycle. Never mutate process-wide environment or stop external services."""
from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import integration
from .multi_user_config import Instance, User, identity
from .protocol import MemoryPluginTaskInput, WorkflowRuntimeContext


class InstanceRuntime:
    def __init__(self, name: str, config: Instance, environment: dict[str, str],
                 run_id: str, output_dir: Path, users: list[User]):
        self.name, self.config, self.run_id = name, config, run_id
        self.namespace = identity(run_id, f"namespace:{name}")
        self.environment = {**environment, "TRACE_RUN_ID": run_id}
        if config.kind == "openclaw":
            self.environment["OPENCLAW_TRANSPORT"] = "http"
        self.users = users
        self.process = None
        self.prepared = False
        self.scratch = None
        self.output_dir = output_dir
        self.state = "pending"
        self.cleanup_failed = False

    def command(self, argv, timeout, payload=None, capture=False):
        process = subprocess.Popen(argv, cwd=self.config.cwd,
            env={**os.environ, **self.environment},
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            try:
                output, _ = process.communicate(
                    input=json.dumps(payload) if payload is not None else None, timeout=timeout)
            except BaseException:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                try: process.communicate()
                except BaseException: pass
                raise
            return subprocess.CompletedProcess(argv, process.returncode, output)
        finally:
            # A check command may return while a child remains in its group.
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass

    def start(self):
        if self.config.deployment == "managed":
            self.process = subprocess.Popen(self.config.start_command, cwd=self.config.cwd,
                                            env={**os.environ, **self.environment},
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("instance health timeout")
            if self.process and self.process.poll() is not None:
                raise RuntimeError("managed instance exited")
            try:
                if self.command(self.config.healthcheck_command, min(5, remaining)).returncode == 0:
                    break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        # Both plugin snapshots and adapter artifacts can contain private data.
        self.scratch = Path(tempfile.mkdtemp(prefix="memory-bench-instance-"))
        if self.config.kind == "openclaw":
            self.prepared = True  # finalize even when prepare failed partway through
            self.plugin("prepare", {"namespace": self.namespace})
            self.probe_isolation()
        self.state = "ready"

    def plugin(self, action, inputs=None, timeout=120):
        inputs = inputs or {}
        if action == "set_phase":
            operation_id = inputs.get("phase", "")
        elif action == "flush":
            operation_id = inputs.get("session_key") or inputs.get("session_id", "")
        elif action == "wait_settle":
            operation = inputs.get("operation", {})
            operation_id = operation.get("task_id") or operation.get("id") or operation.get("session_id", "")
        else:
            operation_id = ""
        key = identity(self.run_id, f"{self.name}:{action}:{operation_id}")
        context = WorkflowRuntimeContext(run_id=self.run_id,
            run_dir=str(self.scratch or self.output_dir / self.name),
            benchmark_id="production-http-replay", agent_id="openclaw",
            memory_integration="agent_plugin", memory_plugin_id=self.config.plugin_id)
        request = MemoryPluginTaskInput(task_id=f"{self.name}:{action}:{key}", action=action,
            inputs=inputs, runtime_context=context, idempotency_key=key)
        result = integration.run_memory_plugin_task(self.config.plugin_id, request,
            environment=self.environment, timeout_seconds=timeout)
        if result.status != "ok" or result.state != "completed":
            raise RuntimeError("plugin lifecycle failed")
        return result

    def probe_isolation(self):
        payload = {"schema": "multi-user-isolation-probe/1", "run_id": self.run_id,
                   "instance_id": self.name, "namespace": self.namespace,
                   "users": [{"user_id": identity(self.run_id, u.id), "agent_id": u.agent_id}
                             for u in self.users]}
        result = self.command(self.config.isolation_probe_command,
                              self.config.startup_timeout_seconds, payload, capture=True)
        try:
            evidence = json.loads(result.stdout)
            valid = (result.returncode == 0 and evidence.get("isolated") is True
                     and evidence.get("identity_mode") == "agent"
                     and evidence.get("run_id") == self.run_id
                     and sorted(evidence.get("agent_ids", [])) == sorted(u.agent_id for u in self.users)
                     and evidence.get("write_scope_verified") is True
                     and evidence.get("read_scope_verified") is True)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("OpenClaw memory isolation probe failed")

    def set_phase(self, phase):
        if self.config.kind == "openclaw":
            self.plugin("set_phase", {"phase": phase})

    def stop(self):
        try:
            if self.prepared:
                try:
                    self.plugin("finalize")
                except Exception:
                    self.cleanup_failed = True
        finally:
            if self.process:
                # The parent can exit while its children remain: signal the owned group.
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=2)
            if self.scratch and not self.cleanup_failed:
                shutil.rmtree(self.scratch)
