from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any


EXPECTED_DURATION_LABELS = {
    "ov.commit.task.total_ms",
    "ov.commit.phase2.total_ms",
    "ov.session.commit.total_ms",
    "ov.session.window_ms",
    "ov.memory.extract.total_ms",
    "ov.memory.extract.stage.prepare_inputs_ms",
    "ov.memory.extract.stage.llm_extract_ms",
    "ov.memory.extract.stage.normalize_candidates_ms",
    "ov.memory.extract.stage.dedup_ms",
    "ov.memory.extract.stage.create_memory_ms",
    "ov.memory.extract.stage.merge_existing_ms",
    "ov.memory.extract.stage.delete_existing_ms",
    "ov.memory.extract.stage.create_relations_ms",
    "ov.memory.extract.stage.flush_semantic_ms",
    "ov.search.target_abstract_ms",
    "ov.search.intent_analysis_ms",
    "ov.search.embed_query_ms",
    "ov.search.vector_retrieval_ms",
    "agent.qa.total_ms",
}

RAW_DURATION_KEY_MAP = {
    "memory.extract.duration_ms": "ov.memory.extract.total_ms",
    "memory.extract.stages.prepare_inputs_ms": "ov.memory.extract.stage.prepare_inputs_ms",
    "memory.extract.stages.llm_extract_ms": "ov.memory.extract.stage.llm_extract_ms",
    "memory.extract.stages.normalize_candidates_ms": "ov.memory.extract.stage.normalize_candidates_ms",
    "memory.extract.stages.dedup_ms": "ov.memory.extract.stage.dedup_ms",
    "memory.extract.stages.create_memory_ms": "ov.memory.extract.stage.create_memory_ms",
    "memory.extract.stages.merge_existing_ms": "ov.memory.extract.stage.merge_existing_ms",
    "memory.extract.stages.delete_existing_ms": "ov.memory.extract.stage.delete_existing_ms",
    "memory.extract.stages.create_relations_ms": "ov.memory.extract.stage.create_relations_ms",
    "memory.extract.stages.flush_semantic_ms": "ov.memory.extract.stage.flush_semantic_ms",
    "search.target_abstract.duration_ms": "ov.search.target_abstract_ms",
    "search.intent_analysis.duration_ms": "ov.search.intent_analysis_ms",
    "search.embed_query.duration_ms": "ov.search.embed_query_ms",
    "search.vector_retrieval.duration_ms": "ov.search.vector_retrieval_ms",
}


def _load_meta(run_dir: Path) -> dict[str, Any]:
    candidates = sorted((run_dir / "external_artifacts" / "official_small").glob("phaseA*_meta.json"))
    if not candidates:
        raise FileNotFoundError(f"phaseA meta not found under {run_dir}")
    return json.loads(candidates[0].read_text(encoding="utf-8"))


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_task_timestamp(task: dict[str, Any], prefix: str) -> datetime | None:
    iso_value = task.get(f"{prefix}_at_iso")
    if isinstance(iso_value, str):
        parsed = _parse_iso8601(iso_value)
        if parsed:
            return parsed
    raw = task.get(prefix)
    try:
        if raw is not None:
            return datetime.fromtimestamp(float(raw))
    except (TypeError, ValueError, OSError):
        return None
    return None


def _quantiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "count": 0,
            "min_ms": 0.0,
            "max_ms": 0.0,
            "mean_ms": 0.0,
            "p50_ms": 0.0,
            "p90_ms": 0.0,
        }
    ordered = sorted(values)
    p50 = statistics.median(ordered)
    p90_index = max(0, math.ceil(len(ordered) * 0.9) - 1)
    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
        "mean_ms": round(sum(ordered) / len(ordered), 3),
        "p50_ms": round(p50, 3),
        "p90_ms": round(ordered[p90_index], 3),
    }


def _walk_duration_values(value: Any, prefix: str = "") -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_walk_duration_values(child, next_prefix))
        return rows
    if value is None:
        return rows
    if not (prefix.endswith("duration_ms") or prefix.endswith("_ms")):
        return rows
    try:
        rows.append((prefix, float(value)))
    except (TypeError, ValueError):
        return rows
    return rows


def _public_duration_label(raw_key: str, scope_prefix: str) -> str:
    if raw_key in RAW_DURATION_KEY_MAP:
        return RAW_DURATION_KEY_MAP[raw_key]
    return f"{scope_prefix}.{raw_key.removesuffix('.duration_ms').replace('.', '_')}_ms"


def _telemetry_summary_from_row(row: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        row.get("telemetry"),
        row.get("telemetry_summary"),
        row.get("ov_telemetry"),
        row.get("ov_telemetry_summary"),
        row.get("operation_telemetry"),
    ]
    ov_detail = ((row.get("ov_observation") or {}).get("detail") or {}) if isinstance(row.get("ov_observation"), dict) else {}
    candidates.extend(
        [
            ov_detail.get("telemetry"),
            ov_detail.get("telemetry_summary"),
        ]
    )
    for item in candidates:
        if isinstance(item, dict):
            summary = item.get("summary")
            if isinstance(summary, dict):
                return summary
            if "duration_ms" in item or "memory" in item or "search" in item:
                return item
    return {}


def _build_duration_events(meta: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in meta.get("ingest_sessions", []):
        session_idx = int(row.get("index", 0) or 0)
        compact_elapsed = float(row.get("compact_elapsed_seconds", 0.0) or 0.0)
        if compact_elapsed > 0:
            events.append(
                {
                    "label": "ov.session.commit.total_ms",
                    "scope": "ingest_session",
                    "entity_id": f"session_{session_idx}",
                    "duration_ms": round(compact_elapsed * 1000.0, 3),
                    "source": "phaseA_meta.compact_elapsed_seconds",
                    "metadata": {"session_index": session_idx},
                }
            )
        ov_detail = ((row.get("ov_observation") or {}).get("detail") or {}) if isinstance(row.get("ov_observation"), dict) else {}
        ov_task = ov_detail.get("_ov_task") if isinstance(ov_detail.get("_ov_task"), dict) else {}
        task_created = _parse_task_timestamp(ov_task, "created")
        task_updated = _parse_task_timestamp(ov_task, "updated")
        if task_created and task_updated:
            events.append(
                {
                    "label": "ov.commit.task.total_ms",
                    "scope": "ingest_session",
                    "entity_id": f"session_{session_idx}",
                    "duration_ms": round((task_updated - task_created).total_seconds() * 1000.0, 3),
                    "source": "phaseA_meta.ov_observation.detail._ov_task.created_at/updated_at",
                    "metadata": {
                        "session_index": session_idx,
                        "task_id": ov_task.get("task_id", ""),
                        "task_status": ov_task.get("status", ""),
                    },
                }
            )
        created_at = _parse_iso8601(ov_detail.get("created_at"))
        updated_at = _parse_iso8601(ov_detail.get("updated_at"))
        if created_at and updated_at:
            events.append(
                {
                    "label": "ov.session.window_ms",
                    "scope": "ingest_session",
                    "entity_id": f"session_{session_idx}",
                    "duration_ms": round((updated_at - created_at).total_seconds() * 1000.0, 3),
                    "source": "phaseA_meta.ov_observation.detail.created_at/updated_at",
                    "metadata": {"session_index": session_idx},
                }
            )
        telemetry_summary = _telemetry_summary_from_row(row)
        root_duration = telemetry_summary.get("duration_ms") if isinstance(telemetry_summary, dict) else None
        if root_duration is not None:
            try:
                events.append(
                    {
                        "label": "ov.commit.phase2.total_ms",
                        "scope": "ingest_session",
                        "entity_id": f"session_{session_idx}",
                        "duration_ms": round(float(root_duration), 3),
                        "source": "phaseA_meta.telemetry_summary.duration_ms",
                        "metadata": {
                            "session_index": session_idx,
                            "operation": telemetry_summary.get("operation", ""),
                            "status": telemetry_summary.get("status", ""),
                        },
                    }
                )
            except (TypeError, ValueError):
                pass
        for key, duration in _walk_duration_values(telemetry_summary):
            if key == "duration_ms":
                continue
            public_label = _public_duration_label(key, "ov")
            events.append(
                {
                    "label": public_label,
                    "scope": "ingest_session",
                    "entity_id": f"session_{session_idx}",
                    "duration_ms": round(duration, 3),
                    "source": "phaseA_meta.telemetry_summary",
                    "metadata": {"session_index": session_idx},
                }
            )

    for row in meta.get("qa_rows", []):
        qi = int(row.get("qi", 0) or 0)
        elapsed = float(row.get("elapsed_seconds", 0.0) or 0.0)
        if elapsed > 0:
            events.append(
                {
                    "label": "agent.qa.total_ms",
                    "scope": "qa_question",
                    "entity_id": f"q{qi}",
                    "duration_ms": round(elapsed * 1000.0, 3),
                    "source": "phaseA_meta.qa_rows.elapsed_seconds",
                    "metadata": {"qi": qi, "question": row.get("question", "")},
                }
            )
        telemetry_summary = _telemetry_summary_from_row(row)
        root_duration = telemetry_summary.get("duration_ms") if isinstance(telemetry_summary, dict) else None
        if root_duration is not None:
            try:
                events.append(
                    {
                        "label": "qa.operation.total_ms",
                        "scope": "qa_question",
                        "entity_id": f"q{qi}",
                        "duration_ms": round(float(root_duration), 3),
                        "source": "phaseA_meta.qa_rows.telemetry_summary.duration_ms",
                        "metadata": {
                            "qi": qi,
                            "operation": telemetry_summary.get("operation", ""),
                            "status": telemetry_summary.get("status", ""),
                        },
                    }
                )
            except (TypeError, ValueError):
                pass
        for key, duration in _walk_duration_values(telemetry_summary):
            if key == "duration_ms":
                continue
            public_label = _public_duration_label(key, "qa")
            events.append(
                {
                    "label": public_label,
                    "scope": "qa_question",
                    "entity_id": f"q{qi}",
                    "duration_ms": round(duration, 3),
                    "source": "phaseA_meta.qa_rows.telemetry_summary",
                    "metadata": {"qi": qi, "question": row.get("question", "")},
                }
            )
    return events


def _build_token_summary(meta: dict[str, Any]) -> dict[str, Any]:
    ingest_llm_total = 0
    ingest_embedding_total = 0
    for row in meta.get("ingest_sessions", []):
        ov_detail = ((row.get("ov_observation") or {}).get("detail") or {}) if isinstance(row.get("ov_observation"), dict) else {}
        llm_usage = row.get("ov_llm_token_usage") or ov_detail.get("llm_token_usage") or {}
        embedding_usage = ov_detail.get("embedding_token_usage") or {}
        ingest_llm_total += int((llm_usage or {}).get("total_tokens", 0) or 0)
        ingest_embedding_total += int((embedding_usage or {}).get("total_tokens", 0) or 0)

    qa_total = 0
    qa_input = 0
    qa_output = 0
    qa_cache_read = 0
    for row in meta.get("qa_rows", []):
        usage = row.get("usage") or {}
        qa_total += int(usage.get("total_tokens", 0) or 0)
        qa_input += int(usage.get("input_tokens", 0) or 0)
        qa_output += int(usage.get("output_tokens", 0) or 0)
        qa_cache_read += int(usage.get("cacheRead", 0) or 0)

    return {
        "ingest": {
            "ov_llm_total_tokens": ingest_llm_total,
            "ov_embedding_total_tokens": ingest_embedding_total,
        },
        "qa": {
            "total_tokens": qa_total,
            "input_tokens": qa_input,
            "output_tokens": qa_output,
            "cache_read_tokens": qa_cache_read,
        },
    }


def _build_wm_preprocess_summary(meta: dict[str, Any]) -> dict[str, Any]:
    sessions = meta.get("ingest_sessions", [])
    statuses = [str(((row.get("wm_preprocess") or {}).get("status") or "")) for row in sessions if row.get("wm_preprocess")]
    structured_facts_total = 0
    selected_span_count_total = 0
    selected_span_tokens_total = 0
    for row in sessions:
        metrics = ((row.get("wm_preprocess") or {}).get("metrics") or {})
        structured_facts_total += int(((row.get("wm_preprocess") or {}).get("structured_facts_count") or 0))
        selected_span_count_total += int(metrics.get("selected_span_count", 0) or 0)
        selected_span_tokens_total += int(metrics.get("selected_span_tokens_est", 0) or 0)
    return {
        "session_count": len(sessions),
        "statuses": statuses,
        "structured_facts_total": structured_facts_total,
        "selected_span_count_total": selected_span_count_total,
        "selected_span_tokens_total": selected_span_tokens_total,
    }


def build_official_small_timing_report(run_dir: Path) -> dict[str, Any]:
    meta = _load_meta(run_dir)
    events = _build_duration_events(meta)
    by_label: dict[str, list[float]] = {}
    for event in events:
        by_label.setdefault(event["label"], []).append(float(event["duration_ms"]))

    distributions = {label: _quantiles(values) for label, values in sorted(by_label.items())}
    available_labels = set(distributions.keys())
    missing_labels = sorted(EXPECTED_DURATION_LABELS - available_labels)
    return {
        "run_id": meta.get("run_id", run_dir.name),
        "source": "official_small_phaseA_meta",
        "generated_at": datetime.now().isoformat(),
        "question_count": len(meta.get("qa_rows", [])),
        "ingest_session_count": len(meta.get("ingest_sessions", [])),
        "duration_events": events,
        "duration_distributions": distributions,
        "token_summary": _build_token_summary(meta),
        "wm_preprocess_summary": _build_wm_preprocess_summary(meta),
        "availability": {
            "available_duration_labels": sorted(available_labels),
            "missing_duration_labels": missing_labels,
            "notes": [
                "当前 official_small 默认稳定可得：session commit 总时长、OV session window、QA 总时长、token 用量。",
                "更细粒度的 OpenViking telemetry duration 只有在 runner/meta 显式带出 summary 时才会出现在报告中。",
            ],
        },
    }


def render_official_small_timing_html(report: dict[str, Any]) -> str:
    cards = [
        ("Run ID", report.get("run_id", "")),
        ("Ingest Sessions", str(report.get("ingest_session_count", 0))),
        ("QA Questions", str(report.get("question_count", 0))),
        ("Duration Labels", str(len(report.get("duration_distributions", {})))),
        ("Missing Labels", str(len(((report.get("availability") or {}).get("missing_duration_labels") or [])))),
    ]

    summary_rows = []
    for label, stats in sorted((report.get("duration_distributions") or {}).items()):
        summary_rows.append(
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{stats.get('count', 0)}</td>"
            f"<td>{stats.get('min_ms', 0.0):.3f}</td>"
            f"<td>{stats.get('p50_ms', 0.0):.3f}</td>"
            f"<td>{stats.get('mean_ms', 0.0):.3f}</td>"
            f"<td>{stats.get('p90_ms', 0.0):.3f}</td>"
            f"<td>{stats.get('max_ms', 0.0):.3f}</td>"
            "</tr>"
        )

    detail_rows = []
    for event in report.get("duration_events", []):
        detail_rows.append(
            "<tr>"
            f"<td>{event.get('label', '')}</td>"
            f"<td>{event.get('scope', '')}</td>"
            f"<td>{event.get('entity_id', '')}</td>"
            f"<td>{float(event.get('duration_ms', 0.0)):.3f}</td>"
            f"<td>{event.get('source', '')}</td>"
            f"<td><pre>{json.dumps(event.get('metadata', {}), ensure_ascii=False)}</pre></td>"
            "</tr>"
        )

    missing_items = "".join(
        f"<li>{label}</li>" for label in ((report.get("availability") or {}).get("missing_duration_labels") or [])
    )
    note_items = "".join(
        f"<li>{note}</li>" for note in ((report.get("availability") or {}).get("notes") or [])
    )
    token_json = json.dumps(report.get("token_summary", {}), ensure_ascii=False, indent=2)
    preprocess_json = json.dumps(report.get("wm_preprocess_summary", {}), ensure_ascii=False, indent=2)

    card_html = "".join(
        f"<div class='card'><div class='label'>{label}</div><div class='value'>{value}</div></div>"
        for label, value in cards
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Timing Report - {report.get('run_id', '')}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; color: #1b1f23; background: #f7f8fa; }}
    h1, h2 {{ margin: 0 0 12px 0; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 16px 0 24px; }}
    .card {{ background: #fff; border: 1px solid #d8dee4; border-radius: 10px; padding: 14px; }}
    .label {{ color: #57606a; font-size: 12px; margin-bottom: 6px; text-transform: uppercase; }}
    .value {{ font-size: 24px; font-weight: 600; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; margin: 12px 0 24px; }}
    th, td {{ border: 1px solid #d8dee4; padding: 8px 10px; text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ background: #f0f4f8; }}
    pre {{ white-space: pre-wrap; margin: 0; font-size: 12px; }}
    .section {{ margin-bottom: 24px; }}
    ul {{ margin: 8px 0 0 20px; }}
  </style>
</head>
<body>
  <h1>Timing Report</h1>
  <div>run_id: <code>{report.get('run_id', '')}</code></div>
  <div class="grid">{card_html}</div>

  <div class="section">
    <h2>Duration Summary</h2>
    <table>
      <thead>
        <tr><th>Label</th><th>Count</th><th>Min (ms)</th><th>P50 (ms)</th><th>Mean (ms)</th><th>P90 (ms)</th><th>Max (ms)</th></tr>
      </thead>
      <tbody>{''.join(summary_rows)}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>Token Summary</h2>
    <pre>{token_json}</pre>
  </div>

  <div class="section">
    <h2>WM Preprocess Summary</h2>
    <pre>{preprocess_json}</pre>
  </div>

  <div class="section">
    <h2>Unavailable Timing Labels</h2>
    <ul>{missing_items}</ul>
    <ul>{note_items}</ul>
  </div>

  <div class="section">
    <h2>Duration Details</h2>
    <table>
      <thead>
        <tr><th>Label</th><th>Scope</th><th>Entity</th><th>Duration (ms)</th><th>Source</th><th>Metadata</th></tr>
      </thead>
      <tbody>{''.join(detail_rows)}</tbody>
    </table>
  </div>
</body>
</html>
"""
