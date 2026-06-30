from __future__ import annotations

import re
from typing import Any

from .locomo_test_artifacts import load_locomo_test_artifacts

_STEP_RE = re.compile(r"\[([a-zA-Z_]+)\] done in ([0-9.]+)s")


def diagnose_locomo_test_output(output_dir: Path) -> dict[str, Any]:
    bundle = load_locomo_test_artifacts(output_dir)
    meta = bundle.meta
    qa_diagnostics = bundle.qa_diagnostics
    ingest_record = bundle.ingest_record
    qa_rows = bundle.qa_rows
    pipeline_log = bundle.pipeline_log
    session_ingest_diagnostics = bundle.session_ingest_diagnostics

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
    slow_threshold = 120.0
    session_elapsed_values = [
        float((item.get("send") or {}).get("elapsed_seconds", 0.0) or 0.0)
        + float((item.get("ov_task_wait") or {}).get("elapsed_seconds", 0.0) or 0.0)
        for item in session_ingest_diagnostics
        if isinstance(item, dict) and item.get("status") == "passed"
    ]
    slow_sessions = [
        item for item in session_ingest_diagnostics
        if isinstance(item, dict)
        and item.get("status") == "passed"
        and (
            float((item.get("send") or {}).get("elapsed_seconds", 0.0) or 0.0)
            + float((item.get("ov_task_wait") or {}).get("elapsed_seconds", 0.0) or 0.0)
        ) >= slow_threshold
    ]
    timeout_sessions = [
        item for item in session_ingest_diagnostics
        if isinstance(item, dict)
        and (
            bool((item.get("send") or {}).get("timeout_hit"))
            or bool((item.get("ov_task_wait") or {}).get("timed_out"))
            or item.get("status") == "failed"
        )
    ]

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
                "session_ingest_total": len(session_ingest_diagnostics),
                "slow_session_ingest_count": len(slow_sessions),
                "timeout_session_ingest_count": len(timeout_sessions),
                "session_ingest_elapsed_max_seconds": round(max(session_elapsed_values), 3) if session_elapsed_values else 0.0,
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
            "session_ingest_elapsed_max_seconds": round(max(session_elapsed_values), 3) if session_elapsed_values else 0.0,
        },
        "artifacts": bundle.artifact_paths(),
        "session_ingest_diagnostics_summary": {
            "session_ingest_total": len(session_ingest_diagnostics),
            "slow_threshold_seconds": slow_threshold,
            "slow_session_ingest_count": len(slow_sessions),
            "timeout_session_ingest_count": len(timeout_sessions),
            "slowest_sessions": [
                {
                    "session_key": item.get("session_key"),
                    "elapsed_seconds": round(
                        float((item.get("send") or {}).get("elapsed_seconds", 0.0) or 0.0)
                        + float((item.get("ov_task_wait") or {}).get("elapsed_seconds", 0.0) or 0.0),
                        3,
                    ),
                    "send_attempts": (item.get("send") or {}).get("attempts", 0),
                    "ov_task_timed_out": bool((item.get("ov_task_wait") or {}).get("timed_out")),
                }
                for item in sorted(
                    slow_sessions,
                    key=lambda payload: (
                        float((payload.get("send") or {}).get("elapsed_seconds", 0.0) or 0.0)
                        + float((payload.get("ov_task_wait") or {}).get("elapsed_seconds", 0.0) or 0.0)
                    ),
                    reverse=True,
                )[:5]
            ],
        },
    }
    findings: list[str] = []
    if result["nodes"]["memory_capture"]["memory_written_but_index_unavailable"] > 0:
        findings.append("存在 memory written but index unavailable 样本，主要异常位于 OpenViking 检索/索引可见性。")
    if len(slow_sessions) > 0:
        findings.append(f"存在 {len(slow_sessions)} 个 session ingest 超过 {int(slow_threshold)}s，当前更像长尾慢而不是完全卡死。")
    if len(timeout_sessions) > 0:
        findings.append(f"存在 {len(timeout_sessions)} 个 session ingest 命中超时/失败边界，需要重点检查上游 LLM 长尾与 OV 任务等待。")
    if result["nodes"]["recall_query"]["recall_hit_count"] == 0 and qa_rows:
        findings.append("所有 QA 样本 recall_hit 均为 0，LoCoMo recall 闭环未真正建立。")
    if result["nodes"]["answer_generation"]["qa_total_tokens"] == 0 and result["nodes"]["answer_generation"]["total_graded"]:
        findings.append("QA usage.total_tokens 全为 0，问答 token 链仍不可信。")
    if result["nodes"]["memory_capture"]["ov_llm_total"] > 0 and result["nodes"]["answer_generation"]["overall_accuracy"] == 0:
        findings.append("OpenViking 已产出 memory token，但最终准确率仍为 0，问题更可能在 recall/answer 阶段。")
    result["findings"] = findings
    return result
