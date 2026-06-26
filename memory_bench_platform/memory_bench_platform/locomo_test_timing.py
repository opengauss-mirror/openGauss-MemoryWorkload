from __future__ import annotations

import re
import statistics
from typing import Any

from .locomo_test_artifacts import load_locomo_test_artifacts

_STEP_RE = re.compile(r"\[([a-zA-Z_]+)\] done in ([0-9.]+)s")


def build_locomo_test_timing_report(output_dir: Path) -> dict[str, Any]:
    bundle = load_locomo_test_artifacts(output_dir)
    meta = bundle.meta
    qa_rows = bundle.qa_rows
    pipeline_log = bundle.pipeline_log
    ingest_record = bundle.ingest_record

    step_seconds = {
        f"{match.group(1)}_seconds": float(match.group(2))
        for match in _STEP_RE.finditer(pipeline_log)
    }
    recall_totals = [int(row.get("ov_recall_total", 0) or 0) for row in qa_rows if str(row.get("ov_recall_total", "")).strip()]
    missing_records = [int(row.get("ov_missing_records", 0) or 0) for row in qa_rows if str(row.get("ov_missing_records", "")).strip()]
    ov_llm_totals = [int(row.get("ov_llm_total_tokens", 0) or 0) for row in qa_rows if str(row.get("ov_llm_total_tokens", "")).strip()]
    timestamps = sorted(
        int(item.get("timestamp", 0) or 0)
        for item in (ingest_record or {}).values()
        if isinstance(item, dict)
    )
    ingest_span_seconds = timestamps[-1] - timestamps[0] if len(timestamps) >= 2 else 0

    duration_distributions = {
        key: _quantiles_ms([value * 1000.0])
        for key, value in step_seconds.items()
    }
    if ingest_span_seconds > 0:
        duration_distributions["ingest_session_span_ms"] = _quantiles_ms([ingest_span_seconds * 1000.0])

    return {
        "run_id": meta.get("name", output_dir.name),
        "source": "locomo_test",
        "question_count": len(qa_rows),
        "duration_distributions": duration_distributions,
        "token_summary": {
            "qa_total_tokens": sum(int(row.get("total_tokens", 0) or 0) for row in qa_rows if str(row.get("total_tokens", "")).strip()),
            "ov_llm_total_tokens_sum": sum(ov_llm_totals),
            "ov_llm_total_tokens_max": max(ov_llm_totals) if ov_llm_totals else 0,
            "ov_embedding_total_tokens": int((meta.get("memory_token_totals") or {}).get("embedding", 0) or 0),
        },
        "recall_summary": {
            "recall_total_mean": round(statistics.mean(recall_totals), 4) if recall_totals else 0.0,
            "recall_total_max": max(recall_totals) if recall_totals else 0,
            "missing_records_max": max(missing_records) if missing_records else 0,
        },
    }


def render_locomo_test_timing_html(report: dict[str, Any]) -> str:
    rows = []
    for label, payload in sorted((report.get("duration_distributions") or {}).items()):
        rows.append(
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{payload.get('count', 0)}</td>"
            f"<td>{payload.get('min_ms', 0.0)}</td>"
            f"<td>{payload.get('max_ms', 0.0)}</td>"
            f"<td>{payload.get('mean_ms', 0.0)}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='5'>No timing data</td></tr>")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{report.get('run_id', 'locomo_test')} - Timing Report</title>
  <style>
    body {{ font-family: "Segoe UI", "PingFang SC", sans-serif; margin: 24px; color: #1f2937; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #d9e2ec; padding: 10px 12px; text-align: left; }}
    th {{ background: #f8fafc; }}
  </style>
</head>
<body>
  <h1>Timing Report</h1>
  <p>run_id={report.get('run_id')} source=locomo_test</p>
  <table>
    <thead><tr><th>Label</th><th>Count</th><th>Min(ms)</th><th>Max(ms)</th><th>Mean(ms)</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body>
</html>"""


def _quantiles_ms(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "min_ms": 0.0, "max_ms": 0.0, "mean_ms": 0.0, "p50_ms": 0.0, "p90_ms": 0.0}
    p50 = statistics.median(ordered)
    p90 = ordered[max(0, int(len(ordered) * 0.9) - 1)]
    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
        "mean_ms": round(sum(ordered) / len(ordered), 3),
        "p50_ms": round(p50, 3),
        "p90_ms": round(p90, 3),
    }
