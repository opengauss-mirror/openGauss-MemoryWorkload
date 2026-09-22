from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from memory_bench_platform.protocol import (
    MemoryTaskInput,
    MemoryTaskOutput,
    WorkflowRuntimeContext,
)


Operation = Literal["add", "search"]


@dataclass(frozen=True)
class ReplayDataset:
    add_path: Path
    search_path: Path


@dataclass(frozen=True)
class ReplayRecord:
    operation: Operation
    line_number: int
    request_id: str
    request: dict[str, Any]
    payload_sha256: str
    payload_size_bytes: int
    top_level_fields: tuple[str, ...]
    baseline: dict[str, Any]


@dataclass(frozen=True)
class ReplayConfig:
    data_path: Path
    output_dir: Path
    run_id: str
    memory_id: str
    agent_id: str
    run_dir: Path | None = None
    memory_integration: str = "backend_direct"
    add_concurrency: int = 1
    search_concurrency: int = 1
    add_rate_per_second: float = 0.0
    search_rate_per_second: float = 0.0
    request_timeout_seconds: float = 120.0
    poll_interval_seconds: float = 1.0
    drain_timeout_seconds: float = 600.0
    model_mode: str = "real-model"
    perf_trace_path: Path | None = None

    def __post_init__(self) -> None:
        if self.add_concurrency < 1 or self.search_concurrency < 1:
            raise ValueError("replay concurrency must be at least 1")
        if self.add_rate_per_second < 0 or self.search_rate_per_second < 0:
            raise ValueError("replay rate must not be negative")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        if self.poll_interval_seconds < 0 or self.drain_timeout_seconds < 0:
            raise ValueError("poll interval and drain timeout must not be negative")
        if self.model_mode not in {
            "real-model",
            "replay-zero-delay",
            "replay-with-delay",
            "mock-fixed",
        }:
            raise ValueError(f"unsupported model mode: {self.model_mode}")
        if self.memory_integration != "backend_direct":
            raise ValueError(
                "production replay requires backend_direct memory integration"
            )


@dataclass(frozen=True)
class RequestEvent:
    request_id: str
    operation: str
    line_number: int
    ts: str
    payload_sha256: str
    payload_size_bytes: int
    top_level_fields: tuple[str, ...]
    duration_ms: float
    status: str
    state: str
    result_count: int | None
    response_size_bytes: int | None
    error_type: str | None
    error_message: str | None


@dataclass(frozen=True)
class ReplaySummary:
    schema: str
    run_id: str
    dataset_state: str
    model_mode: str
    operations: dict[str, dict[str, Any]]
    join_coverage: dict[str, int]
    attribution_status: str
    events: tuple[RequestEvent, ...]
    attribution_error: str | None = None


class _Pacer:
    def __init__(self, rate_per_second: float):
        self._interval = 0.0 if rate_per_second == 0 else 1.0 / rate_per_second
        self._next_start = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._interval == 0:
            return
        with self._lock:
            now = time.monotonic()
            reserved = max(now, self._next_start)
            self._next_start = reserved + self._interval
        delay = reserved - now
        if delay > 0:
            time.sleep(delay)


def _has_operation_token(name: str, operation: Operation) -> bool:
    stem = name[: -len(".jsonl")]
    return re.search(rf"(?:^|[_-]){operation}(?:$|[_-])", stem, re.IGNORECASE) is not None


def discover_dataset(path: Path) -> ReplayDataset:
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"dataset directory not found: {root}")

    files = [
        item
        for item in root.iterdir()
        if item.is_file() and not item.is_symlink() and item.name.lower().endswith(".jsonl")
    ]
    candidates = {
        operation: sorted(
            (item for item in files if _has_operation_token(item.name, operation)),
            key=lambda item: item.name,
        )
        for operation in ("add", "search")
    }
    for operation, matches in candidates.items():
        if len(matches) != 1:
            names = ", ".join(item.name for item in matches) or "none"
            reason = "missing" if not matches else "ambiguous"
            raise ValueError(f"{reason} {operation} JSONL in {root}: {names}")
    return ReplayDataset(add_path=candidates["add"][0], search_path=candidates["search"][0])


def _canonical_request_bytes(request: dict[str, Any]) -> bytes:
    return json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _request_id(operation: str, line_number: int, digest: str, run_id: str) -> str:
    run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    return f"{operation}-{line_number:06d}-{digest[:12]}-{run_digest}"


def _result_count(response: dict[str, Any]) -> int | None:
    for value in response.values():
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            nested = _result_count(value)
            if nested is not None:
                return nested
    return None


def _baseline(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("code", "success", "status", "state"):
        value = response.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if key in response:
                result[key] = value
    count = _result_count(response)
    if count is not None:
        result["result_count"] = count
    return result


def iter_replay_records(
    path: Path, operation: Operation, run_id: str
) -> Iterator[ReplayRecord]:
    if operation not in ("add", "search"):
        raise ValueError(f"unsupported operation: {operation}")
    source = Path(path)
    with source.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                envelope = json.loads(line)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError(f"{source.name}: line {line_number}: invalid JSON") from exc
            if not isinstance(envelope, dict):
                raise ValueError(f"{source.name}: line {line_number}: row must be an object")
            request = envelope.get("request")
            if not isinstance(request, dict):
                raise ValueError(
                    f"{source.name}: line {line_number}: request must be an object"
                )

            canonical = _canonical_request_bytes(request)
            digest = hashlib.sha256(canonical).hexdigest()
            replay_request = copy.deepcopy(request)
            if replay_request.get("user_id") == "":
                replay_request["user_id"] = f"replay-{run_id}"

            yield ReplayRecord(
                operation=operation,
                line_number=line_number,
                request_id=_request_id(operation, line_number, digest, run_id),
                request=replay_request,
                payload_sha256=digest,
                payload_size_bytes=len(canonical),
                top_level_fields=tuple(sorted(request)),
                baseline=_baseline(envelope.get("response")),
            )


def validate_dataset(path: Path) -> dict[str, Any]:
    dataset = discover_dataset(path)
    records = {
        "add": iter_replay_records(dataset.add_path, "add", "validation"),
        "search": iter_replay_records(dataset.search_path, "search", "validation"),
    }
    counts: dict[str, int] = {}
    shapes: dict[str, list[list[str]]] = {}
    for operation, items in records.items():
        count = 0
        unique_shapes: set[tuple[str, ...]] = set()
        for record in items:
            count += 1
            unique_shapes.add(record.top_level_fields)
        counts[operation] = count
        shapes[operation] = [list(shape) for shape in sorted(unique_shapes)]
    return {
        "status": "ok",
        "source_path": str(Path(path)),
        "add_file": dataset.add_path.name,
        "search_file": dataset.search_path.name,
        "add_count": counts["add"],
        "search_count": counts["search"],
        "request_shapes": shapes,
    }


def _runtime_context(config: ReplayConfig) -> WorkflowRuntimeContext:
    return WorkflowRuntimeContext(
        run_id=config.run_id,
        run_dir=str(config.run_dir or config.output_dir),
        benchmark_id="production-http-replay",
        agent_id=config.agent_id,
        memory_id=config.memory_id,
        memory_integration="backend_direct",
    )


def _task_input(
    config: ReplayConfig,
    record: ReplayRecord,
    action: Literal["ingest", "status", "recall"],
    *,
    operation: dict[str, Any] | None = None,
) -> MemoryTaskInput:
    inputs: dict[str, Any] = {
        "source_protocol": "openmem-v1",
        "source_operation": record.operation,
        "request_id": record.request_id,
    }
    if action == "status":
        inputs["operation"] = copy.deepcopy(operation or {})
    else:
        inputs["raw_request"] = copy.deepcopy(record.request)
    return MemoryTaskInput(
        task_id=record.request_id,
        action=action,
        inputs=inputs,
        runtime_context=_runtime_context(config),
        idempotency_key=f"production-replay:{record.request_id}",
    )


def _response_size(output: MemoryTaskOutput) -> int:
    return len(
        json.dumps(output.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _event(
    record: ReplayRecord,
    started: float,
    output: MemoryTaskOutput | None,
    *,
    state: str | None = None,
    error_type: str | None = None,
) -> RequestEvent:
    succeeded = output is not None and output.status == "ok" and (state or output.state) == "completed"
    result_count = None
    if output is not None:
        count = output.output.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            result_count = count
        elif isinstance(output.output.get("memories"), list):
            result_count = len(output.output["memories"])
    return RequestEvent(
        request_id=record.request_id,
        operation=record.operation,
        line_number=record.line_number,
        ts=datetime.now(timezone.utc).isoformat(),
        payload_sha256=record.payload_sha256,
        payload_size_bytes=record.payload_size_bytes,
        top_level_fields=record.top_level_fields,
        duration_ms=(time.perf_counter() - started) * 1000,
        status="ok" if succeeded else "failed",
        state=state or (output.state if output is not None else "failed"),
        result_count=result_count,
        response_size_bytes=_response_size(output) if output is not None else None,
        error_type=error_type if not succeeded else None,
        error_message="memory invocation failed" if error_type else None,
    )


def _run_add(
    config: ReplayConfig,
    record: ReplayRecord,
    invoke_memory: Callable[..., MemoryTaskOutput],
) -> RequestEvent:
    started = time.perf_counter()
    try:
        output = invoke_memory(
            config.memory_id, _task_input(config, record, "ingest"),
            timeout_seconds=config.request_timeout_seconds,
        )
        if output.status == "ok" and not output.operation.get("session_id"):
            return _event(
                record,
                started,
                output,
                state="failed",
                error_type="invalid_ingest_operation",
            )
        if output.status == "ok" and output.state in {"accepted", "running"}:
            if not output.operation.get("task_id"):
                return _event(
                    record,
                    started,
                    output,
                    state="failed",
                    error_type="invalid_ingest_operation",
                )
            deadline = time.monotonic() + config.drain_timeout_seconds
            operation = copy.deepcopy(output.operation)
            while time.monotonic() < deadline:
                if config.poll_interval_seconds:
                    time.sleep(
                        min(
                            config.poll_interval_seconds,
                            max(0.0, deadline - time.monotonic()),
                        )
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _event(record, started, output, state="failed", error_type="drain_timeout")
                try:
                    output = invoke_memory(
                        config.memory_id,
                        _task_input(config, record, "status", operation=operation),
                        timeout_seconds=min(config.request_timeout_seconds, remaining),
                    )
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        return _event(record, started, None, state="failed", error_type="drain_timeout")
                    raise
                if time.monotonic() >= deadline:
                    return _event(record, started, output, state="failed", error_type="drain_timeout")
                operation = {**operation, **copy.deepcopy(output.operation)}
                if output.status == "failed" or output.state in {"completed", "failed"}:
                    break
            else:
                return _event(record, started, output, state="failed", error_type="drain_timeout")
        return _event(
            record,
            started,
            output,
            error_type=None if output.status == "ok" and output.state == "completed" else "memory_failed",
        )
    except Exception as exc:  # The request remains in the workload as a failed event.
        return _event(record, started, None, error_type=type(exc).__name__)


def _run_search(
    config: ReplayConfig,
    record: ReplayRecord,
    invoke_memory: Callable[..., MemoryTaskOutput],
) -> RequestEvent:
    started = time.perf_counter()
    try:
        output = invoke_memory(
            config.memory_id, _task_input(config, record, "recall"),
            timeout_seconds=config.request_timeout_seconds,
        )
        return _event(
            record,
            started,
            output,
            error_type=None if output.status == "ok" and output.state == "completed" else "memory_failed",
        )
    except Exception as exc:  # The request remains in the workload as a failed event.
        return _event(record, started, None, error_type=type(exc).__name__)


def _run_phase(
    records: Iterator[ReplayRecord],
    concurrency: int,
    rate_per_second: float,
    worker: Callable[[ReplayRecord], RequestEvent],
) -> tuple[list[RequestEvent], float]:
    phase_started = time.perf_counter()
    pacer = _Pacer(rate_per_second)

    def paced_worker(record: ReplayRecord) -> RequestEvent:
        pacer.wait()
        return worker(record)

    events: list[RequestEvent] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        pending = set()
        for _ in range(concurrency):
            try:
                pending.add(executor.submit(paced_worker, next(records)))
            except StopIteration:
                break
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                events.append(future.result())
                try:
                    pending.add(executor.submit(paced_worker, next(records)))
                except StopIteration:
                    pass
    return (
        sorted(events, key=lambda event: event.line_number),
        (time.perf_counter() - phase_started) * 1000,
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _operation_summary(
    events: tuple[RequestEvent, ...],
    operation: str,
    elapsed_ms: float | None = None,
) -> dict[str, Any]:
    selected = [event for event in events if event.operation == operation]
    durations = [event.duration_ms for event in selected]
    success = sum(event.status == "ok" for event in selected)
    elapsed_seconds = (elapsed_ms if elapsed_ms is not None else sum(durations)) / 1000
    result: dict[str, Any] = {
        "count": len(selected),
        "success": success,
        "success_rate": success / len(selected) if selected else 0.0,
        "qps": len(selected) / elapsed_seconds if elapsed_seconds else 0.0,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "p99_ms": _percentile(durations, 0.99),
        "max_ms": max(durations, default=0.0),
    }
    if operation == "search":
        successful = [event for event in selected if event.status == "ok"]
        non_empty = sum((event.result_count or 0) > 0 for event in successful)
        distribution: dict[str, int] = {}
        for event in successful:
            key = str(event.result_count or 0)
            distribution[key] = distribution.get(key, 0) + 1
        result["non_empty_rate"] = non_empty / len(successful) if successful else 0.0
        result["result_count_distribution"] = distribution
    return result


def summarize_replay(
    run_id: str,
    dataset_state: str,
    events: tuple[RequestEvent, ...],
    internal_request_ids: tuple[str, ...],
    model_mode: str = "real-model",
    phase_elapsed_ms: dict[str, float] | None = None,
    attribution_error: str | None = None,
) -> ReplaySummary:
    client_ids = [event.request_id for event in events]
    unique_clients = set(client_ids)
    unique_internal = set(internal_request_ids)
    matched = unique_clients & unique_internal
    duplicates = len(client_ids) - len(unique_clients)
    join_coverage = {
        "client_requests": len(events),
        "matched_internal_traces": len(matched),
        "missing_internal_traces": len(unique_clients - unique_internal),
        "duplicate_request_ids": duplicates,
        "unmatched_internal_traces": len(unique_internal - unique_clients),
    }
    attribution_status = (
        "validated"
        if unique_clients and not duplicates and not join_coverage["missing_internal_traces"] and not attribution_error
        else "exploratory"
    )
    return ReplaySummary(
        schema="production-replay-summary/1",
        run_id=run_id,
        dataset_state=dataset_state,
        model_mode=model_mode,
        operations={
            "add": _operation_summary(
                events, "add", None if phase_elapsed_ms is None else phase_elapsed_ms.get("add")
            ),
            "search": _operation_summary(
                events,
                "search",
                None if phase_elapsed_ms is None else phase_elapsed_ms.get("search"),
            ),
        },
        join_coverage=join_coverage,
        attribution_status=attribution_status,
        events=events,
        attribution_error=attribution_error,
    )


def _load_internal_request_ids(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    request_ids: list[str] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            payload = line.strip()
            if payload.startswith("[MEM_PERF]"):
                payload = payload[len("[MEM_PERF]") :].strip()
            try:
                event = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}: line {line_number}: invalid trace JSON") from exc
            request_id = event.get("request_id") if isinstance(event, dict) else None
            if isinstance(request_id, str) and request_id:
                request_ids.append(request_id)
    return tuple(request_ids)


def _write_run_config(config: ReplayConfig) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    run_config = {
        "schema": "production-replay-config/1",
        "run_id": config.run_id,
        "benchmark_id": "production-http-replay",
        "memory_id": config.memory_id,
        "agent_id": config.agent_id,
        "memory_integration": config.memory_integration,
        "model_mode": config.model_mode,
        "add_concurrency": config.add_concurrency,
        "search_concurrency": config.search_concurrency,
        "add_rate_per_second": config.add_rate_per_second,
        "search_rate_per_second": config.search_rate_per_second,
        "request_timeout_seconds": config.request_timeout_seconds,
        "poll_interval_seconds": config.poll_interval_seconds,
        "drain_timeout_seconds": config.drain_timeout_seconds,
        "identity_policy": "empty-user-id-to-replay-run-id",
    }
    (config.output_dir / "run_config.json").write_text(
        json.dumps(run_config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_request_events(config: ReplayConfig, events: tuple[RequestEvent, ...]) -> None:
    with (config.output_dir / "request_events.jsonl").open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(asdict(event), ensure_ascii=False, sort_keys=True) + "\n")


def _write_summary(config: ReplayConfig, summary: ReplaySummary) -> None:
    summary_payload = asdict(summary)
    summary_payload.pop("events")
    (config.output_dir / "production_replay_summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def execute_replay(
    config: ReplayConfig,
    invoke_memory: Callable[..., MemoryTaskOutput],
) -> ReplaySummary:
    dataset = discover_dataset(config.data_path)
    _write_run_config(config)
    add_events, add_elapsed_ms = _run_phase(
        iter_replay_records(dataset.add_path, "add", config.run_id),
        config.add_concurrency,
        config.add_rate_per_second,
        lambda record: _run_add(config, record, invoke_memory),
    )
    dataset_state = (
        "complete" if all(event.status == "ok" for event in add_events) else "partially_written"
    )
    search_events, search_elapsed_ms = _run_phase(
        iter_replay_records(dataset.search_path, "search", config.run_id),
        config.search_concurrency,
        config.search_rate_per_second,
        lambda record: _run_search(config, record, invoke_memory),
    )
    events = tuple(add_events + search_events)
    _write_request_events(config, events)
    internal_request_ids: tuple[str, ...] = ()
    attribution_error = None
    try:
        internal_request_ids = _load_internal_request_ids(config.perf_trace_path)
    except OSError:
        attribution_error = "trace_read_error"
    except ValueError:
        attribution_error = "trace_parse_error"
    summary = summarize_replay(
        config.run_id,
        dataset_state,
        events,
        internal_request_ids,
        config.model_mode,
        {"add": add_elapsed_ms, "search": search_elapsed_ms},
        attribution_error=attribution_error,
    )
    _write_summary(config, summary)
    return summary


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return default if value is None else float(value)


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return default if value is None else int(value)


def main() -> None:
    from memory_bench_platform.integration import run_memory_task

    try:
        data_path = Path(os.environ["DATA_PATH"])
        output_dir = Path(os.environ.get("OUTPUT_DIR") or os.environ["RUN_DIR"])
        config = ReplayConfig(
            data_path=data_path,
            output_dir=output_dir,
            run_id=os.environ["RUN_ID"],
            memory_id=os.environ["MEMORY_BACKEND"],
            agent_id=os.environ.get("AGENT_ID", "generic-cli"),
            run_dir=Path(os.environ.get("RUN_DIR") or output_dir),
            memory_integration=os.environ.get(
                "MEMORY_INTEGRATION", "backend_direct"
            ),
            add_concurrency=_env_int("MEMORY_BENCH_REPLAY_ADD_CONCURRENCY", 1),
            search_concurrency=_env_int("MEMORY_BENCH_REPLAY_SEARCH_CONCURRENCY", 1),
            add_rate_per_second=_env_float("MEMORY_BENCH_REPLAY_ADD_RATE", 0),
            search_rate_per_second=_env_float("MEMORY_BENCH_REPLAY_SEARCH_RATE", 0),
            request_timeout_seconds=_env_float(
                "MEMORY_BENCH_REPLAY_REQUEST_TIMEOUT_SECONDS", 120
            ),
            poll_interval_seconds=_env_float(
                "MEMORY_BENCH_REPLAY_POLL_INTERVAL_SECONDS", 1
            ),
            drain_timeout_seconds=_env_float(
                "MEMORY_BENCH_REPLAY_DRAIN_TIMEOUT_SECONDS", 600
            ),
            model_mode=os.environ.get("MEMORY_BENCH_MODEL_MODE", "real-model"),
            perf_trace_path=(
                Path(os.environ["MEMORY_BENCH_PERF_TRACE_PATH"])
                if os.environ.get("MEMORY_BENCH_PERF_TRACE_PATH")
                else None
            ),
        )
        execute_replay(config, run_memory_task)
    except (KeyError, OSError, ValueError) as exc:
        print(f"production replay configuration failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
