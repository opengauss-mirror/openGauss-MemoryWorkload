from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .adapters.locomo.artifacts import load_locomo_test_artifacts

def import_external_result(run_dir: Path) -> dict[str, Any]:
    production_summary = run_dir / "production_replay_summary.json"
    if production_summary.is_file():
        return _import_production_replay(run_dir, production_summary)

    bundle = load_locomo_test_artifacts(run_dir)
    meta = bundle.meta
    diagnostics = bundle.qa_diagnostics
    csv_path = _pick_csv(run_dir)
    phase_meta_rows = _load_phase_meta_rows(run_dir)

    case_results = _load_case_results_from_csv(csv_path)
    case_results = _merge_with_phase_meta(case_results, phase_meta_rows)

    if meta:
        # Keep question count aligned with meta when possible.
        total_questions = int(meta.get("total_questions", 0) or 0)
        if total_questions <= 0 and phase_meta_rows:
            total_questions = len(phase_meta_rows)
        if not total_questions:
            total_questions = len(case_results)
        total_graded = int(meta.get("total_graded", 0) or 0)
        run_validity = _detect_locomo_run_validity(meta, diagnostics)
        return {
            "source": "locomo_test",
            "summary": {
                "overall_accuracy": meta.get("overall_accuracy", 0.0),
                "total_correct": meta.get("total_correct", 0),
                "total_graded": total_graded,
                "total_questions": total_questions,
                "ungraded_count": max(0, total_questions - total_graded),
                "accuracy_by_category": meta.get("accuracy_by_category", {}),
                "token_totals": meta.get("token_totals", {}),
                "memory_token_totals": meta.get("memory_token_totals", {}),
                "run_validity": run_validity,
            },
            "case_results": case_results,
            "benchmark_diagnostics": _build_locomo_benchmark_diagnostics(run_dir, meta, diagnostics, run_validity),
        }

    csv_path = _pick_csv(run_dir)
    case_results = _load_case_results_from_csv(csv_path)
    total_graded = sum(1 for item in case_results if item["label"] != "ungraded")
    total_correct = sum(1 for item in case_results if item["passed"])
    return {
        "source": "csv_result",
        "summary": {
            "overall_accuracy": round(total_correct / total_graded, 4) if total_graded else 0.0,
            "total_correct": total_correct,
            "total_graded": total_graded,
            "total_questions": len(case_results),
            "ungraded_count": len(case_results) - total_graded,
            "accuracy_by_category": _build_category_summary(case_results),
            "token_totals": {},
            "memory_token_totals": {},
        },
        "case_results": case_results,
    }


_PRODUCTION_EVENT_KEYS = {
    "request_id",
    "operation",
    "line_number",
    "ts",
    "payload_sha256",
    "payload_size_bytes",
    "top_level_fields",
    "duration_ms",
    "status",
    "state",
    "result_count",
    "response_size_bytes",
    "error_type",
    "error_message",
}


def _load_production_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"missing production replay event file: {path.name}")
    events: list[dict[str, Any]] = []
    request_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}: line {line_number}: invalid JSON") from exc
            if not isinstance(event, dict):
                raise ValueError(f"{path.name}: line {line_number}: event must be an object")
            forbidden = sorted(set(event) - _PRODUCTION_EVENT_KEYS)
            if forbidden:
                raise ValueError(
                    f"{path.name}: line {line_number}: forbidden event field: "
                    + ", ".join(forbidden)
                )
            request_id = event.get("request_id")
            operation = event.get("operation")
            status = event.get("status")
            state = event.get("state")
            if not isinstance(request_id, str) or not request_id:
                raise ValueError(f"{path.name}: line {line_number}: invalid request_id")
            if request_id in request_ids:
                raise ValueError(f"{path.name}: duplicate request_id: {request_id}")
            if operation not in {"add", "search"}:
                raise ValueError(f"{path.name}: line {line_number}: invalid operation")
            if status not in {"ok", "failed"} or not isinstance(state, str):
                raise ValueError(f"{path.name}: line {line_number}: invalid status or state")
            request_ids.add(request_id)
            events.append(event)
    return events


def _import_production_replay(run_dir: Path, summary_path: Path) -> dict[str, Any]:
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("production replay summary is not valid JSON") from exc
    if not isinstance(summary, dict):
        raise ValueError("production replay summary must be an object")
    if summary.get("schema") != "production-replay-summary/1":
        raise ValueError(
            f"unsupported production replay schema: {summary.get('schema')!r}"
        )

    events = _load_production_events(run_dir / "request_events.jsonl")
    case_results: list[dict[str, Any]] = []
    operation_counts = {"add": 0, "search": 0}
    operation_success = {"add": 0, "search": 0}
    for event in events:
        operation = event["operation"]
        passed = event["status"] == "ok" and event["state"] == "completed"
        operation_counts[operation] += 1
        operation_success[operation] += int(passed)
        case_results.append(
            {
                "case_id": event["request_id"],
                "passed": passed,
                "label": "passed" if passed else "failed",
                "question": operation,
                "expected": "successful request",
                "response": event["state"],
                "category": operation,
                "reasoning": event.get("error_type") or "",
            }
        )

    invalid_reasons: list[str] = []
    operations = summary.get("operations")
    if not isinstance(operations, dict):
        raise ValueError("production replay summary operations must be an object")
    for operation in ("add", "search"):
        metrics = operations.get(operation)
        if not isinstance(metrics, dict):
            raise ValueError(f"production replay summary missing {operation} metrics")
        if int(metrics.get("count", -1)) != operation_counts[operation]:
            invalid_reasons.append(f"{operation}_event_count_mismatch")
        if int(metrics.get("success", -1)) != operation_success[operation]:
            invalid_reasons.append(f"{operation}_success_count_mismatch")

    dataset_state = summary.get("dataset_state")
    attribution_status = summary.get("attribution_status")
    join_coverage = summary.get("join_coverage")
    if not isinstance(join_coverage, dict):
        raise ValueError("production replay summary join_coverage must be an object")
    if dataset_state != "complete":
        invalid_reasons.append("dataset_not_complete")
    if attribution_status != "validated":
        invalid_reasons.append("attribution_not_validated")
    if int(join_coverage.get("duplicate_request_ids", 0) or 0) > 0:
        invalid_reasons.append("duplicate_request_ids")

    total_correct = sum(item["passed"] for item in case_results)
    total_questions = len(case_results)
    run_validity = {"valid": not invalid_reasons, "reasons": invalid_reasons}
    return {
        "source": "production_http_replay",
        "summary": {
            "overall_accuracy": (
                round(total_correct / total_questions, 4) if total_questions else 0.0
            ),
            "total_correct": total_correct,
            "total_graded": total_questions,
            "total_questions": total_questions,
            "ungraded_count": 0,
            "accuracy_by_category": _build_category_summary(case_results),
            "token_totals": {},
            "memory_token_totals": {},
            "run_validity": run_validity,
        },
        "case_results": case_results,
        "benchmark_diagnostics": {
            "source": "production_http_replay",
            "dataset_state": dataset_state,
            "model_mode": summary.get("model_mode"),
            "operations": operations,
            "join_coverage": join_coverage,
            "attribution_status": attribution_status,
            "run_validity": run_validity,
        },
    }


def _build_locomo_benchmark_diagnostics(
    run_dir: Path,
    meta: dict[str, Any],
    diagnostics: dict[str, Any],
    run_validity: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "source": "locomo_test",
        "qa_reindex": meta.get("qa_reindex", {}),
        "ov_closure_summary": meta.get("ov_closure_summary", {}),
        "ov_closure_counts": meta.get("ov_closure_counts", {}),
        "issues": diagnostics.get("issues", {}),
        "qa_diagnostics_summary": diagnostics.get("ov_closure_summary", {}),
        "run_validity": run_validity,
        "artifacts": {},
    }
    artifact_map = {
        "report_html": run_dir / "report.html",
        "qa_diagnostics_json": run_dir / "qa_diagnostics.json",
        "meta_json": run_dir / "meta.json",
        "qa_results_csv": run_dir / "qa_results.csv",
    }
    for key, path in artifact_map.items():
        if path.exists():
            payload["artifacts"][key] = str(path)
    return payload


def _detect_locomo_run_validity(meta: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, Any]:
    memory_totals = meta.get("memory_token_totals", {}) if isinstance(meta, dict) else {}
    issues = diagnostics.get("issues", {}) if isinstance(diagnostics, dict) else {}
    closure_summary = meta.get("ov_closure_summary", {}) if isinstance(meta, dict) else {}

    llm_total = int(memory_totals.get("llm_total", 0) or 0)
    embedding = int(memory_totals.get("embedding", 0) or 0)
    memories = int(memory_totals.get("memories", 0) or 0)
    overall_accuracy = float(meta.get("overall_accuracy", 0.0) or 0.0)
    total_questions = int(meta.get("total_questions", 0) or 0)
    dominant_state = str(closure_summary.get("dominant_state", "") or "")

    invalid_reasons: list[str] = []
    if (
        total_questions > 0
        and overall_accuracy == 0.0
        and llm_total == 0
        and embedding == 0
        and memories == 0
        and issues.get("openviking_tokens_all_zero")
        and dominant_state == "qa_direct_recall_only"
    ):
        invalid_reasons.append("openviking_memory_extraction_unavailable")

    return {
        "valid": not invalid_reasons,
        "reasons": invalid_reasons,
    }


def _pick_csv(run_dir: Path) -> Path:
    if (run_dir / "qa_results.csv").exists():
        return run_dir / "qa_results.csv"
    phase_files = sorted(run_dir.glob("phaseA*.csv"))
    if phase_files:
        return phase_files[0]
    raise FileNotFoundError(f"no supported result file under {run_dir}")


def _load_case_results_from_csv(csv_path: Path) -> list[dict[str, Any]]:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    results: list[dict[str, Any]] = []
    for row in rows:
        result = (row.get("result") or "").strip().upper()
        case_id = row.get("case_id") or f"{row.get('sample_id', 'sample')}-q{row.get('qi', '?')}"
        label = result.lower() if result else "ungraded"
        results.append(
            {
                "case_id": case_id,
                "passed": result == "CORRECT",
                "label": label,
                "question": row.get("question", ""),
                "expected": row.get("expected") or row.get("answer", ""),
                "response": row.get("response", ""),
                "category": row.get("category", ""),
                "reasoning": row.get("reasoning", "") or ("missing judge result in external csv" if not result else ""),
                "sample_id": row.get("sample_id"),
                "qi": row.get("qi"),
            }
        )
    return results


def _normalize_case_key(sample_id: Any, qi: Any) -> str:
    return f"{sample_id or 'sample'}-q{qi if qi is not None and qi != '' else '?'}"


def _row_index_key(row: dict[str, Any]) -> str:
    qi = row.get("qi")
    sample_id = row.get("sample_id")
    case_id = row.get("case_id")
    if qi is not None and qi != "":
        return _normalize_case_key(sample_id, qi)
    if isinstance(case_id, str) and "-q" in case_id:
        return case_id
    return str(case_id or _normalize_case_key(sample_id, "?"))


def _load_phase_meta_rows(run_dir: Path) -> list[dict[str, Any]]:
    phase_meta_path = _pick_phase_meta_file(run_dir)
    if phase_meta_path is None:
        return []
    try:
        payload = json.loads(phase_meta_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    qa_rows = payload.get("qa_rows")
    if not isinstance(qa_rows, list):
        return []

    rows: list[dict[str, Any]] = []
    for row in qa_rows:
        if not isinstance(row, dict):
            continue
        result = str(row.get("result") or "").strip().upper()
        rows.append(
            {
                "case_id": _normalize_case_key(row.get("sample_id") or row.get("sample"), row.get("qi")),
                "passed": result == "CORRECT",
                "label": result.lower() if result else "ungraded",
                "question": row.get("question", ""),
                "expected": row.get("expected", ""),
                "response": row.get("response", ""),
                "category": row.get("category", ""),
                "reasoning": row.get("reasoning", "") or ("missing judge result in phaseA qa_rows" if not result else ""),
                "sample_id": row.get("sample_id") or row.get("sample"),
                "qi": row.get("qi"),
            }
        )
    return rows


def _pick_phase_meta_file(run_dir: Path) -> Path | None:
    candidates = sorted((run_dir / "external_artifacts").glob("*/phaseA*.json"))
    if not candidates:
        candidates = sorted(run_dir.glob("**/phaseA*.json"))
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("qa_rows"), list):
            return path
    return None


def _merge_with_phase_meta(
    case_results: list[dict[str, Any]],
    phase_meta_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not phase_meta_rows:
        return case_results

    csv_rows_by_key: dict[str, dict[str, Any]] = {
        _row_index_key(item): item for item in case_results
    }
    merged: list[dict[str, Any]] = []
    used_csv_keys: set[str] = set()

    for row in phase_meta_rows:
        case_id = row["case_id"]
        if case_id in csv_rows_by_key:
            merged.append(csv_rows_by_key[case_id])
            used_csv_keys.add(case_id)
        else:
            merged.append(row)

    for row in case_results:
        case_id = row["case_id"]
        if case_id not in used_csv_keys:
            merged.append(row)

    # Keep key-based canonical ids and ensure stable values for fallback rows.
    for item in merged:
        item["case_id"] = item["case_id"] or _normalize_case_key(item.get("sample_id"), item.get("qi"))

    return merged


def _build_category_summary(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    category_summary: dict[str, dict[str, Any]] = {}
    for item in case_results:
        if item["label"] == "ungraded":
            continue
        category = str(item.get("category") or "")
        bucket = category_summary.setdefault(category, {"correct": 0, "total": 0, "accuracy": 0.0})
        bucket["total"] += 1
        if item["passed"]:
            bucket["correct"] += 1
    for bucket in category_summary.values():
        bucket["accuracy"] = round(bucket["correct"] / bucket["total"], 4) if bucket["total"] else 0.0
    return category_summary
