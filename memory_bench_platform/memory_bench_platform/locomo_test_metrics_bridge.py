from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def check_locomo_qa_results(output_dir: str) -> dict[str, Any]:
    csv_path = Path(output_dir) / "qa_results.csv"
    issues: dict[str, Any] = {}
    if not csv_path.exists():
        issues["missing_csv"] = str(csv_path)
        return issues

    try:
        with csv_path.open("r", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except Exception as exc:
        issues["csv_read_error"] = str(exc)
        return issues

    empty_responses = sum(1 for row in rows if not row.get("response") or str(row["response"]).startswith("[ERROR"))
    if empty_responses:
        issues["empty_or_error_responses"] = empty_responses

    ov_token_col = "ov_llm_total_tokens"
    if rows and ov_token_col in rows[0]:
        ov_zero = sum(1 for row in rows if int((row.get(ov_token_col) or "0").strip() or 0) == 0)
        if ov_zero == len(rows):
            issues["openviking_tokens_all_zero"] = ov_zero

    ov_missing_col = "ov_missing_records"
    if rows and ov_missing_col in rows[0]:
        max_missing = max(int((row.get(ov_missing_col) or "0").strip() or 0) for row in rows)
        if max_missing > 0:
            issues["openviking_index_missing_records_max"] = max_missing
            rows_with_tokens = sum(
                1
                for row in rows
                if int((row.get(ov_token_col) or "0").strip() or 0) > 0
            )
            if rows_with_tokens > 0:
                issues["openviking_memory_written_but_index_unavailable"] = rows_with_tokens
    return issues


def summarize_locomo_qa_results(output_dir: str) -> dict[str, Any]:
    csv_path = Path(output_dir) / "qa_results.csv"
    if not csv_path.exists():
        return {"missing_csv": str(csv_path)}

    try:
        with csv_path.open("r", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except Exception as exc:
        return {"csv_read_error": str(exc)}

    valid = [row for row in rows if row.get("category") != "5"]
    closure_counts: dict[str, int] = {}
    for row in valid:
        state = str(row.get("ov_closure_state") or "").strip()
        if not state:
            continue
        closure_counts[state] = closure_counts.get(state, 0) + 1

    def _has_true(field: str) -> bool:
        return any(str((row.get(field) or "")).strip().lower() == "true" for row in valid)

    dominant_state = ""
    if closure_counts:
        dominant_state = max(sorted(closure_counts.items()), key=lambda item: item[1])[0]

    return {
        "rows": len(rows),
        "valid_rows": len(valid),
        "issues": check_locomo_qa_results(output_dir),
        "ov_closure_counts": closure_counts,
        "ov_closure_summary": {
            "dominant_state": dominant_state,
            "has_memory_written": _has_true("ov_memory_written"),
            "has_token_emitted": _has_true("ov_token_emitted"),
            "has_index_unavailable": any(
                str((row.get("ov_index_available") or "")).strip().lower() == "false"
                for row in valid
            ),
        }
        if closure_counts
        else {},
    }


def write_locomo_qa_diagnostics(output_dir: str) -> dict[str, Any]:
    diagnostics = summarize_locomo_qa_results(output_dir)
    path = Path(output_dir) / "qa_diagnostics.json"
    path.write_text(json.dumps(diagnostics, indent=2, ensure_ascii=False), encoding="utf-8")
    return diagnostics


def derive_locomo_ov_closure_summary(rows: list[dict], counts: dict[str, int]) -> dict[str, Any]:
    if not rows or not counts:
        return {}

    dominant_state = max(sorted(counts.items()), key=lambda item: item[1])[0]

    def _has_true(field: str) -> bool:
        return any(str((row.get(field) or "")).strip().lower() == "true" for row in rows)

    return {
        "dominant_state": dominant_state,
        "has_memory_written": _has_true("ov_memory_written"),
        "has_token_emitted": _has_true("ov_token_emitted"),
        "has_index_unavailable": any(
            str((row.get("ov_index_available") or "")).strip().lower() == "false"
            for row in rows
        ),
    }
