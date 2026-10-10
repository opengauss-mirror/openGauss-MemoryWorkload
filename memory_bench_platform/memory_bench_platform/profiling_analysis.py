"""Recompute profiling findings solely from archived measurements."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .profiling_archive import load_client_events, load_profile_archive
from .profiling_trace_analysis import analyze_trace
from .profiling_report import write_profiling_reports


@dataclass(frozen=True)
class ProfilingAnalysis:
    summary: dict
    report_paths: dict[str, str]


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _gate(run_dir: Path, key: str, default: float) -> float:
    for filename in ("run_config.json", "run.json"):
        path = run_dir / filename
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return float(data.get("profiling", {}).get("quality_gates", {}).get(key, default))
    return default


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _hardware(archive) -> dict[str, dict]:
    counters = _rows(archive.run_dir / "profiling" / "perf_counters.csv")
    topdown = _rows(archive.run_dir / "profiling" / "topdown_summary.csv")
    threshold = _gate(archive.run_dir, "min_counter_running_fraction", 0.90)
    result = {}
    for phase in archive.phases:
        key = f"{phase.target_id}:{phase.phase_id}"
        status = next((s for s in archive.collectors if s.target_id == phase.target_id and s.phase_id == phase.phase_id), None)
        if status is None:
            result[key] = {"state": "skipped", "reason": "collector_unavailable"}
            continue
        if status.state != "completed":
            result[key] = {"state": status.state, "reason": status.reason}
            continue
        selected = [r for r in counters if r.get("target_id") == phase.target_id and r.get("phase_id") == phase.phase_id]
        if not selected:
            result[key] = {"state": "invalid", "reason": "missing_counters"}
            continue
        scopes = {(r.get("cpu_scope"), r.get("event_group")) for r in selected}
        if len(scopes) != 1:
            result[key] = {"state": "invalid", "reason": "incomparable_event_scope"}
            continue
        try:
            fractions = [float(r["running_fraction"]) for r in selected]
            values = {r["event"]: float(r["value"]) for r in selected}
        except (KeyError, TypeError, ValueError):
            result[key] = {"state": "invalid", "reason": "unparseable_counter"}
            continue
        if any(value < threshold or value > 1 for value in fractions):
            result[key] = {"state": "invalid", "reason": "low_running_fraction",
                           "running_fraction_min": min(fractions)}
            continue
        item = {"state": "completed", "cpu_scope": selected[0]["cpu_scope"],
                "event_group": selected[0]["event_group"], "running_fraction_min": min(fractions),
                "counters": values}
        if values.get("cycles", 0) > 0 and "instructions" in values:
            item["ipc"] = values["instructions"] / values["cycles"]
        td = [r for r in topdown if r.get("target_id") == phase.target_id and r.get("phase_id") == phase.phase_id]
        if td:
            if (len(td) != 1 or td[0].get("cpu_scope") != item["cpu_scope"]
                    or td[0].get("event_group") != item["event_group"]):
                item.update(topdown_valid=False, topdown_reason="incomparable_event_scope")
            else:
                try:
                    shares = {name: float(td[0][name]) for name in
                              ("frontend_bound", "bad_speculation", "backend_bound", "retiring")}
                except (KeyError, TypeError, ValueError):
                    item.update(topdown_valid=False, topdown_reason="unparseable_topdown")
                else:
                    valid = 95 <= sum(shares.values()) <= 105 and all(0 <= x <= 100 for x in shares.values())
                    item.update(topdown_valid=valid, topdown_reason=None if valid else "sum_out_of_range")
                    if valid:
                        item["topdown_l1"] = shares
        result[key] = item
    return result


def _phase_metrics(archive, clients) -> dict[str, dict]:
    operation_for_phase = {"ingest": "add", "drain": "drain", "recall": "search"}
    results = {}
    for phase in archive.phases:
        key = f"{phase.target_id}:{phase.phase_id}"
        operation = operation_for_phase.get(phase.phase_id)
        physical = next(target.get("physical_instance_id") for target in archive.targets
                        if target["target_id"] == phase.target_id)
        selected = [event for event in clients if event.instance_id == physical
                    and event.operation == operation and event.started_utc is not None
                    and phase.start_utc <= event.started_utc < phase.end_utc]
        seconds = (phase.end_monotonic_ns - phase.start_monotonic_ns) / 1_000_000_000
        by_user = {}
        for event in selected:
            user = event.user_id or "unscoped"
            item = by_user.setdefault(user, {"count": 0, "succeeded": 0})
            item["count"] += 1
            item["succeeded"] += event.status == "ok"
        durations = [float(event.duration_ms) for event in selected if event.duration_ms is not None]
        results[key] = {"state": phase.state, "window_ms": seconds * 1000,
                        "count": len(selected), "succeeded": sum(e.status == "ok" for e in selected),
                        "success_rate": sum(e.status == "ok" for e in selected) / len(selected) if selected else 0.0,
                        "qps": len(selected) / seconds if seconds else 0.0,
                        "p50_ms": _percentile(durations, 0.50),
                        "p95_ms": _percentile(durations, 0.95),
                        "p99_ms": _percentile(durations, 0.99), "users": by_user}
    return results


def analyze_profiling_archive(run_dir: Path) -> ProfilingAnalysis | None:
    archive = load_profile_archive(run_dir)
    if archive is None:
        return None
    clients = load_client_events(run_dir)
    trace = analyze_trace(archive, clients)
    summary = {"schema": "profiling-analysis/1", "run_id": archive.phases[0].run_id if archive.phases else None,
               "phases": _phase_metrics(archive, clients), "hardware": _hardware(archive),
               "trace": {"validity": trace.validity, "reason": trace.reason,
                         "join_coverage": trace.join_coverage, "invalid_rows": trace.invalid_rows,
                         "time_bucket_eligible": trace.time_bucket_eligible,
                         "stage_durations_ms": trace.stage_durations_ms,
                         "residuals": trace.residuals}}
    report_path = run_dir / "reports" / "profiling_analysis.json"
    if report_path.is_file() and json.loads(report_path.read_text(encoding="utf-8")) != summary:
        raise ValueError("profiling report disagrees with archived evidence")
    paths = write_profiling_reports(run_dir, summary)
    return ProfilingAnalysis(summary, paths)
