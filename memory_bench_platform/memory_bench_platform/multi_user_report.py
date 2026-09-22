"""Content-free multi-user metrics, with explicit latency and throughput windows."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .multi_user_config import Alias, Nonnegative, StrictModel, replay_module


class RequestEvent(StrictModel):
    user_id: Alias
    instance_id: Alias
    request_id: str = Field(pattern=r"^[A-Za-z0-9:_-]{1,160}$")
    operation: Literal["add", "drain", "search"]
    line_number: int = Field(ge=0, strict=True)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_size_bytes: int = Field(ge=0, strict=True)
    started_ms: Nonnegative
    ended_ms: Nonnegative
    duration_ms: Nonnegative
    queue_ms: Nonnegative
    status: Literal["ok", "failed"]
    error_type: Literal["timeout", "invocation_failed", "invalid_response", "memory_failed", "drain_failed"] | None = None
    result_count: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def valid_window(self):
        if self.ended_ms < self.started_ms:
            raise ValueError("invalid event window")
        return self


class Metrics(StrictModel):
    count: int = Field(ge=0, strict=True)
    succeeded: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)
    timeouts: int = Field(ge=0, strict=True)
    success_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    window_ms: Nonnegative
    active_window_ms: Nonnegative
    qps: Nonnegative
    p50_ms: Nonnegative
    p95_ms: Nonnegative
    p99_ms: Nonnegative
    max_ms: Nonnegative
    payload_size_bytes: int = Field(ge=0, strict=True)
    result_count: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def counts_agree(self):
        if self.succeeded + self.failed != self.count or self.timeouts > self.failed:
            raise ValueError("invalid counts")
        if not self.p50_ms <= self.p95_ms <= self.p99_ms <= self.max_ms:
            raise ValueError("invalid percentiles")
        return self


class UserSummary(StrictModel):
    user_id: Alias
    instance_id: Alias
    workload: Alias
    status: Literal["completed", "setup_failed", "partially_written", "failed", "interrupted"]
    operations: dict[Literal["add", "drain", "search"], Metrics]


class InstanceSummary(StrictModel):
    users: list[Alias]
    status: Literal["ready", "setup_failed", "pending", "phase_failed"]
    setup_ms: Nonnegative
    cleanup_failed: bool
    recovery_snapshot: str | None = Field(default=None, pattern=r"^memory-bench-instance-[A-Za-z0-9_-]+$")
    isolation: Literal["probe_verified", "unavailable"]
    attribution: Literal["unavailable", "correlation_only", "trace_failed"]
    matched_requests: int = Field(ge=0, strict=True)
    operations: dict[Literal["add", "drain", "search"], Metrics]


class Report(StrictModel):
    schema_version: Literal["multi-user-report/1"] = Field(alias="schema")
    run_id: Alias
    mode: Literal["backend_direct", "openclaw"]
    model_mode: Literal["real-model", "replay-zero-delay", "replay-with-delay", "mock-fixed"]
    status: Literal["completed", "failed", "interrupted"]
    latency_kind: Literal["memory_call_including_add_settle", "agent_end_to_end"]
    users: dict[Alias, UserSummary]
    instances: dict[Alias, InstanceSummary]
    overall: dict[Literal["add", "drain", "search"], Metrics]
    phase_windows_ms: dict[Literal["add", "drain", "search"], Nonnegative]
    limitations: list[Literal["internal_stages_unavailable", "quality_unavailable", "hardware_collectors_skipped", "tokens_unavailable"]]

    @model_validator(mode="after")
    def relations(self):
        operations = {"add", "drain", "search"}
        if not self.users or not self.instances or set(self.overall) != operations:
            raise ValueError("incomplete report")
        for name, user in self.users.items():
            if set(user.operations) != operations:
                raise ValueError("incomplete user operations")
            if user.instance_id not in self.instances or name not in self.instances[user.instance_id].users:
                raise ValueError("unknown user instance")
        for name, instance in self.instances.items():
            if set(instance.operations) != operations:
                raise ValueError("incomplete instance operations")
            if sorted(instance.users) != sorted(u for u, v in self.users.items() if v.instance_id == name):
                raise ValueError("invalid instance users")
        for operation, metric in self.overall.items():
            if metric.count != sum(u.operations[operation].count for u in self.users.values()):
                raise ValueError("inconsistent overall count")
        return self


def summarize(events, operation, window_ms=None):
    rows = [e for e in events if e["operation"] == operation]
    durations = sorted(e["duration_ms"] for e in rows)
    active = max(e["ended_ms"] for e in rows) - min(e["started_ms"] for e in rows) if rows else 0
    window = active if window_ms is None else window_ms
    success = sum(e["status"] == "ok" for e in rows)
    counts = [e["result_count"] for e in rows if e["result_count"] is not None]
    return Metrics(count=len(rows), succeeded=success, failed=len(rows)-success,
        timeouts=sum(e["error_type"] == "timeout" for e in rows),
        success_rate=success / len(rows) if rows else 0, window_ms=window, active_window_ms=active,
        qps=len(rows) * 1000 / window if window else 0,
        p50_ms=replay_module()._percentile(durations, 0.50),
        p95_ms=replay_module()._percentile(durations, 0.95),
        p99_ms=replay_module()._percentile(durations, 0.99), max_ms=max(durations, default=0),
        payload_size_bytes=sum(e["payload_size_bytes"] for e in rows),
        result_count=sum(counts) if counts else None).model_dump()


def write_reports(output_dir: Path, report: dict):
    clean = Report.model_validate(report).model_dump(mode="json", by_alias=True)
    (output_dir / "summary.json").write_text(json.dumps(clean, indent=2), encoding="utf-8")
    for group, filename in (("users", "user_summary"), ("instances", "instance_summary")):
        (output_dir / f"{filename}.json").write_text(json.dumps(clean[group], indent=2), encoding="utf-8")
        with (output_dir / f"{filename}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["id", "operation", *Metrics.model_fields])
            writer.writeheader()
            for key, summary in clean[group].items():
                for operation, metrics in summary["operations"].items():
                    writer.writerow({"id": key, "operation": operation, **metrics})
    rows = []
    for key, user in clean["users"].items():
        for operation, metrics in user["operations"].items():
            values = [key, user["instance_id"], user["status"], operation,
                      metrics["count"], metrics["success_rate"], metrics["qps"], metrics["p95_ms"]]
            rows.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in values) + "</tr>")
    body = ("<!doctype html><meta charset='utf-8'><title>Multi-user replay</title>"
            f"<h1>{html.escape(clean['run_id'])}</h1><p>{clean['status']} · {clean['latency_kind']}</p>"
            "<p>Internal stages, quality, token metrics and hardware: unavailable.</p>"
            "<table><tr><th>User</th><th>Instance</th><th>Status</th><th>Operation</th>"
            "<th>Requests</th><th>Success rate</th><th>QPS</th><th>P95 ms</th></tr>"
            + "".join(rows) + "</table>")
    (output_dir / "report.html").write_text(body, encoding="utf-8")
    return clean


def import_report(output_dir: Path):
    """Validate before rendering. No unknown report/event fields reach output."""
    try:
        report = Report.model_validate_json((output_dir / "summary.json").read_text(encoding="utf-8"))
        users = {u.user_id: u.instance_id for u in report.users.values()}
        events = []
        seen = set()
        with (output_dir / "request_events.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                event = RequestEvent.model_validate_json(line)
                if users.get(event.user_id) != event.instance_id:
                    raise ValueError("invalid event binding")
                key = (event.request_id, event.operation)
                if key in seen:
                    raise ValueError("duplicate event")
                seen.add(key)
                events.append(event.model_dump())
        # Aggregate metrics are re-derived, not trusted from a modified summary.
        for op, metrics in report.overall.items():
            expected = summarize(events, op, report.phase_windows_ms.get(op, 0))
            if metrics.model_dump() != expected:
                raise ValueError("report metrics disagree with events")
        for user in report.users.values():
            rows = [e for e in events if e["user_id"] == user.user_id]
            if any(m.model_dump() != summarize(rows, op) for op, m in user.operations.items()):
                raise ValueError("user metrics disagree with events")
        for name, instance in report.instances.items():
            rows = [e for e in events if e["instance_id"] == name]
            if any(m.model_dump() != summarize(rows, op, report.phase_windows_ms.get(op, 0))
                   for op, m in instance.operations.items()):
                raise ValueError("instance metrics disagree with events")
    except Exception:
        raise ValueError("invalid multi-user report") from None
    return write_reports(output_dir, report.model_dump(mode="json", by_alias=True))
