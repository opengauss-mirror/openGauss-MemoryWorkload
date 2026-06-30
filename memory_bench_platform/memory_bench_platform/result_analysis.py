from __future__ import annotations

import csv
from datetime import datetime
import html
import json
from pathlib import Path
import re
from typing import Any

from .official_small_diagnostics import diagnose_official_small_run
from .official_small_timing import (
    build_official_small_timing_report,
    render_official_small_timing_html,
)
from .external_report_import import import_external_result
from .adapters.locomo.diagnostics import diagnose_locomo_test_output
from .adapters.locomo.timing import build_locomo_test_timing_report, render_locomo_test_timing_html
from .reporter import write_analysis_json, write_analysis_markdown
from .reporter import write_case_results, write_external_result_summary, write_summary
from .reporter import write_run_report_html, write_timing_report_html, write_timing_report_json

RETRIEVAL_MISS_PATTERNS = (
    "no information",
    "no mention",
    "don't mention",
    "don't have memory",
    "do not have memory",
    "don't have information",
    "do not have information",
    "not specified",
    "no explicit information",
    "no explicit mention",
    "isn't explicit information",
    "there isn't explicit information",
    "没有相关信息",
    "没有提到",
    "没有信息",
)


def analyze_run(run_dir: Path) -> dict[str, Any]:
    run_record = _load_json(run_dir / "run.json")
    summary = _load_json(run_dir / "reports" / "summary.json")
    case_results = _load_json(run_dir / "reports" / "case_results.json")
    external_result = _load_optional_json(run_dir / "reports" / "external_result_summary.json")
    refreshed = _refresh_external_reports(run_dir, run_record)
    if refreshed is not None:
        summary, case_results, external_result = refreshed

    failures = [item for item in case_results if not bool(item.get("passed"))]
    buckets = _bucket_failures(failures)
    external_output_dir = _resolve_external_output_dir(run_dir)
    chain_diagnostics: dict[str, Any] = {}
    timing_report: dict[str, Any] = {}
    if external_result is not None and external_result.get("source") == "locomo_test" and external_output_dir is not None:
        chain_diagnostics = diagnose_locomo_test_output(external_output_dir)
        timing_report = build_locomo_test_timing_report(external_output_dir)
        write_timing_report_json(run_dir, timing_report)
        write_timing_report_html(run_dir, render_locomo_test_timing_html(timing_report))
    elif list((run_dir / "external_artifacts").glob("official_*")):
        try:
            chain_diagnostics = diagnose_official_small_run(run_dir)
        except FileNotFoundError:
            chain_diagnostics = {}
        try:
            timing_report = build_official_small_timing_report(run_dir)
        except FileNotFoundError:
            timing_report = {}
        if timing_report:
            write_timing_report_json(run_dir, timing_report)
            write_timing_report_html(run_dir, render_official_small_timing_html(timing_report))

    resource_timeline = _read_resource_timeline(run_dir, chain_diagnostics)
    analysis = {
        "run_id": summary["run_id"],
        "benchmark_id": _extract_benchmark_id(run_record),
        "agent_id": run_record.get("agent_id"),
        "entrypoint_kind": run_record.get("source_kind"),
        "status": summary["status"],
        "overall_accuracy": _compute_accuracy(summary, external_result),
        "case_total": summary["case_total"],
        "case_passed": summary["case_passed"],
        "case_failed": summary["case_failed"],
        "category_summary": summary.get("category_summary", {}),
        "failure_summary": _summarize_failures(case_results, buckets),
        "failure_buckets": buckets,
        "resource_summary": _read_resource_summary(run_dir),
        "resource_timeline": resource_timeline,
        "resource_phase_summary": _summarize_resource_phases(resource_timeline),
        "ingest_summary": _read_ingest_summary(run_dir),
        "source_artifacts": _source_artifacts(run_dir, external_result),
        "benchmark_diagnostics": _extract_benchmark_diagnostics(external_result),
        "analysis_notes": _build_notes(summary, external_result, buckets, _read_ingest_summary(run_dir)),
    }
    if chain_diagnostics:
        analysis["chain_diagnostics"] = chain_diagnostics
    if timing_report:
        analysis["timing_report"] = {
            "json": str(run_dir / "reports" / "timing_report.json"),
            "html": str(run_dir / "reports" / "timing_report.html"),
            "duration_label_count": len(timing_report.get("duration_distributions", {})),
            "report": timing_report,
        }
    write_analysis_json(run_dir, analysis)
    write_analysis_markdown(run_dir, _render_analysis_markdown(analysis))
    write_run_report_html(run_dir, _render_analysis_html(analysis))
    return analysis


def _refresh_external_reports(
    run_dir: Path,
    run_record: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]] | None:
    if run_record.get("source_kind") != "external_benchmark_runner":
        return None
    external_output_dir = _resolve_external_output_dir(run_dir)
    if external_output_dir is None:
        return None
    try:
        imported = import_external_result(external_output_dir)
    except FileNotFoundError:
        return None

    case_results = imported["case_results"]
    summary = _summary_from_external_result(run_dir.name, imported)
    write_external_result_summary(run_dir, imported)
    write_case_results(run_dir, case_results)
    write_summary(run_dir, summary)
    return summary, case_results, imported


def _resolve_external_output_dir(run_dir: Path) -> Path | None:
    record_path = run_dir / "records" / "external_entrypoint.json"
    if record_path.exists():
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        entrypoint_id = payload.get("entrypoint_id")
        if isinstance(entrypoint_id, str) and entrypoint_id:
            candidate = run_dir / "external_artifacts" / entrypoint_id
            if candidate.exists():
                return candidate

    artifacts_root = run_dir / "external_artifacts"
    if not artifacts_root.exists():
        return None
    for candidate in sorted(path for path in artifacts_root.iterdir() if path.is_dir()):
        if (candidate / "qa_results.csv").exists():
            return candidate
        if list(candidate.glob("phaseA*.csv")):
            return candidate
    return None


def _summary_from_external_result(run_id: str, imported: dict[str, Any]) -> dict[str, Any]:
    summary = imported.get("summary", {})
    total_questions = int(summary.get("total_questions", 0) or 0)
    total_correct = int(summary.get("total_correct", 0) or 0)
    total_graded = int(summary.get("total_graded", 0) or 0)
    ungraded_count = int(summary.get("ungraded_count", max(0, total_questions - total_graded)) or 0)
    run_validity = summary.get("run_validity", {}) if isinstance(summary, dict) else {}
    if total_questions <= 0:
        status = "failed"
    elif isinstance(run_validity, dict) and not bool(run_validity.get("valid", True)):
        status = "partial"
    elif ungraded_count > 0:
        status = "partial"
    else:
        status = "passed"
    return {
        "run_id": run_id,
        "status": status,
        "case_total": total_questions,
        "case_passed": total_correct,
        "case_failed": total_graded - total_correct if ungraded_count <= 0 else total_questions - total_correct,
        "category_summary": summary.get("accuracy_by_category", {}),
        "resource_summary": {
            "token_totals": summary.get("token_totals", {}),
            "memory_token_totals": summary.get("memory_token_totals", {}),
            "ungraded_count": ungraded_count,
            "run_validity": run_validity if isinstance(run_validity, dict) else {},
        },
    }


def classify_failure(case_result: dict[str, Any]) -> tuple[str, str]:
    response = str(case_result.get("response", "") or "").strip()
    question = str(case_result.get("question", "") or "").strip()
    error_detail = str(case_result.get("error_detail", "") or "").strip()
    lowered_error = error_detail.lower()
    if "401" in error_detail or "authentication" in lowered_error or "unauthorized" in lowered_error:
        return "operator_error", "authentication or provider error"
    if not response:
        return "format_or_empty", "empty response"
    lowered = response.lower()
    if any(pattern in lowered for pattern in RETRIEVAL_MISS_PATTERNS):
        return "retrieval_miss", "memory refusal pattern detected"
    if question and response == question:
        return "unsupported_no_info", "response repeats question"
    if not case_result.get("passed"):
        return "judge_mismatch_candidate", "non-empty response but judged wrong"
    return "other", "unclassified"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_optional_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return _load_json(path)


def _extract_benchmark_id(run_record: dict[str, Any]) -> str | None:
    source_id = str(run_record.get("source_id", "") or "")
    if ":" in source_id:
        return source_id.split(":", 1)[0]
    return source_id or None


def _compute_accuracy(summary: dict[str, Any], external_result: dict[str, Any] | None) -> float:
    if external_result is not None:
        return float(external_result.get("summary", {}).get("overall_accuracy", 0.0))
    total = int(summary.get("case_total", 0) or 0)
    passed = int(summary.get("case_passed", 0) or 0)
    return round(passed / total, 4) if total else 0.0


def _extract_benchmark_diagnostics(external_result: dict[str, Any] | None) -> dict[str, Any]:
    if not external_result:
        return {}
    payload = external_result.get("benchmark_diagnostics")
    return payload if isinstance(payload, dict) else {}


def _bucket_failures(failures: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {
        "retrieval_miss": [],
        "unsupported_no_info": [],
        "format_or_empty": [],
        "operator_error": [],
        "judge_mismatch_candidate": [],
        "other": [],
    }
    for item in failures:
        bucket, reason = classify_failure(item)
        buckets.setdefault(bucket, []).append(
            {
                "case_id": item.get("case_id"),
                "question": item.get("question", ""),
                "expected_answer": item.get("expected_answer") or item.get("expected", ""),
                "response": item.get("response", ""),
                "category": item.get("category", ""),
                "error_detail": item.get("error_detail", ""),
                "reason": reason,
                "bucket": bucket,
            }
        )
    return buckets


def _summarize_failures(case_results: list[dict[str, Any]], buckets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    correct_count = sum(1 for item in case_results if bool(item.get("passed")))
    wrong_count = sum(1 for item in case_results if not bool(item.get("passed")))
    return {
        "correct_count": correct_count,
        "wrong_count": wrong_count,
        "retrieval_miss_count": len(buckets.get("retrieval_miss", [])),
        "unsupported_no_info_count": len(buckets.get("unsupported_no_info", [])),
        "format_or_empty_count": len(buckets.get("format_or_empty", [])),
        "operator_error_count": len(buckets.get("operator_error", [])),
        "judge_mismatch_candidate_count": len(buckets.get("judge_mismatch_candidate", [])),
        "other_count": len(buckets.get("other", [])),
    }


def _read_cpu_summary(csv_path: Path) -> dict[str, Any]:
    if not csv_path.exists():
        return {"cpu_sample_count": 0, "missing": "artifacts/monitor/cpu_status.csv"}
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    if not rows:
        return {"cpu_sample_count": 0, "missing": "artifacts/monitor/cpu_status.csv"}
    users = [float(row["summary_util_user"]) for row in rows]
    systems = [float(row["summary_util_sys"]) for row in rows]
    idles = [float(row["summary_util_idle"]) for row in rows]
    return {
        "cpu_sample_count": len(rows),
        "cpu_user_avg": round(sum(users) / len(users), 4),
        "cpu_sys_avg": round(sum(systems) / len(systems), 4),
        "cpu_idle_avg": round(sum(idles) / len(idles), 4),
        "cpu_user_peak": max(users),
        "cpu_sys_peak": max(systems),
        "cpu_idle_min": min(idles),
    }


def _read_memory_summary(csv_path: Path) -> dict[str, Any]:
    if not csv_path.exists():
        return {"mem_sample_count": 0, "missing_mem": "artifacts/monitor/mem_status.csv"}
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    if not rows:
        return {"mem_sample_count": 0, "missing_mem": "artifacts/monitor/mem_status.csv"}
    free_values = [float(row["mem_free_mb"]) for row in rows]
    used_values = [float(row["mem_used_mb"]) for row in rows]
    return {
        "mem_sample_count": len(rows),
        "mem_free_avg_mb": round(sum(free_values) / len(free_values), 4),
        "mem_used_avg_mb": round(sum(used_values) / len(used_values), 4),
        "mem_free_min_mb": min(free_values),
        "mem_used_peak_mb": max(used_values),
    }


def _read_disk_summary(csv_path: Path) -> dict[str, Any]:
    if not csv_path.exists():
        return {"disk_sample_count": 0, "missing_disk": "artifacts/monitor/disk_status.csv"}
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    if not rows:
        return {"disk_sample_count": 0, "missing_disk": "artifacts/monitor/disk_status.csv"}
    read_values = [float(row["read_bw_mb"]) for row in rows]
    write_values = [float(row["write_bw_mb"]) for row in rows]
    total_values = [float(row["disk_bw_mb"]) for row in rows]
    free_values = [float(row["disk_free_mb"]) for row in rows]
    return {
        "disk_sample_count": len(rows),
        "disk_read_bw_avg_mb": round(sum(read_values) / len(read_values), 4),
        "disk_write_bw_avg_mb": round(sum(write_values) / len(write_values), 4),
        "disk_bw_peak_mb": max(total_values),
        "disk_free_min_mb": min(free_values),
    }


def _read_network_summary(csv_path: Path) -> dict[str, Any]:
    if not csv_path.exists():
        return {"net_sample_count": 0, "missing_net": "artifacts/monitor/net_status.csv"}
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    if not rows:
        return {"net_sample_count": 0, "missing_net": "artifacts/monitor/net_status.csv"}
    recv_packets = [float(row["recv_pcks_rate"]) for row in rows]
    sent_packets = [float(row["sent_pcks_rate"]) for row in rows]
    recv_bytes = [float(row["recv_bytes_rate"]) for row in rows]
    sent_bytes = [float(row["sent_bytes_rate"]) for row in rows]
    return {
        "net_sample_count": len(rows),
        "net_recv_packets_avg": round(sum(recv_packets) / len(recv_packets), 4),
        "net_sent_packets_avg": round(sum(sent_packets) / len(sent_packets), 4),
        "net_recv_bytes_peak": max(recv_bytes),
        "net_sent_bytes_peak": max(sent_bytes),
    }


def _read_resource_summary(run_dir: Path) -> dict[str, Any]:
    cpu_path, mem_path, disk_path, net_path = _resolve_monitor_paths(run_dir)
    payload = {}
    payload.update(_read_cpu_summary(cpu_path))
    payload.update(_read_memory_summary(mem_path))
    payload.update(_read_disk_summary(disk_path))
    payload.update(_read_network_summary(net_path))
    return payload


def _resolve_monitor_paths(run_dir: Path) -> tuple[Path, Path, Path, Path]:
    cpu_path = run_dir / "artifacts" / "monitor" / "cpu_status.csv"
    mem_path = run_dir / "artifacts" / "monitor" / "mem_status.csv"
    disk_path = run_dir / "artifacts" / "monitor" / "disk_status.csv"
    net_path = run_dir / "artifacts" / "monitor" / "net_status.csv"
    fallback_root = _resolve_external_output_dir(run_dir)
    if fallback_root is not None:
        fallback_cpu = fallback_root / "monitor" / "cpu_status.csv"
        fallback_mem = fallback_root / "monitor" / "mem_status.csv"
        fallback_disk = fallback_root / "monitor" / "disk_status.csv"
        fallback_net = fallback_root / "monitor" / "net_status.csv"
        if fallback_cpu.exists() and fallback_mem.exists():
            cpu_path = fallback_cpu
            mem_path = fallback_mem
        if fallback_disk.exists():
            disk_path = fallback_disk
        if fallback_net.exists():
            net_path = fallback_net
    return cpu_path, mem_path, disk_path, net_path


def _read_resource_timeline(run_dir: Path, chain_diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    cpu_path, mem_path, disk_path, net_path = _resolve_monitor_paths(run_dir)
    if not cpu_path.exists() or not mem_path.exists() or not disk_path.exists() or not net_path.exists():
        return {"sample_count": 0, "points": [], "phases": []}
    cpu_rows = list(csv.DictReader(cpu_path.open(encoding="utf-8")))
    mem_rows = list(csv.DictReader(mem_path.open(encoding="utf-8")))
    disk_rows = list(csv.DictReader(disk_path.open(encoding="utf-8")))
    net_rows = list(csv.DictReader(net_path.open(encoding="utf-8")))
    if not cpu_rows or not mem_rows or not disk_rows or not net_rows:
        return {"sample_count": 0, "points": [], "phases": []}
    count = min(len(cpu_rows), len(mem_rows), len(disk_rows), len(net_rows))
    raw_points: list[dict[str, Any]] = []
    for idx in range(count):
        cpu = cpu_rows[idx]
        mem = mem_rows[idx]
        disk = disk_rows[idx]
        net = net_rows[idx]
        raw_points.append(
            {
                "index": idx,
                "timestamp": cpu.get("timestamp") or mem.get("timestamp") or "",
                "cpu_user": float(cpu.get("summary_util_user") or 0.0),
                "cpu_sys": float(cpu.get("summary_util_sys") or 0.0),
                "cpu_idle": float(cpu.get("summary_util_idle") or 0.0),
                "mem_free_mb": float(mem.get("mem_free_mb") or 0.0),
                "mem_used_mb": float(mem.get("mem_used_mb") or 0.0),
                "disk_read_bw_mb": float(disk.get("read_bw_mb") or 0.0),
                "disk_write_bw_mb": float(disk.get("write_bw_mb") or 0.0),
                "disk_bw_mb": float(disk.get("disk_bw_mb") or 0.0),
                "disk_free_mb": float(disk.get("disk_free_mb") or 0.0),
                "recv_pcks_rate": float(net.get("recv_pcks_rate") or 0.0),
                "sent_pcks_rate": float(net.get("sent_pcks_rate") or 0.0),
                "recv_bytes_rate": float(net.get("recv_bytes_rate") or 0.0),
                "sent_bytes_rate": float(net.get("sent_bytes_rate") or 0.0),
            }
        )
    start_dt = _parse_resource_timestamp(str(raw_points[0]["timestamp"])) if raw_points else None
    for point in raw_points:
        current_dt = _parse_resource_timestamp(str(point["timestamp"]))
        if start_dt and current_dt:
            point["offset_seconds"] = round((current_dt - start_dt).total_seconds(), 3)
        else:
            point["offset_seconds"] = float(point["index"])
    return {
        "sample_count": count,
        "points": _sample_timeline_points(raw_points, max_points=180),
        "phases": _build_phase_markers(chain_diagnostics),
    }


def _parse_resource_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _sample_timeline_points(points: list[dict[str, Any]], *, max_points: int) -> list[dict[str, Any]]:
    if len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    sampled = [points[idx] for idx in range(0, len(points), step)]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def _build_phase_markers(chain_diagnostics: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(chain_diagnostics, dict):
        return []
    timing = chain_diagnostics.get("timing") or {}
    if not isinstance(timing, dict):
        return []
    steps = timing.get("steps") or {}
    if not isinstance(steps, dict):
        return []
    order = ["health_check_seconds", "ingest_seconds", "qa_seconds", "judge_seconds", "stats_seconds"]
    cursor = 0.0
    phases = []
    for key in order:
        if key not in steps:
            continue
        duration = float(steps.get(key) or 0.0)
        phases.append(
            {
                "label": key.removesuffix("_seconds"),
                "start_seconds": round(cursor, 3),
                "end_seconds": round(cursor + duration, 3),
            }
        )
        cursor += duration
    return phases


def _summarize_resource_phases(resource_timeline: dict[str, Any]) -> dict[str, Any]:
    points = resource_timeline.get("points", []) if isinstance(resource_timeline, dict) else []
    phases = resource_timeline.get("phases", []) if isinstance(resource_timeline, dict) else []
    if not points or not phases:
        return {}
    summary: dict[str, Any] = {}
    for phase in phases:
        label = str(phase.get("label", "") or "")
        if not label:
            continue
        start_seconds = float(phase.get("start_seconds", 0.0) or 0.0)
        end_seconds = float(phase.get("end_seconds", 0.0) or 0.0)
        phase_points = [
            point
            for point in points
            if start_seconds <= float(point.get("offset_seconds", 0.0) or 0.0) < end_seconds
        ]
        if not phase_points:
            continue
        cpu_user = [float(point.get("cpu_user", 0.0) or 0.0) for point in phase_points]
        cpu_sys = [float(point.get("cpu_sys", 0.0) or 0.0) for point in phase_points]
        mem_used = [float(point.get("mem_used_mb", 0.0) or 0.0) for point in phase_points]
        disk_bw = [float(point.get("disk_bw_mb", 0.0) or 0.0) for point in phase_points]
        recv_bytes = [float(point.get("recv_bytes_rate", 0.0) or 0.0) for point in phase_points]
        sent_bytes = [float(point.get("sent_bytes_rate", 0.0) or 0.0) for point in phase_points]
        summary[label] = {
            "sample_count": len(phase_points),
            "duration_seconds": round(end_seconds - start_seconds, 3),
            "cpu_user_avg": round(sum(cpu_user) / len(cpu_user), 4),
            "cpu_user_peak": max(cpu_user),
            "cpu_sys_avg": round(sum(cpu_sys) / len(cpu_sys), 4),
            "cpu_sys_peak": max(cpu_sys),
            "mem_used_avg_mb": round(sum(mem_used) / len(mem_used), 4),
            "mem_used_peak_mb": max(mem_used),
            "disk_bw_avg_mb": round(sum(disk_bw) / len(disk_bw), 4),
            "disk_bw_peak_mb": max(disk_bw),
            "net_recv_bytes_peak": max(recv_bytes),
            "net_sent_bytes_peak": max(sent_bytes),
        }
    return summary


def _source_artifacts(run_dir: Path, external_result: dict[str, Any] | None) -> dict[str, Any]:
    cpu_path, mem_path, disk_path, net_path = _resolve_monitor_paths(run_dir)
    payload = {
        "summary_json": str(run_dir / "reports" / "summary.json"),
        "case_results_json": str(run_dir / "reports" / "case_results.json"),
        "cpu_status_csv": str(cpu_path),
        "mem_status_csv": str(mem_path),
        "disk_status_csv": str(disk_path),
        "net_status_csv": str(net_path),
    }
    if external_result is not None:
        payload["external_result_summary_json"] = str(run_dir / "reports" / "external_result_summary.json")
        payload["external_source"] = external_result.get("source")
    return payload


def _read_ingest_summary(run_dir: Path) -> dict[str, Any]:
    remote_logs_dir = run_dir / "external_artifacts"
    master_logs = list(remote_logs_dir.glob("**/remote_logs/*.master.log"))
    if not master_logs:
        return {"session_total": 0, "zero_memory_sessions": 0, "memory_counts": []}
    text = master_logs[0].read_text(encoding="utf-8", errors="ignore")
    counts = [int(match.group(1)) for match in re.finditer(r"memories=(\d+)", text)]
    if not counts:
        return {"session_total": 0, "zero_memory_sessions": 0, "memory_counts": []}
    return {
        "session_total": len(counts),
        "zero_memory_sessions": sum(1 for item in counts if item == 0),
        "memory_counts": counts,
        "master_log": str(master_logs[0]),
    }


def _build_notes(
    summary: dict[str, Any],
    external_result: dict[str, Any] | None,
    buckets: dict[str, list[dict[str, Any]]],
    ingest_summary: dict[str, Any],
) -> list[str]:
    notes: list[str] = []
    wrong_count = int(summary.get("case_failed", 0) or 0)
    retrieval_count = len(buckets.get("retrieval_miss", []))
    benchmark_diagnostics = (
        external_result.get("benchmark_diagnostics", {})
        if isinstance(external_result, dict)
        else {}
    )
    issues = benchmark_diagnostics.get("issues", {}) if isinstance(benchmark_diagnostics, dict) else {}
    if wrong_count and retrieval_count >= max(1, wrong_count // 2):
        notes.append("当前失败样本以召回缺失或无信息拒答模式为主。")
    if ingest_summary.get("session_total", 0) and ingest_summary.get("zero_memory_sessions", 0):
        notes.append(
            f"session 级写入异常明显：{ingest_summary['session_total']} 个 session 中有 "
            f"{ingest_summary['zero_memory_sessions']} 个未写入任何 memory。"
        )
    if int(issues.get("openviking_direct_recall_only_mode", 0) or 0) > 0:
        notes.append(
            "QA 当前以 direct recall 命中 memory 后直接回答为主；这属于已验证的有效模式，不应再按 OV QA token 为 0 解释为链路异常。"
        )
    if external_result is not None:
        notes.append(f"当前分析基于外部结果导入，来源类型为 {external_result.get('source', 'unknown')}。")
    if not notes:
        notes.append("当前 run 失败样本较少，未观察到明显集中归因。")
    return notes


def _render_analysis_markdown(analysis: dict[str, Any]) -> str:
    lines = [
        "# Run Analysis",
        "",
        f"- run_id: `{analysis['run_id']}`",
        f"- benchmark_id: `{analysis.get('benchmark_id')}`",
        f"- agent_id: `{analysis.get('agent_id')}`",
        f"- entrypoint_kind: `{analysis.get('entrypoint_kind')}`",
        f"- status: `{analysis.get('status')}`",
        f"- overall_accuracy: `{analysis.get('overall_accuracy')}`",
        "",
        "## Failure Summary",
        "",
    ]
    for key, value in analysis["failure_summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Resource Summary", ""])
    for key, value in analysis["resource_summary"].items():
        lines.append(f"- {key}: `{value}`")
    if analysis.get("resource_phase_summary"):
        lines.extend(["", "## Resource Phases", ""])
        for key, value in analysis["resource_phase_summary"].items():
            lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Ingest Summary", ""])
    for key, value in analysis["ingest_summary"].items():
        lines.append(f"- {key}: `{value}`")
    if analysis.get("benchmark_diagnostics"):
        lines.extend(["", "## Benchmark Diagnostics", ""])
        for key, value in analysis["benchmark_diagnostics"].items():
            lines.append(f"- {key}: `{value}`")
    if analysis.get("chain_diagnostics"):
        lines.extend(["", "## Chain Diagnostics", ""])
        for key, value in analysis["chain_diagnostics"].items():
            lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Notes", ""])
    for note in analysis["analysis_notes"]:
        lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def _render_analysis_html(analysis: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape("" if value is None else str(value))

    def pct(value: Any) -> str:
        try:
            return f"{float(value) * 100:.2f}%"
        except Exception:
            return "0.00%"

    def render_mapping(mapping: dict[str, Any]) -> str:
        if not mapping:
            return "<tr><td colspan='2' class='muted'>无</td></tr>"
        return "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in mapping.items())

    def _format_duration_ms(value: Any) -> str:
        try:
            value_ms = float(value)
        except Exception:
            return "0 ms"
        if value_ms >= 60000:
            return f"{value_ms / 60000:.2f} min"
        if value_ms >= 1000:
            return f"{value_ms / 1000:.2f} s"
        return f"{value_ms:.0f} ms"

    def render_stage_timing_cards(timing_payload: dict[str, Any]) -> str:
        duration_distributions = timing_payload.get("duration_distributions", {}) if isinstance(timing_payload, dict) else {}
        if not duration_distributions:
            return "<section class='card' style='margin-top: 16px;'><h2>Stage Timing</h2><div class='muted'>无阶段耗时数据</div></section>"
        preferred_order = [
            "health_check_seconds",
            "ingest_seconds",
            "ingest_session_span_ms",
            "qa_seconds",
            "judge_seconds",
            "stats_seconds",
        ]
        ordered_labels = [label for label in preferred_order if label in duration_distributions]
        ordered_labels.extend(sorted(label for label in duration_distributions if label not in ordered_labels))
        cards = []
        for label in ordered_labels:
            payload = duration_distributions.get(label, {}) or {}
            cards.append(
                "<div class='mini-card'>"
                f"<div class='metric-label'>{esc(label)}</div>"
                f"<div class='metric-value mini'>{_format_duration_ms(payload.get('mean_ms', 0.0))}</div>"
                f"<div class='muted'>count={esc(payload.get('count', 0))} · p90={_format_duration_ms(payload.get('p90_ms', 0.0))}</div>"
                "</div>"
            )
        return (
            "<section class='card' style='margin-top: 16px;'><h2>Stage Timing</h2>"
            "<div class='muted'>按阶段汇总的耗时卡片，主值显示 mean。</div>"
            "<div class='mini-grid'>"
            + "".join(cards)
            + "</div></section>"
        )

    def render_stage_timeline(timing_payload: dict[str, Any]) -> str:
        duration_distributions = timing_payload.get("duration_distributions", {}) if isinstance(timing_payload, dict) else {}
        if not duration_distributions:
            return "<section class='card' style='margin-top: 16px;'><h2>Stage Timeline</h2><div class='muted'>无阶段时间轴数据</div></section>"
        stage_items: list[tuple[str, float]] = []
        for label in ["health_check_seconds", "ingest_seconds", "qa_seconds", "judge_seconds", "stats_seconds"]:
            payload = duration_distributions.get(label)
            if not isinstance(payload, dict):
                continue
            stage_items.append((label.removesuffix("_seconds"), float(payload.get("mean_ms", 0.0) or 0.0)))
        if not stage_items:
            return "<section class='card' style='margin-top: 16px;'><h2>Stage Timeline</h2><div class='muted'>无阶段时间轴数据</div></section>"
        total_ms = max(sum(duration for _, duration in stage_items), 1.0)
        cursor = 0.0
        bars = []
        ticks = []
        palette = ["#0f766e", "#2563eb", "#d97706", "#dc2626", "#7c3aed"]
        for idx, (label, duration_ms) in enumerate(stage_items):
            width_pct = max(duration_ms / total_ms * 100.0, 1.2)
            start_pct = cursor / total_ms * 100.0
            color = palette[idx % len(palette)]
            bars.append(
                "<div class='timeline-row'>"
                f"<div class='timeline-label'>{esc(label)}</div>"
                "<div class='timeline-track'>"
                f"<div class='timeline-bar' style='left:{start_pct:.3f}%; width:{width_pct:.3f}%; background:{color};'></div>"
                "</div>"
                f"<div class='timeline-value'>{_format_duration_ms(duration_ms)}</div>"
                "</div>"
            )
            ticks.append(
                "<tr>"
                f"<td>{esc(label)}</td>"
                f"<td>{_format_duration_ms(cursor)}</td>"
                f"<td>{_format_duration_ms(cursor + duration_ms)}</td>"
                f"<td>{_format_duration_ms(duration_ms)}</td>"
                "</tr>"
            )
            cursor += duration_ms
        return (
            "<section class='card' style='margin-top: 16px;'><h2>Stage Timeline</h2>"
            "<div class='muted'>按 benchmark 生命周期顺序展示阶段跨度。</div>"
            + "".join(bars)
            + "<table style='margin-top:16px;'><thead><tr><th>Stage</th><th>Start</th><th>End</th><th>Duration</th></tr></thead><tbody>"
            + "".join(ticks)
            + "</tbody></table></section>"
        )

    def render_resource_chart(title: str, points: list[dict[str, Any]], value_key: str, color: str, unit: str) -> str:
        if not points:
            return f"<section class='card' style='margin-top: 16px;'><h2>{title}</h2><div class='muted'>无采样数据</div></section>"
        width = 960
        height = 220
        pad = 28
        values = [float(point.get(value_key, 0.0)) for point in points]
        max_value = max(max(values), 1.0)
        x_max = max(float(point.get("offset_seconds", point.get("index", 0))) for point in points) or 1.0
        poly = []
        for point in points:
            x_val = float(point.get("offset_seconds", point.get("index", 0)))
            y_val = float(point.get(value_key, 0.0))
            x = pad + (x_val / x_max) * (width - pad * 2)
            y = height - pad - (y_val / max_value) * (height - pad * 2)
            poly.append(f"{x:.2f},{y:.2f}")
        phase_lines = []
        for phase in (analysis.get("resource_timeline") or {}).get("phases", []):
            phase_start = float(phase.get("start_seconds", 0.0))
            x = pad + (phase_start / x_max) * (width - pad * 2) if x_max else pad
            phase_lines.append(
                f"<line class='phase-marker' x1='{x:.2f}' y1='{pad}' x2='{x:.2f}' y2='{height-pad}' />"
                f"<text class='phase-label' x='{x+4:.2f}' y='{pad+12:.2f}'>{esc(phase.get('label'))}</text>"
            )
        return (
            f"<section class='card' style='margin-top: 16px;'><h2>{title}</h2>"
            f"<div class='muted'>samples={len(points)} max={max_value:.2f}{unit}</div>"
            f"<svg viewBox='0 0 {width} {height}' class='resource-chart'>"
            f"<polyline fill='none' stroke='{color}' stroke-width='2' points='{' '.join(poly)}' />"
            f"{''.join(phase_lines)}</svg></section>"
        )

    notes_html = "".join(f"<li>{esc(item)}</li>" for item in (analysis.get("analysis_notes") or []))
    if not notes_html:
        notes_html = "<li class='muted'>无</li>"
    benchmark_diagnostics = analysis.get("benchmark_diagnostics") or {}
    issues = benchmark_diagnostics.get("issues", {}) if isinstance(benchmark_diagnostics, dict) else {}
    direct_recall_mode = int(issues.get("openviking_direct_recall_only_mode", 0) or 0)
    timing_html = ""
    stage_timing_cards_html = ""
    stage_timeline_html = ""
    if analysis.get("timing_report"):
        timing_payload = analysis["timing_report"].get("report", {}) if isinstance(analysis["timing_report"], dict) else {}
        stage_timing_cards_html = render_stage_timing_cards(timing_payload)
        stage_timeline_html = render_stage_timeline(timing_payload)
        timing_html = (
            "<section class='card'><h2>Timing Report</h2><table><tbody>"
            + render_mapping(analysis["timing_report"])
            + "</tbody></table></section>"
        )
    chain_html = ""
    if analysis.get("chain_diagnostics"):
        chain_html = (
            "<section class='card' style='margin-top: 16px;'><h2>Chain Diagnostics</h2><table><tbody>"
            + render_mapping(analysis["chain_diagnostics"])
            + "</tbody></table></section>"
        )
    phase_html = ""
    if analysis.get("resource_phase_summary"):
        phase_html = (
            "<section class='card' style='margin-top: 16px;'><h2>Resource Phases</h2><table><tbody>"
            + render_mapping(analysis["resource_phase_summary"])
            + "</tbody></table></section>"
        )
    timeline = analysis.get("resource_timeline") or {}
    timeline_points = timeline.get("points", []) if isinstance(timeline, dict) else []
    cpu_chart = render_resource_chart("CPU Usage Timeline", timeline_points, "cpu_user", "#0f766e", "%")
    mem_chart = render_resource_chart("Memory Usage Timeline", timeline_points, "mem_used_mb", "#2563eb", "MB")
    disk_chart = render_resource_chart("Disk I/O Timeline", timeline_points, "disk_bw_mb", "#b45309", "MB/s")
    net_chart = render_resource_chart("Network I/O Timeline", timeline_points, "recv_bytes_rate", "#7c3aed", "B/s")
    qa_mode_html = ""
    if direct_recall_mode > 0:
        qa_mode_html = (
            "<section class='card' style='margin-bottom: 16px; border-color: #0f766e; background: #ecfdf5;'>"
            "<h2>QA Mode</h2>"
            f"<div>检测到 <strong>{direct_recall_mode}</strong> 条 <code>qa_direct_recall_only</code> 样本。</div>"
            "<div class='muted' style='margin-top:8px;'>"
            "这表示 QA 主要通过 direct recall 命中 memory 后直接回答，属于当前已验证的有效模式，"
            "不应再按 OV QA token 为 0 解释为链路异常。"
            "</div></section>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{esc(analysis.get("run_id"))} - Run Report</title>
  <style>
    :root {{
      --bg: #f4f7fb;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #667085;
      --line: #d9e2ec;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; color: var(--text); background: linear-gradient(180deg, #f8fafc, #edf4f7); }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
    .hero, .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 16px; box-shadow: 0 8px 24px rgba(15, 23, 42, 0.05); }}
    .hero {{ padding: 24px; margin-bottom: 20px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-bottom: 20px; }}
    .sections {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .card {{ padding: 18px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .metric-label {{ color: var(--muted); font-size: 13px; }}
    .metric-value {{ font-size: 28px; font-weight: 700; margin-top: 8px; }}
    .metric-value.mini {{ font-size: 22px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 10px 12px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }}
    .muted {{ color: var(--muted); }}
    ul {{ margin: 0; padding-left: 18px; line-height: 1.8; }}
    .mini-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }}
    .mini-card {{ background: #f8fafc; border: 1px solid var(--line); border-radius: 12px; padding: 14px; }}
    .resource-chart {{ width: 100%; height: auto; margin-top: 12px; background: #f8fafc; border: 1px solid var(--line); border-radius: 8px; }}
    .phase-marker {{ stroke: #94a3b8; stroke-width: 1; stroke-dasharray: 4 4; }}
    .phase-label {{ fill: #64748b; font-size: 10px; }}
    .timeline-row {{ display: grid; grid-template-columns: 140px 1fr 110px; gap: 12px; align-items: center; margin-top: 12px; }}
    .timeline-label, .timeline-value {{ font-size: 13px; }}
    .timeline-track {{ position: relative; height: 14px; background: #e5edf5; border-radius: 999px; overflow: hidden; }}
    .timeline-bar {{ position: absolute; top: 0; bottom: 0; border-radius: 999px; }}
    @media (max-width: 960px) {{ .grid, .sections {{ grid-template-columns: 1fr 1fr; }} }}
    @media (max-width: 960px) {{ .mini-grid {{ grid-template-columns: 1fr 1fr; }} }}
    @media (max-width: 680px) {{ .grid, .sections, .mini-grid {{ grid-template-columns: 1fr; }} .timeline-row {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>{esc(analysis.get("run_id"))}</h1>
      <div class="muted">benchmark={esc(analysis.get("benchmark_id"))} · agent={esc(analysis.get("agent_id"))} · status={esc(analysis.get("status"))}</div>
    </section>
    <section class="grid">
      <div class="card"><div class="metric-label">Overall Accuracy</div><div class="metric-value">{pct(analysis.get("overall_accuracy"))}</div></div>
      <div class="card"><div class="metric-label">Cases</div><div class="metric-value">{esc(analysis.get("case_passed"))} / {esc(analysis.get("case_total"))}</div></div>
      <div class="card"><div class="metric-label">Failures</div><div class="metric-value">{esc(analysis.get("case_failed"))}</div></div>
      <div class="card"><div class="metric-label">EntryPoint</div><div class="metric-value">{esc(analysis.get("entrypoint_kind"))}</div></div>
    </section>
    {qa_mode_html}
    {stage_timing_cards_html}
    <section class="sections">
      <section class="card"><h2>Failure Summary</h2><table><tbody>{render_mapping(analysis.get("failure_summary", {}))}</tbody></table></section>
      <section class="card"><h2>Resource Summary</h2><table><tbody>{render_mapping(analysis.get("resource_summary", {}))}</tbody></table></section>
      <section class="card"><h2>Ingest Summary</h2><table><tbody>{render_mapping(analysis.get("ingest_summary", {}))}</tbody></table></section>
      <section class="card"><h2>Benchmark Diagnostics</h2><table><tbody>{render_mapping(analysis.get("benchmark_diagnostics", {}))}</tbody></table></section>
      <section class="card"><h2>Category Summary</h2><table><tbody>{render_mapping(analysis.get("category_summary", {}))}</tbody></table></section>
      <section class="card"><h2>Artifacts</h2><table><tbody>{render_mapping(analysis.get("source_artifacts", {}))}</tbody></table></section>
    </section>
    {stage_timeline_html}
    {cpu_chart}
    {mem_chart}
    {disk_chart}
    {net_chart}
    {phase_html}
    {timing_html}
    {chain_html}
    <section class="card" style="margin-top: 16px;">
      <h2>Analysis Notes</h2>
      <ul>{notes_html}</ul>
    </section>
  </div>
</body>
</html>"""
