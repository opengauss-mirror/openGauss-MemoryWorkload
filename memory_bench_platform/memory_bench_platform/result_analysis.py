from __future__ import annotations

import csv
import json
from pathlib import Path
import re
from typing import Any

from .official_small_diagnostics import diagnose_official_small_run
from .official_small_timing import (
    build_official_small_timing_report,
    render_official_small_timing_html,
)
from .reporter import write_analysis_json, write_analysis_markdown
from .reporter import write_timing_report_html, write_timing_report_json

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

    failures = [item for item in case_results if not bool(item.get("passed"))]
    buckets = _bucket_failures(failures)
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
        "resource_summary": _read_cpu_summary(run_dir / "artifacts" / "monitor" / "cpu_status.csv"),
        "ingest_summary": _read_ingest_summary(run_dir),
        "source_artifacts": _source_artifacts(run_dir, external_result),
        "analysis_notes": _build_notes(summary, external_result, buckets, _read_ingest_summary(run_dir)),
    }
    if (run_dir / "external_artifacts" / "official_small").exists():
        try:
            analysis["chain_diagnostics"] = diagnose_official_small_run(run_dir)
        except FileNotFoundError:
            analysis["chain_diagnostics"] = {}
        try:
            timing_report = build_official_small_timing_report(run_dir)
        except FileNotFoundError:
            timing_report = {}
        if timing_report:
            write_timing_report_json(run_dir, timing_report)
            write_timing_report_html(run_dir, render_official_small_timing_html(timing_report))
            analysis["timing_report"] = {
                "json": str(run_dir / "reports" / "timing_report.json"),
                "html": str(run_dir / "reports" / "timing_report.html"),
                "duration_label_count": len(timing_report.get("duration_distributions", {})),
            }
    write_analysis_json(run_dir, analysis)
    write_analysis_markdown(run_dir, _render_analysis_markdown(analysis))
    return analysis


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


def _source_artifacts(run_dir: Path, external_result: dict[str, Any] | None) -> dict[str, Any]:
    payload = {
        "summary_json": str(run_dir / "reports" / "summary.json"),
        "case_results_json": str(run_dir / "reports" / "case_results.json"),
        "cpu_status_csv": str(run_dir / "artifacts" / "monitor" / "cpu_status.csv"),
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
    if wrong_count and retrieval_count >= max(1, wrong_count // 2):
        notes.append("当前失败样本以召回缺失或无信息拒答模式为主。")
    if ingest_summary.get("session_total", 0) and ingest_summary.get("zero_memory_sessions", 0):
        notes.append(
            f"session 级写入异常明显：{ingest_summary['session_total']} 个 session 中有 "
            f"{ingest_summary['zero_memory_sessions']} 个未写入任何 memory。"
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
    lines.extend(["", "## Ingest Summary", ""])
    for key, value in analysis["ingest_summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Notes", ""])
    for note in analysis["analysis_notes"]:
        lines.append(f"- {note}")
    return "\n".join(lines) + "\n"
