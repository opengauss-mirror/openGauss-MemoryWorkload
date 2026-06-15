from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def import_external_result(run_dir: Path) -> dict[str, Any]:
    meta_path = run_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        case_results = _load_case_results_from_csv(_pick_csv(run_dir))
        return {
            "source": "locomo_test",
            "summary": {
                "overall_accuracy": meta.get("overall_accuracy", 0.0),
                "total_correct": meta.get("total_correct", 0),
                "total_graded": meta.get("total_graded", 0),
                "total_questions": meta.get("total_questions", meta.get("total_graded", 0)),
                "ungraded_count": max(0, len(case_results) - int(meta.get("total_graded", 0) or 0)),
                "accuracy_by_category": meta.get("accuracy_by_category", {}),
                "token_totals": meta.get("token_totals", {}),
                "memory_token_totals": meta.get("memory_token_totals", {}),
            },
            "case_results": case_results,
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
            }
        )
    return results


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
