"""Request-scoped, non-overlapping timing attribution from sanitized spans."""
from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
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
            profile = config.get("profiling") or config.get("config", {}).get("profiling") or {}
            return float(profile.get("quality_gates", {}).get("min_request_join_rate", 0.95))
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
    invalid = Counter()
    extra_scopes = set()
    anchored = 0
    distributions: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    residuals = []
    scope_ids = {}
    scopes = []
    with tempfile.TemporaryDirectory(prefix="memory-bench-trace-") as scratch:
        database = sqlite3.connect(str(Path(scratch) / "spans.sqlite"))
        try:
            database.execute("PRAGMA journal_mode=OFF")
            database.execute("PRAGMA temp_store=FILE")
            database.execute("CREATE TABLE seen (span_key TEXT PRIMARY KEY)")
            database.execute("CREATE TABLE spans (scope_id INTEGER, role TEXT, category TEXT, "
                             "duration REAL, start_us INTEGER, end_us INTEGER)")
            with source.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        invalid["malformed_json"] += 1
                        continue
                    if not isinstance(row, dict) or row.get("kind") != "memory_perf":
                        invalid["malformed_row"] += 1
                        continue
                    span_id = row.get("span_id")
                    span_key = (row.get("run_id"), row.get("instance_id"), row.get("user_id"),
                                row.get("request_id"), span_id)
                    if span_id:
                        inserted = database.execute("INSERT OR IGNORE INTO seen VALUES (?)",
                            (json.dumps(span_key, separators=(",", ":")),)).rowcount
                        if not inserted:
                            invalid["duplicate_span_id"] += 1
                            continue
                    try:
                        duration = float(row["duration_ms"])
                    except (KeyError, TypeError, ValueError):
                        invalid["invalid_duration"] += 1
                        continue
                    if not math.isfinite(duration):
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
                    if key not in scope_ids:
                        scope_ids[key] = len(scopes)
                        scopes.append(key)
                    start_us = end_us = None
                    if _anchored(row):
                        anchored += 1
                        start_us = round(datetime.fromisoformat(row["start_utc"]).timestamp() * 1_000_000)
                        end_us = round(datetime.fromisoformat(row["end_utc"]).timestamp() * 1_000_000)
                    database.execute("INSERT INTO spans VALUES (?, ?, ?, ?, ?, ?)",
                        (scope_ids[key], role, category, duration, start_us, end_us))
            database.commit()
            rows = database.execute("SELECT scope_id, role, category, duration, start_us, end_us "
                                    "FROM spans ORDER BY scope_id, start_us")
            for scope_id, group in groupby(rows, key=lambda row: row[0]):
                key = scopes[scope_id]
                by_category = Counter()
                wrapper_max = 0.0
                wrapper_seen = False
                leaf_count = 0
                unanchored = False
                overlapping = False
                single_leaf = None
                interval = None
                for _, role, category, duration, start_us, end_us in group:
                    if role != "leaf":
                        wrapper_seen = True
                        wrapper_max = max(wrapper_max, duration)
                        continue
                    leaf_count += 1
                    single_leaf = (category, duration)
                    if start_us is None:
                        unanchored = True
                        continue
                    if interval is None:
                        interval = (start_us, end_us, category)
                    elif start_us < interval[1]:
                        if category != interval[2]:
                            overlapping = True
                        else:
                            interval = (interval[0], max(interval[1], end_us), category)
                    else:
                        by_category[interval[2]] += (interval[1] - interval[0]) / 1000
                        interval = (start_us, end_us, category)
                if interval is not None:
                    by_category[interval[2]] += (interval[1] - interval[0]) / 1000
                if leaf_count == 1:
                    by_category = Counter({single_leaf[0]: single_leaf[1]})
                elif unanchored:
                    invalid["unverifiable_leaf_overlap"] += 1
                    by_category.clear()
                elif overlapping:
                    invalid["overlapping_leaf_categories"] += 1
                    by_category.clear()
                operation = key[-1]
                for category, duration in by_category.items():
                    distributions[operation][category].append(duration)
                leaf_total = sum(by_category.values())
                service_total = wrapper_max if wrapper_seen else leaf_total
                client_duration = client_index[key].duration_ms
                residuals.append({"request_id": key[-2], "operation": operation,
                                  "unattributed_ms": max(0.0, service_total - leaf_total),
                                  "client_service_gap_ms": max(0.0, (client_duration or 0.0) - service_total)})
        finally:
            database.close()
    matched = len(scopes)
    total = len(client_index)
    rate = matched / total if total else 0.0
    coverage = {"matched": matched, "missing": total - matched, "extra": len(extra_scopes),
                "duplicates": invalid["duplicate_span_id"], "rate": rate}
    overlap_invalid = invalid["overlapping_leaf_categories"] or invalid["unverifiable_leaf_overlap"]
    validity = "valid" if rate >= _quality_gate(archive.run_dir) and not overlap_invalid else "invalid"
    return TraceAnalysis(coverage, {op: dict(values) for op, values in distributions.items()}, residuals,
                         dict(invalid), anchored, validity,
                         None if validity == "valid" else
                         "leaf_overlap_unverifiable" if overlap_invalid else "low_request_join_rate")
