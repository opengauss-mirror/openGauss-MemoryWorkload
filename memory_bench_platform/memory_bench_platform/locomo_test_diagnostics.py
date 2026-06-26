from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any


_STEP_RE = re.compile(r"\[([a-zA-Z_]+)\] done in ([0-9.]+)s")


def diagnose_locomo_test_output(output_dir: Path) -> dict[str, Any]:
    meta = _load_optional_json(output_dir / "meta.json")
    qa_diagnostics = _load_optional_json(output_dir / "qa_diagnostics.json")
    ingest_record = _load_optional_json(output_dir / ".ingest_record.json")
    qa_rows = _load_csv_rows(output_dir / "qa_results.csv")
    pipeline_log = (output_dir / "pipeline.log").read_text(encoding="utf-8", errors="ignore") if (output_dir / "pipeline.log").exists() else ""

    recall_totals = [int(row.get("ov_recall_total", 0) or 0) for row in qa_rows if str(row.get("ov_recall_total", "")).strip()]
    recall_hits = sum(1 for row in qa_rows if str(row.get("ov_recall_hit", "")).strip().lower() == "true")
    consistency_values = [int(row.get("ov_missing_records", 0) or 0) for row in qa_rows if str(row.get("ov_missing_records", "")).strip()]
    ov_token_rows = [int(row.get("ov_llm_total_tokens", 0) or 0) for row in qa_rows if str(row.get("ov_llm_total_tokens", "")).strip()]
    qa_token_rows = [int(row.get("total_tokens", 0) or 0) for row in qa_rows if str(row.get("total_tokens", "")).strip()]

    timings = {
        f"{match.group(1)}_seconds": float(match.group(2))
        for match in _STEP_RE.finditer(pipeline_log)
    }
    ingest_session_timestamps = sorted(
        int(item.get("timestamp", 0) or 0)
        for item in (ingest_record or {}).values()
        if isinstance(item, dict)
    )
    ingest_span_seconds = (
        ingest_session_timestamps[-1] - ingest_session_timestamps[0]
        if len(ingest_session_timestamps) >= 2
        else 0
    )

    result = {
        "source": "locomo_test",
        "nodes": {
            "session_construction": {
                "dataset": meta.get("dataset"),
                "session_policy": meta.get("session_policy"),
                "ingest_session_count": len(ingest_record or {}),
            },
            "memory_capture": {
                "closure_dominant_state": (meta.get("ov_closure_summary") or {}).get("dominant_state"),
                "closure_counts": meta.get("ov_closure_counts", {}),
                "index_missing_records_max": (qa_diagnostics.get("issues") or {}).get("openviking_index_missing_records_max", 0),
                "memory_written_but_index_unavailable": (qa_diagnostics.get("issues") or {}).get(
                    "openviking_memory_written_but_index_unavailable", 0
                ),
                "ov_llm_total": (meta.get("memory_token_totals") or {}).get("llm_total", 0),
                "ov_embedding_total": (meta.get("memory_token_totals") or {}).get("embedding", 0),
                "ov_memories_total": (meta.get("memory_token_totals") or {}).get("memories", 0),
            },
            "recall_query": {
                "qa_row_count": len(qa_rows),
                "recall_hit_count": recall_hits,
                "recall_total_max": max(recall_totals) if recall_totals else 0,
                "recall_total_mean": round(sum(recall_totals) / len(recall_totals), 4) if recall_totals else 0.0,
                "missing_records_max": max(consistency_values) if consistency_values else 0,
            },
            "answer_generation": {
                "total_questions": meta.get("total_questions", len(qa_rows)),
                "total_correct": meta.get("total_correct", 0),
                "total_graded": meta.get("total_graded", 0),
                "overall_accuracy": meta.get("overall_accuracy", 0.0),
                "qa_total_tokens": sum(qa_token_rows),
                "ov_llm_token_rows": sum(1 for value in ov_token_rows if value > 0),
            },
        },
        "timing": {
            "steps": timings,
            "ingest_session_span_seconds": ingest_span_seconds,
        },
        "artifacts": {
            key: str(path)
            for key, path in {
                "meta_json": output_dir / "meta.json",
                "qa_diagnostics_json": output_dir / "qa_diagnostics.json",
                "qa_results_csv": output_dir / "qa_results.csv",
                "pipeline_log": output_dir / "pipeline.log",
                "ingest_record_json": output_dir / ".ingest_record.json",
                "report_html": output_dir / "report.html",
            }.items()
            if path.exists()
        },
    }
    findings: list[str] = []
    if result["nodes"]["memory_capture"]["memory_written_but_index_unavailable"] > 0:
        findings.append("存在 memory written but index unavailable 样本，主要异常位于 OpenViking 检索/索引可见性。")
    if result["nodes"]["recall_query"]["recall_hit_count"] == 0 and qa_rows:
        findings.append("所有 QA 样本 recall_hit 均为 0，LoCoMo recall 闭环未真正建立。")
    if result["nodes"]["answer_generation"]["qa_total_tokens"] == 0 and result["nodes"]["answer_generation"]["total_graded"]:
        findings.append("QA usage.total_tokens 全为 0，问答 token 链仍不可信。")
    if result["nodes"]["memory_capture"]["ov_llm_total"] > 0 and result["nodes"]["answer_generation"]["overall_accuracy"] == 0:
        findings.append("OpenViking 已产出 memory token，但最终准确率仍为 0，问题更可能在 recall/answer 阶段。")
    result["findings"] = findings
    return result


def _load_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
