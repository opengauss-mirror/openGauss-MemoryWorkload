"""Bounded, per-user sequential replay across independently managed instances."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import integration
from .multi_user_config import MultiUserConfig, identity, preflight, replay_module, validate_agent_request
from .multi_user_report import RequestEvent, summarize, write_reports
from .multi_user_runtime import InstanceRuntime
from .protocol import RenderedTaskInput
from .profiling_config import ProfilingConfig
from .profiling_perf import ProfilerSession
from .profiling_phase import PhaseSink
from .profiling_targets import TargetIdentity, resolve_targets
from .profiling_trace_import import TraceRunScope, _stage_map, capture_trace_cursor, import_trace


def _snapshot(config, run_id):
    workloads = {}
    for name, workload in config.workloads.items():
        files = {}
        for operation in ("add", "search"):
            digest = hashlib.sha256()
            size = 0
            with getattr(workload, operation + "_file").open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
                    size += len(chunk)
            files[operation] = {"sha256": digest.hexdigest(), "size_bytes": size}
        workloads[name] = {"format": workload.format, "files": files}
    instances = {}
    active_instances = {u.instance for u in config.users}
    for name, instance in config.instances.items():
        skill = None
        if name in active_instances:
            skill = (integration.get_memory_manifest(instance.memory_skill) if instance.kind == "memory"
                     else integration.get_memory_plugin_manifest(instance.plugin_id))
        instances[name] = {"kind": instance.kind, "deployment": instance.deployment,
            "max_inflight": instance.max_inflight, "skill_version": getattr(skill, "version", None)}
        if instance.kind == "memory":
            instances[name]["memory_skill"] = instance.memory_skill
        else:
            agent = integration.get_agent_manifest("openclaw") if name in active_instances else None
            instances[name].update(plugin_id=instance.plugin_id, agent_skill="openclaw",
                                   agent_version=getattr(agent, "version", None))
    return {"schema": "multi-user-run/1", "run_id": run_id, "mode": config.mode,
        "model_mode": config.model_mode, "started_at": datetime.now(timezone.utc).isoformat(),
        "max_workers": config.max_workers, "workloads": workloads,
        "instances": instances,
        "users": {u.id: {"user_id": identity(run_id, u.id), "instance": u.instance,
                         "workload": u.workload, "controls": config.controls_for(u).model_dump()}
                  for u in config.users}}


def _snapshot_workloads(config: MultiUserConfig, directory: Path) -> MultiUserConfig:
    frozen = config.model_copy(deep=True)
    copies = {}
    for workload in frozen.workloads.values():
        for operation in ("add", "search"):
            field = operation + "_file"
            source = getattr(workload, field)
            if source not in copies:
                target = directory / f"{len(copies)}.jsonl"
                with source.open("rb") as original, target.open("xb") as snapshot:
                    shutil.copyfileobj(original, snapshot)
                copies[source] = target
            target = copies[source]
            count = 0
            for record in replay_module().iter_replay_records(target, operation, "preflight"):
                count += 1
                if frozen.mode == "openclaw":
                    validate_agent_request(record.request, operation)
            if not count:
                raise ValueError("workload files must not be empty")
            target.chmod(0o400)
            setattr(workload, field, target)
    return frozen


class Replay:
    def __init__(self, config, output_dir, run_id, environments):
        self.config, self.output_dir, self.run_id = config, output_dir, run_id
        self.origin = time.monotonic()
        self.origin_utc = datetime.now(timezone.utc)
        self.runtimes = {name: InstanceRuntime(name, config.instances[name], env, run_id, output_dir,
                         [u for u in config.users if u.instance == name]) for name, env in environments.items()}
        self.setup_ms = {}
        self.sessions = {u.id: {} for u in config.users}
        self.events = []
        self.windows = {}
        self.interrupted = False
        self.profile_invalid = False

    def records(self, user, operation):
        uid = identity(self.run_id, user.id)
        file = getattr(self.config.workloads[user.workload], operation + "_file")
        offset = 0
        line_number = 0
        while True:
            # Each read closes its descriptor; users do not hold files open
            # while waiting for another instance or a rate-limit deadline.
            with file.open("rb") as stream:
                stream.seek(offset)
                line = stream.readline()
                offset = stream.tell()
            if not line:
                return
            line_number += 1
            record = replay_module().parse_replay_record(
                line.decode("utf-8"), file, operation, line_number, uid)
            request = {**record.request, "user_id": uid}
            if "conversation_id" in request:
                request["conversation_id"] = identity(uid, str(request["conversation_id"]))
            yield replace(record, request=request)

    def invoke(self, user, record, operation, ready):
        started = time.monotonic()
        runtime = self.runtimes[user.instance]
        controls = self.config.controls_for(user)
        uid = identity(self.run_id, user.id)
        event = dict(user_id=uid, instance_id=user.instance, request_id=record.request_id,
            operation=operation, line_number=record.line_number, payload_sha256=record.payload_sha256,
            payload_size_bytes=record.payload_size_bytes if operation != "drain" else 0,
            started_ms=(started-self.origin)*1000, queue_ms=max(0, started-ready)*1000,
            status="failed", error_type=None, result_count=None)
        try:
            if self.config.mode == "backend_direct":
                adapter_dir = runtime.scratch / uid
                adapter_dir.mkdir(exist_ok=True)
                cfg = replay_module().ReplayConfig(data_path=self.output_dir, output_dir=self.output_dir,
                    run_dir=adapter_dir,
                    run_id=self.run_id, memory_id=runtime.config.memory_skill, agent_id="none",
                    request_timeout_seconds=controls.request_timeout_seconds,
                    drain_timeout_seconds=controls.drain_timeout_seconds,
                    poll_interval_seconds=controls.poll_interval_seconds)
                def invoke_memory(skill, request, **kwargs):
                    request.scope_id = uid
                    request.inputs["scope_id"] = uid
                    return integration.run_memory_task(skill, request, environment=runtime.environment, **kwargs)
                runner = replay_module()._run_add if operation == "add" else replay_module()._run_search
                result = runner(cfg, record, invoke_memory)
                event.update(status=result.status, result_count=result.result_count,
                             error_type=("timeout" if result.error_type in {"TimeoutError", "TimeoutExpired", "drain_timeout"}
                                         else "memory_failed") if result.status != "ok" else None)
                if event["result_count"] is not None and event["result_count"] < 0:
                    event.update(status="failed", error_type="invalid_response", result_count=None)
            elif operation == "drain":
                session = self.sessions[user.id][record.request_id]
                deadline = time.monotonic() + controls.drain_timeout_seconds
                result = runtime.plugin("flush", {**session, "agent_id": user.agent_id,
                    "compact_timeout_seconds": controls.drain_timeout_seconds}, controls.drain_timeout_seconds)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                result = runtime.plugin("wait_settle", {"operation": result.operation, "agent_id": user.agent_id,
                    "timeout_seconds": remaining, "grace_seconds": 0,
                    "interval_seconds": controls.poll_interval_seconds}, remaining)
                if result.output.get("method") not in {"exact_task_poll", "adapter_completed"}:
                    raise ValueError("unverified readiness")
                event["status"] = "ok"
            else:
                conversation = record.request.get("conversation_id", "history") if operation == "add" else record.request_id
                session_id = identity(uid, str(conversation))
                session_key = f"agent:{user.agent_id}:explicit:{session_id}"
                text = ("Historical conversation (retain the roles as source data):\n" +
                        json.dumps(record.request["messages"], ensure_ascii=False)) if operation == "add" else record.request["query"]
                request = RenderedTaskInput(task_id=record.request_id, messages=[{"role": "user", "content": text}],
                    metadata={"agent_id": user.agent_id, "session_id": session_id, "session_key": session_key,
                              "user_id": uid, "request_id": record.request_id,
                              "timeout_seconds": controls.request_timeout_seconds})
                result = integration.run_agent_task("openclaw", request, environment=runtime.environment,
                                                    timeout_seconds=controls.request_timeout_seconds)
                if not isinstance(result, dict) or result.get("status") != "ok":
                    raise ValueError("invalid Agent response")
                raw = result.get("raw")
                if (not isinstance(raw, dict) or raw.get("error")
                        or raw.get("status") not in {None, "completed"}
                        or not isinstance(raw.get("output"), list)
                        or any(not isinstance(item, dict) for item in raw["output"])):
                    raise ValueError("invalid or failed Responses API response")
                if operation == "add":
                    handle = result.get("output", {}).get("session_handle", {})
                    if not isinstance(handle, dict):
                        raise ValueError("invalid session handle")
                    self.sessions[user.id][session_key] = (replace(record, request={}, baseline={}), {"session_id": session_id,
                        "session_key": session_key, "session_handle": handle})
                event["status"] = "ok"
        except (TimeoutError, subprocess.TimeoutExpired):
            event["error_type"] = "timeout"
        except Exception:
            event["error_type"] = "drain_failed" if operation == "drain" else "invocation_failed"
        event["ended_ms"] = (time.monotonic()-self.origin)*1000
        event["duration_ms"] = event["ended_ms"] - event["started_ms"]
        return RequestEvent.model_validate(event).model_dump()

    def phase(self, operation, stream):
        started = time.monotonic()
        users = [u for u in self.config.users if self.runtimes[u.instance].state == "ready"]
        queues = {}
        for user in users:
            if operation == "drain":
                sessions = self.sessions[user.id]
                entries = list(sessions.values())
                self.sessions[user.id] = {r.request_id: handle for r, handle in entries}
                queues[user.id] = iter(r for r, _ in entries)
            else:
                queues[user.id] = self.records(user, operation)
        # Interleave instances so a large user group cannot fill the whole pool first.
        groups = {name: deque(u for u in users if u.instance == name) for name in self.runtimes}
        ordered = []
        while any(groups.values()):
            for group in groups.values():
                if group: ordered.append(group.popleft())
        pending = deque(ordered)
        ready_at = {u.id: started for u in users}
        inflight = {name: 0 for name in self.runtimes}
        remaining_users = {name: sum(u.instance == name for u in users) for name in self.runtimes}
        buffered = {}
        futures = {}
        executor = ThreadPoolExecutor(max_workers=self.config.max_workers)
        def collect(future):
            user = futures.pop(future)
            inflight[user.instance] -= 1
            event = future.result()
            self.events.append(event)
            stream.write(json.dumps(event) + "\n")
            stream.flush()
            ready_at[user.id] = max(ready_at[user.id], time.monotonic())
            pending.append(user)
        try:
            while pending or futures:
                for _ in range(len(pending)):
                    user = pending.popleft()
                    active_instances = sum(count > 0 for count in remaining_users.values())
                    # Reserve one slot per active instance when the worker budget permits.
                    # Otherwise use at most one slot per instance until one exhausts its work.
                    reserved = sum(name != user.instance and count > 0 and inflight[name] == 0
                                   for name, count in remaining_users.items())
                    available = self.config.max_workers - reserved if active_instances <= self.config.max_workers else self.config.max_workers
                    if (len(futures) >= self.config.max_workers
                            or len(futures) >= available
                            or (active_instances > self.config.max_workers and inflight[user.instance] > 0)
                            or inflight[user.instance] >= self.runtimes[user.instance].config.max_inflight):
                        pending.append(user)
                        continue
                    record = buffered.get(user.id)
                    if record is None:
                        record = next(queues[user.id], None)
                    if record is None:
                        remaining_users[user.instance] -= 1
                        continue
                    if time.monotonic() < ready_at[user.id]:
                        buffered[user.id] = record
                        pending.append(user)
                        continue
                    buffered.pop(user.id, None)
                    eligible = ready_at[user.id]
                    rate = getattr(self.config.controls_for(user), operation + "_rate", 0)
                    ready_at[user.id] = time.monotonic() + (1/rate if rate else 0)
                    future = executor.submit(self.invoke, user, record, operation, eligible)
                    futures[future] = user
                    inflight[user.instance] += 1
                if futures:
                    now = time.monotonic()
                    next_due = min((ready_at[u.id] - now for u in pending
                                    if u.id in buffered and ready_at[u.id] > now), default=0.05)
                    done, _ = wait(futures, timeout=min(0.05, next_due), return_when=FIRST_COMPLETED)
                    for future in done: collect(future)
                elif pending:
                    time.sleep(min(0.05, max(0, min(ready_at[u.id] for u in pending)-time.monotonic())))
        except BaseException:
            self.interrupted = True
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            for future in list(futures):
                if not future.cancelled(): collect(future)
            for iterator in queues.values():
                if hasattr(iterator, "close"): iterator.close()
            self.windows[operation] = (time.monotonic()-started)*1000

    def report(self):
        operations = ("add", "drain", "search")
        by_user = {identity(self.run_id, u.id): [] for u in self.config.users}
        by_instance = {name: [] for name in self.runtimes}
        for event in self.events:
            by_user[event["user_id"]].append(event)
            by_instance[event["instance_id"]].append(event)
        users = {}
        for user in self.config.users:
            uid = identity(self.run_id, user.id)
            rows = by_user[uid]
            metrics = {op: summarize(rows, op) for op in operations}
            state = "completed"
            if self.runtimes[user.instance].state != "ready": state = "setup_failed"
            elif self.interrupted: state = "interrupted"
            elif metrics["add"]["failed"] or metrics["drain"]["failed"]: state = "partially_written"
            elif metrics["search"]["failed"]: state = "failed"
            users[user.id] = dict(user_id=uid, instance_id=user.instance, workload=user.workload,
                                  status=state, operations=metrics)
        instances = {}
        for name, runtime in self.runtimes.items():
            rows = by_instance[name]
            attribution, matched = "unavailable", 0
            if runtime.config.perf_trace_path:
                try:
                    ids = set(replay_module()._load_internal_request_ids(runtime.config.perf_trace_path))
                    matched = len({e["request_id"] for e in rows} & ids)
                    attribution = "correlation_only" if matched else "unavailable"
                except Exception:
                    attribution = "trace_failed"
            instances[name] = dict(users=[u.id for u in runtime.users], status=runtime.state,
                setup_ms=self.setup_ms.get(name, 0), cleanup_failed=runtime.cleanup_failed,
                recovery_snapshot=runtime.scratch.name if runtime.cleanup_failed and runtime.scratch else None,
                isolation="probe_verified" if runtime.config.kind == "openclaw" and runtime.state == "ready" else "unavailable",
                attribution=attribution, matched_requests=matched,
                operations={op: summarize(rows, op, self.windows.get(op, 0)) for op in operations})
        status = "completed" if all(u["status"] == "completed" for u in users.values()) else "failed"
        if any(r.cleanup_failed for r in self.runtimes.values()): status = "failed"
        return {"schema": "multi-user-report/1", "run_id": self.run_id, "mode": self.config.mode,
            "model_mode": self.config.model_mode, "status": "invalid_profile" if self.profile_invalid else "interrupted" if self.interrupted else status,
            "latency_kind": "agent_end_to_end" if self.config.mode == "openclaw" else "memory_call_including_add_settle",
            "users": users, "instances": instances,
            "overall": {op: summarize(self.events, op, self.windows.get(op, 0)) for op in operations},
            "phase_windows_ms": self.windows,
            "limitations": ["internal_stages_unavailable", "quality_unavailable", "hardware_collectors_skipped", "tokens_unavailable"]}


def run_multi_user(config: MultiUserConfig, output_dir: Path, run_id: str, *,
                   profiling_config: ProfilingConfig | None = None):
    from pydantic import TypeAdapter
    from .multi_user_config import Alias
    TypeAdapter(Alias).validate_python(run_id)
    environments = preflight(config)
    with tempfile.TemporaryDirectory(prefix="memory-bench-workloads-") as temporary:
        config = _snapshot_workloads(config, Path(temporary))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=False)
        snapshot = _snapshot(config, run_id)
        if profiling_config is not None:
            snapshot["profiling"] = {"mode": profiling_config.mode,
                "hardware": profiling_config.hardware.model_dump(),
                "quality_gates": profiling_config.quality_gates.model_dump()}
        replay = Replay(config, output_dir, run_id, environments)
        snapshot["request_time_origin_utc"] = replay.origin_utc.isoformat()
        (output_dir / "run_config.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        sink = None
        cursor = None
        if profiling_config is not None:
            explicit = []
            for name, instance in config.instances.items():
                if name not in replay.runtimes:
                    continue
                identity_spec = profiling_config.external_targets.get(name)
                explicit.append(TargetIdentity(name, "memory" if instance.kind == "memory" else "agent",
                    name, instance.deployment,
                    cgroup_path=identity_spec.cgroup_path if identity_spec else None,
                    host_pid=identity_spec.host_pid if identity_spec else None,
                    cpu_scope=identity_spec.cpu_scope if identity_spec else None))
            targets = resolve_targets(profiling_config, None, explicit)
            sink = PhaseSink(output_dir, run_id, ProfilerSession(profiling_config, targets, output_dir))
            if profiling_config.internal_trace.source == "file":
                _stage_map(profiling_config.internal_trace.stage_map)
                cursor = capture_trace_cursor(profiling_config.internal_trace.path)
        try:
            with (output_dir / "request_events.jsonl").open("w", encoding="utf-8") as stream:
                for name, runtime in replay.runtimes.items():
                    started = time.monotonic()
                    try:
                        runtime.start()
                        runtime.set_phase("ingest")
                    except Exception:
                        runtime.state = "setup_failed"
                    finally:
                        replay.setup_ms[name] = (time.monotonic()-started)*1000
                def measured(operation, phase, applicable=True):
                    if sink is None or phase not in profiling_config.phases:
                        if applicable: replay.phase(operation, stream)
                        return
                    before = len(replay.events)
                    ids = [target.target_id for target in sink.session.targets.values()
                           if replay.runtimes[target.physical_instance_id].state == "ready"]
                    sink.run_phase(phase, ids,
                        lambda: replay.phase(operation, stream) if applicable else None,
                        applicable=applicable,
                        request_count=lambda: len(replay.events) - before)
                    if profiling_config.mode == "required" and sink.invalid_profile:
                        replay.profile_invalid = True
                        raise RuntimeError("invalid_profile")
                measured("add", "ingest")
                measured("drain", "drain", config.mode == "openclaw")
                for runtime in replay.runtimes.values():
                    if runtime.state == "ready":
                        try: runtime.set_phase("qa")
                        except Exception: runtime.state = "phase_failed"
                measured("search", "recall")
        except BaseException:
            replay.interrupted = not replay.profile_invalid
            raise
        finally:
            replay_failed = sys.exc_info()[0] is not None
            for runtime in reversed(list(replay.runtimes.values())):
                try: runtime.stop()
                except Exception: runtime.cleanup_failed = True
            trace_error = None
            if cursor is not None:
                try:
                    now = datetime.now(timezone.utc)
                    events = replay.events
                    import_trace(profiling_config.internal_trace.path, cursor,
                        TraceRunScope(run_id, set(replay.runtimes),
                            {identity(run_id, user.id) for user in config.users},
                            {event["request_id"] for event in events},
                            datetime.fromisoformat(snapshot["started_at"]), now,
                            profiling_config.internal_trace.max_raw_bytes),
                        profiling_config.internal_trace.stage_map, output_dir)
                except Exception as exc:
                    trace_error = exc
                finally:
                    if cursor.stream is not None:
                        cursor.stream.close()
            if trace_error is not None and profiling_config.mode == "required":
                replay.profile_invalid = True
            report = write_reports(output_dir, replay.report())
            if trace_error is not None:
                if not replay_failed:
                    raise trace_error
                print(f"trace import also failed: {trace_error}", file=sys.stderr)
        return report
