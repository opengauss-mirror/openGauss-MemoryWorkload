"""Request-scoped, non-overlapping timing attribution from sanitized spans."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .profiling_archive import ClientEvent, ProfileArchive


@dataclass(frozen=True)
class TraceAnalysis:
    join_coverage: dict[str, int | float]
    stage_durations_ms: dict[str, dict[str, list[float]]]
    residuals: list[dict]
    invalid_rows: dict[str, int]
    time_bucket_eligible: int
    validity: str
    reason: str | None


def _scope(run_id, instance_id, user_id, request_id, operation):
    return (run_id, instance_id, user_id, request_id, operation)


def _client_scope(event: ClientEvent):
    return _scope(event.run_id, event.instance_id, event.user_id, event.request_id, event.operation)


def _quality_gate(root: Path) -> float:
    for filename in ("run_config.json", "run.json"):
        path = root / filename
        if path.is_file():
            config = json.loads(path.read_text(encoding="utf-8"))
            return float(config.get("profiling", {}).get("quality_gates", {}).get("min_request_join_rate", 0.95))
    return 0.95


def _anchored(row: dict) -> bool:
    try:
        start = datetime.fromisoformat(row["start_utc"])
        end = datetime.fromisoformat(row["end_utc"])
        return start.tzinfo is not None and end.tzinfo is not None and end >= start
    except (KeyError, TypeError, ValueError):
        return False


def analyze_trace(archive: ProfileArchive, clients: list[ClientEvent]) -> TraceAnalysis:
    root = archive.run_dir / "profiling"
    source = root / "perf_trace_events.jsonl"
    if not source.is_file():
        return TraceAnalysis({"matched": 0, "missing": len(clients), "extra": 0,
                              "duplicates": 0, "rate": 0.0}, {}, [], {}, 0,
                             "unavailable", "trace_unavailable")
    manifest = json.loads((root / "stage_map.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "stage-map/1" or not isinstance(manifest.get("stages"), dict):
        raise ValueError("unsupported stage map")
    stage_map = manifest["stages"]
    client_index = {_client_scope(event): event for event in clients if event.request_id and event.operation}
    rows_by_scope: dict[tuple, list[tuple[dict, str, str]]] = defaultdict(list)
    invalid = Counter()
    seen_spans = set()
    extra_scopes = set()
    anchored = 0
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            invalid["malformed_json"] += 1
            continue
        if not isinstance(row, dict) or row.get("kind") != "memory_perf":
            invalid["malformed_row"] += 1
            continue
        span_id = row.get("span_id")
        if span_id and span_id in seen_spans:
            invalid["duplicate_span_id"] += 1
            continue
        if span_id:
            seen_spans.add(span_id)
        try:
            duration = float(row["duration_ms"])
        except (KeyError, TypeError, ValueError):
            invalid["invalid_duration"] += 1
            continue
        if duration < 0:
            invalid["negative_duration"] += 1
            continue
        stage = stage_map.get(row.get("stage"))
        if stage is None:
            invalid["unknown_stage"] += 1
            role = row.get("span_role", "leaf")
            category = "other_leaf" if role == "leaf" else "other_wrapper"
        else:
            role = stage["role"]
            category = stage["category"]
            if row.get("span_role") != role or row.get("operation") != stage["operation"]:
                invalid["stage_contract_mismatch"] += 1
                continue
        operation = "drain" if stage and stage.get("async") and row.get("task_id") else row.get("operation")
        key = _scope(row.get("run_id"), row.get("instance_id"), row.get("user_id"),
                     row.get("request_id"), operation)
        if key not in client_index:
            extra_scopes.add(key)
            continue
        rows_by_scope[key].append((row, role, category))
        if _anchored(row):
            anchored += 1
    distributions: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    residuals = []
    for key, rows in rows_by_scope.items():
        by_category = Counter()
        wrappers = []
        for row, role, category in rows:
            if role == "leaf":
                by_category[category] += float(row["duration_ms"])
            else:
                wrappers.append(float(row["duration_ms"]))
        operation = key[-1]
        for category, duration in by_category.items():
            distributions[operation][category].append(duration)
        leaf_total = sum(by_category.values())
        service_total = max(wrappers, default=leaf_total)
        client_duration = client_index[key].duration_ms
        residuals.append({"request_id": key[-2], "operation": operation,
                          "unattributed_ms": max(0.0, service_total - leaf_total),
                          "client_service_gap_ms": max(0.0, (client_duration or 0.0) - service_total)})
    matched = len(rows_by_scope)
    total = len(client_index)
    rate = matched / total if total else 0.0
    coverage = {"matched": matched, "missing": total - matched, "extra": len(extra_scopes),
                "duplicates": invalid["duplicate_span_id"], "rate": rate}
    validity = "valid" if rate >= _quality_gate(archive.run_dir) else "invalid"
    return TraceAnalysis(coverage, {op: dict(values) for op, values in distributions.items()}, residuals,
                         dict(invalid), anchored, validity,
                         None if validity == "valid" else "low_request_join_rate")
