from __future__ import annotations

import html
import json
import re
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifacts import load_locomo_test_artifacts

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
    duration_events = _build_step_duration_events(step_seconds)
    duration_events.extend(_build_session_diagnostic_duration_events(bundle.session_ingest_diagnostics))
    duration_events.extend(_build_qa_duration_events(qa_rows))
    for label, values in _group_event_durations(duration_events).items():
        duration_distributions[label] = _quantiles_ms(values)

    return {
        "run_id": meta.get("name", output_dir.name),
        "source": "locomo_test",
        "question_count": len(qa_rows),
        "duration_events": duration_events,
        "duration_distributions": duration_distributions,
        "stage_taxonomy": _build_stage_taxonomy(duration_events),
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


def _build_session_diagnostic_duration_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def append_once(event_payload: dict[str, Any]) -> None:
        key = (
            str(event_payload.get("label") or ""),
            str(event_payload.get("entity_id") or ""),
            str(event_payload.get("source") or ""),
        )
        if key in seen:
            return
        seen.add(key)
        events.append(event_payload)

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        event = str(row.get("event") or row.get("status") or "")
        session_key = str(row.get("session_key") or "")
        session_id = str(((row.get("ov_commit") or {}).get("session_id") if isinstance(row.get("ov_commit"), dict) else "") or "")
        entity_id = session_id or session_key or f"session_{index}"
        base_metadata = {
            "sample_id": row.get("sample_id"),
            "session_key": session_key,
            "session_id": session_id,
            "event": event,
            "query_mode": row.get("query_mode"),
        }
        accepted_seconds = _float_or_none(row.get("accepted_elapsed_seconds"))
        if accepted_seconds is not None:
            append_once(
                _duration_event(
                    label="agent.ingest.accepted_ms",
                    canonical_stage="agent.ingest.accept",
                    span_role="wrapper",
                    duration_ms=accepted_seconds * 1000.0,
                    scope="ingest_session",
                    entity_id=entity_id,
                    parent_id="stage:ingest",
                    depth=2,
                    source="session_ingest_diagnostics.accepted_elapsed_seconds",
                    metadata=base_metadata,
                )
            )
        send_payload = row.get("send") if isinstance(row.get("send"), dict) else {}
        send_seconds = _float_or_none(send_payload.get("elapsed_seconds") if isinstance(send_payload, dict) else None)
        if send_seconds is not None:
            append_once(
                _duration_event(
                    label="agent.ingest.send_ms",
                    canonical_stage="agent.ingest.send",
                    span_role="leaf",
                    duration_ms=send_seconds * 1000.0,
                    scope="ingest_session",
                    entity_id=entity_id,
                    parent_id="stage:ingest",
                    depth=2,
                    source="session_ingest_diagnostics.send.elapsed_seconds",
                    metadata={**base_metadata, "attempts": send_payload.get("attempts")},
                )
            )
        task_wait = row.get("ov_task_wait") if isinstance(row.get("ov_task_wait"), dict) else {}
        wait_seconds = _float_or_none(task_wait.get("elapsed_seconds") if isinstance(task_wait, dict) else None)
        if wait_seconds is not None:
            events.append(
                _duration_event(
                    label="ov.ingest.drain_wait_ms",
                    canonical_stage="extract.task_wait",
                    span_role="wrapper",
                    duration_ms=wait_seconds * 1000.0,
                    scope="ingest_session",
                    entity_id=entity_id,
                    parent_id=f"ingest_session:{entity_id}",
                    depth=3,
                    source="session_ingest_diagnostics.ov_task_wait.elapsed_seconds",
                    metadata={
                        **base_metadata,
                        "poll_count": task_wait.get("poll_count"),
                        "final_status": task_wait.get("final_status"),
                        "timed_out": task_wait.get("timed_out"),
                        "fallback_used": task_wait.get("fallback_used"),
                    },
                )
            )
        total_seconds = _float_or_none(row.get("session_total_elapsed_seconds"))
        if total_seconds is not None:
            events.append(
                _duration_event(
                    label="ov.ingest.session_total_ms",
                    canonical_stage="extract.session_total",
                    span_role="wrapper",
                    duration_ms=total_seconds * 1000.0,
                    scope="ingest_session",
                    entity_id=entity_id,
                    parent_id="stage:ingest",
                    depth=2,
                    source="session_ingest_diagnostics.session_total_elapsed_seconds",
                    metadata=base_metadata,
                )
            )
        consistency = row.get("ov_consistency") if isinstance(row.get("ov_consistency"), dict) else {}
        if consistency:
            events.append(
                {
                    "label": "ov.ingest.consistency_check",
                    "canonical_stage": "retrieve.consistency_check",
                    "span_role": "leaf",
                    "scope": "ingest_session",
                    "entity_id": entity_id,
                    "parent_id": f"ingest_session:{entity_id}",
                    "depth": 3,
                    "duration_ms": 0.0,
                    "source": "session_ingest_diagnostics.ov_consistency",
                    "metadata": {
                        **base_metadata,
                        "ok": consistency.get("ok"),
                        "missing_record_count": consistency.get("missing_record_count"),
                    },
                }
            )
    return events


def _build_step_duration_events(step_seconds: dict[str, float]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for key, seconds in sorted(step_seconds.items()):
        step = key.removesuffix("_seconds")
        events.append(
            _duration_event(
                label=f"pipeline.{step}_ms",
                canonical_stage=f"pipeline.{step}",
                span_role="wrapper",
                duration_ms=seconds * 1000.0,
                scope="run_step",
                entity_id=f"stage:{step}",
                parent_id="run",
                depth=1,
                source="pipeline.log.step_done",
                metadata={"step": step},
            )
        )
    return events


def _build_qa_duration_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    timestamp_fallbacks = _build_qa_timestamp_fallbacks(rows)
    for index, row in enumerate(rows, start=1):
        sample_id = str(row.get("sample_id") or "")
        qi = str(row.get("qi") or index)
        entity_id = f"{sample_id}:q{qi}" if sample_id else f"q{qi}"
        metadata = {
            "sample_id": sample_id,
            "qi": qi,
            "jsonl_filename": row.get("jsonl_filename"),
            "ov_direct_recall_count": row.get("ov_direct_recall_count"),
            "total_tokens": row.get("total_tokens"),
        }
        total_seconds = _float_or_none(row.get("qa_elapsed_seconds"))
        total_source = "qa_results.qa_elapsed_seconds"
        if total_seconds is None or total_seconds <= 0:
            total_seconds = timestamp_fallbacks.get(index)
            total_source = "qa_results.timestamp_delta_seconds"
        if total_seconds is not None and total_seconds > 0:
            events.append(
                _duration_event(
                    label="qa.session_total_ms",
                    canonical_stage="qa.session_total",
                    span_role="wrapper",
                    duration_ms=total_seconds * 1000.0,
                    scope="qa_session",
                    entity_id=entity_id,
                    parent_id="stage:qa",
                    depth=2,
                    source=total_source,
                    metadata={**metadata, "timing_precision": "exact" if total_source.endswith("qa_elapsed_seconds") else "timestamp_delta"},
                )
            )
        recall_seconds = _float_or_none(row.get("qa_direct_recall_elapsed_seconds"))
        if recall_seconds is not None and recall_seconds > 0:
            events.append(
                _duration_event(
                    label="qa.direct_recall_ms",
                    canonical_stage="retrieve.direct_recall",
                    span_role="leaf",
                    duration_ms=recall_seconds * 1000.0,
                    scope="qa_session",
                    entity_id=entity_id,
                    parent_id=f"qa_session:{entity_id}",
                    depth=3,
                    source="qa_results.qa_direct_recall_elapsed_seconds",
                    metadata=metadata,
                )
            )
        llm_seconds = _float_or_none(row.get("qa_llm_elapsed_seconds"))
        if llm_seconds is not None and llm_seconds > 0:
            events.append(
                _duration_event(
                    label="qa.llm_answer_ms",
                    canonical_stage="agent.qa.answer",
                    span_role="leaf",
                    duration_ms=llm_seconds * 1000.0,
                    scope="qa_session",
                    entity_id=entity_id,
                    parent_id=f"qa_session:{entity_id}",
                    depth=3,
                    source="qa_results.qa_llm_elapsed_seconds",
                    metadata=metadata,
                )
            )
    return events


def _build_qa_timestamp_fallbacks(rows: list[dict[str, Any]]) -> dict[int, float]:
    parsed: list[tuple[int, datetime]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        dt = _parse_qa_timestamp(str(row.get("timestamp") or ""))
        if dt is not None:
            parsed.append((index, dt))
    if len(parsed) < 2:
        return {}
    fallback: dict[int, float] = {}
    deltas: list[float] = []
    for (prev_index, prev_dt), (index, dt) in zip(parsed, parsed[1:]):
        delta = max((dt - prev_dt).total_seconds(), 0.0)
        if delta > 0:
            fallback[index] = delta
            deltas.append(delta)
    if deltas:
        fallback[parsed[0][0]] = statistics.median(deltas)
    return fallback


def _parse_qa_timestamp(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _duration_event(
    *,
    label: str,
    canonical_stage: str,
    span_role: str,
    duration_ms: float,
    scope: str,
    entity_id: str,
    parent_id: str,
    depth: int,
    source: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "label": label,
        "canonical_stage": canonical_stage,
        "span_role": span_role,
        "scope": scope,
        "entity_id": entity_id,
        "parent_id": parent_id,
        "depth": depth,
        "duration_ms": round(float(duration_ms), 3),
        "source": source,
        "metadata": metadata,
    }


def _group_event_durations(events: list[dict[str, Any]]) -> dict[str, list[float]]:
    grouped: dict[str, list[float]] = {}
    for event in events:
        try:
            duration = float(event.get("duration_ms", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if duration <= 0:
            continue
        grouped.setdefault(str(event.get("label") or "unknown"), []).append(duration)
    return grouped


def _build_stage_taxonomy(events: list[dict[str, Any]]) -> dict[str, Any]:
    leaf_labels = sorted({str(event.get("label")) for event in events if event.get("span_role") == "leaf"})
    wrapper_labels = sorted({str(event.get("label")) for event in events if event.get("span_role") == "wrapper"})
    return {
        "share_denominator": "leaf_only",
        "leaf_labels": leaf_labels,
        "wrapper_labels": wrapper_labels,
        "notes": [
            "Leaf stages can be used for non-overlapping stage share.",
            "Wrapper stages are diagnostic only and must not be added to leaf-stage totals.",
        ],
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def render_locomo_test_timing_html(report: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape("" if value is None else str(value))

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
    detail_rows = []
    for event in report.get("duration_events", []) or []:
        detail_rows.append(
            "<tr>"
            f"<td>{event.get('label', '')}</td>"
            f"<td>{event.get('canonical_stage', '')}</td>"
            f"<td>{event.get('span_role', '')}</td>"
            f"<td>{event.get('scope', '')}</td>"
            f"<td>{event.get('entity_id', '')}</td>"
            f"<td>{event.get('parent_id', '')}</td>"
            f"<td>{event.get('depth', '')}</td>"
            f"<td>{float(event.get('duration_ms', 0.0) or 0.0):.3f}</td>"
            f"<td>{event.get('source', '')}</td>"
            "</tr>"
        )
    taxonomy_json = html.escape(json.dumps(report.get("stage_taxonomy", {}), ensure_ascii=False, indent=2))
    distribution_chart = _render_duration_distribution_chart(report.get("duration_distributions", {}) or {})
    hierarchy_chart = _render_call_hierarchy_chart(report.get("duration_events", []) or [])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{report.get('run_id', 'locomo_test')} - Timing Report</title>
  <style>
    body {{ font-family: "Segoe UI", "PingFang SC", sans-serif; margin: 24px; color: #1f2937; background: #f8fafc; }}
    section {{ background: #fff; border: 1px solid #d9e2ec; border-radius: 14px; padding: 18px; margin-top: 18px; box-shadow: 0 8px 20px rgba(15, 23, 42, 0.04); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #d9e2ec; padding: 10px 12px; text-align: left; }}
    th {{ background: #f8fafc; }}
    .chart {{ width: 100%; height: auto; background: #f8fafc; border: 1px solid #e5edf5; border-radius: 10px; }}
    .bar-label {{ font-size: 11px; fill: #334155; }}
    .bar-value {{ font-size: 10px; fill: #64748b; }}
    .node {{ fill: #fff; stroke: #94a3b8; stroke-width: 1; }}
    .node.leaf {{ fill: #ecfdf5; stroke: #0f766e; }}
    .node.wrapper {{ fill: #eff6ff; stroke: #2563eb; }}
    .edge {{ stroke: #cbd5e1; stroke-width: 1.4; }}
    .node-text {{ font-size: 11px; fill: #1f2937; }}
    .muted {{ color: #64748b; }}
  </style>
</head>
<body>
  <h1>Timing Report</h1>
  <p>run_id={esc(report.get('run_id'))} source=locomo_test</p>
  <section>
  <h2>Duration Distribution Chart</h2>
  <div class="muted">按 duration label 展示 mean/p90/max，便于快速识别慢阶段。</div>
  {distribution_chart}
  </section>
  <section>
  <h2>Call Hierarchy</h2>
  <div class="muted">调用层级：run -> pipeline step -> session -> leaf operation。蓝色为 wrapper，绿色为 leaf。</div>
  {hierarchy_chart}
  </section>
  <section>
  <h2>Duration Summary</h2>
  <table>
    <thead><tr><th>Label</th><th>Count</th><th>Min(ms)</th><th>Max(ms)</th><th>Mean(ms)</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  </section>
  <section>
  <h2>Stage Taxonomy</h2>
  <pre>{taxonomy_json}</pre>
  </section>
  <section>
  <h2>Duration Events</h2>
  <table>
    <thead><tr><th>Label</th><th>Canonical Stage</th><th>Role</th><th>Scope</th><th>Entity</th><th>Parent</th><th>Depth</th><th>Duration(ms)</th><th>Source</th></tr></thead>
    <tbody>{''.join(detail_rows) or "<tr><td colspan='9'>No duration events</td></tr>"}</tbody>
  </table>
  </section>
</body>
</html>"""


def _render_duration_distribution_chart(distributions: dict[str, Any]) -> str:
    items: list[tuple[str, float, float, float]] = []
    for label, payload in distributions.items():
        if not isinstance(payload, dict):
            continue
        items.append(
            (
                str(label),
                float(payload.get("mean_ms", 0.0) or 0.0),
                float(payload.get("p90_ms", 0.0) or 0.0),
                float(payload.get("max_ms", 0.0) or 0.0),
            )
        )
    items = sorted(items, key=lambda item: item[3], reverse=True)[:18]
    if not items:
        return "<div class='muted'>No timing distribution data</div>"
    width = 1180
    row_h = 30
    left = 250
    right = 80
    top = 24
    height = top + len(items) * row_h + 20
    max_value = max([item[3] for item in items] + [1.0])
    chart_w = width - left - right
    rows = []
    for idx, (label, mean_ms, p90_ms, max_ms) in enumerate(items):
        y = top + idx * row_h
        max_w = (max_ms / max_value) * chart_w
        p90_w = (p90_ms / max_value) * chart_w
        mean_w = (mean_ms / max_value) * chart_w
        rows.append(
            f"<text class='bar-label' x='8' y='{y + 17}'>{html.escape(label[:42])}</text>"
            f"<rect x='{left}' y='{y + 6}' width='{max_w:.2f}' height='16' rx='8' fill='#dbeafe' />"
            f"<rect x='{left}' y='{y + 6}' width='{p90_w:.2f}' height='16' rx='8' fill='#93c5fd' />"
            f"<rect x='{left}' y='{y + 6}' width='{mean_w:.2f}' height='16' rx='8' fill='#2563eb' />"
            f"<text class='bar-value' x='{left + max_w + 6:.2f}' y='{y + 17}'>{_format_ms(max_ms)}</text>"
        )
    legend = (
        "<text class='bar-value' x='250' y='14'>mean=dark blue · p90=blue · max=light blue</text>"
    )
    return f"<svg viewBox='0 0 {width} {height}' class='chart'>{legend}{''.join(rows)}</svg>"


def _render_call_hierarchy_chart(events: list[dict[str, Any]]) -> str:
    if not events:
        return "<div class='muted'>No duration event hierarchy data</div>"
    scopes = {"run": {"label": "run", "depth": 0, "role": "wrapper", "parent": ""}}
    for event in events:
        parent = str(event.get("parent_id") or "")
        entity = str(event.get("entity_id") or "")
        scope = str(event.get("scope") or "")
        if parent.startswith("stage:"):
            scopes[parent] = {"label": parent.replace("stage:", "stage:"), "depth": 1, "role": "wrapper", "parent": "run"}
        if scope in {"ingest_session", "qa_session"} and entity:
            node_id = f"{scope}:{entity}"
            stage_parent = "stage:ingest" if scope == "ingest_session" else "stage:qa"
            scopes[node_id] = {
                "label": f"{scope}:{entity}",
                "depth": 2,
                "role": "wrapper",
                "parent": stage_parent,
            }
    leaves = []
    for idx, event in enumerate(events):
        label = str(event.get("label") or "event")
        parent = str(event.get("parent_id") or "run")
        if parent.startswith("ingest_session:") or parent.startswith("qa_session:"):
            parent_id = parent
        elif str(event.get("scope") or "") == "ingest_session":
            parent_id = f"ingest_session:{event.get('entity_id')}"
        elif str(event.get("scope") or "") == "qa_session":
            parent_id = f"qa_session:{event.get('entity_id')}"
        else:
            parent_id = parent
        leaves.append(
            {
                "id": f"event:{idx}",
                "label": label,
                "depth": int(event.get("depth") or 3),
                "role": str(event.get("span_role") or "leaf"),
                "parent": parent_id,
                "duration": float(event.get("duration_ms", 0.0) or 0.0),
            }
        )
    nodes = list(scopes.items()) + [(leaf["id"], leaf) for leaf in leaves[:60]]
    by_depth: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for node_id, node in nodes:
        by_depth.setdefault(int(node.get("depth", 0)), []).append((node_id, node))
    width = 1180
    col_w = 280
    row_h = 38
    positions: dict[str, tuple[float, float]] = {}
    max_rows = max((len(items) for items in by_depth.values()), default=1)
    height = max(220, max_rows * row_h + 60)
    for depth, depth_nodes in by_depth.items():
        for idx, (node_id, _node) in enumerate(depth_nodes):
            positions[node_id] = (20 + depth * col_w, 30 + idx * row_h)
    edges = []
    rects = []
    for node_id, node in nodes:
        x, y = positions[node_id]
        parent = str(node.get("parent") or "")
        if parent in positions:
            px, py = positions[parent]
            edges.append(
                f"<line class='edge' x1='{px + 220}' y1='{py + 13}' x2='{x}' y2='{y + 13}' />"
            )
        role = "leaf" if node.get("role") == "leaf" else "wrapper"
        label = html.escape(str(node.get("label") or "")[:30])
        duration = float(node.get("duration", 0.0) or 0.0)
        suffix = f" {_format_ms(duration)}" if duration > 0 else ""
        rects.append(
            f"<rect class='node {role}' x='{x}' y='{y}' width='220' height='26' rx='8' />"
            f"<text class='node-text' x='{x + 8}' y='{y + 17}'>{label}{html.escape(suffix)}</text>"
        )
    return f"<svg viewBox='0 0 {width} {height}' class='chart'>{''.join(edges)}{''.join(rects)}</svg>"


def _format_ms(value_ms: float) -> str:
    if value_ms >= 60000:
        return f"{value_ms / 60000:.1f}m"
    if value_ms >= 1000:
        return f"{value_ms / 1000:.1f}s"
    return f"{value_ms:.0f}ms"


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
