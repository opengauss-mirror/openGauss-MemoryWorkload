"""Content-free profiling report files."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path


def _csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _percentile(ordered: list[float], fraction: float) -> float:
    rank = (len(ordered) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def write_profiling_reports(run_dir: Path, summary: dict) -> dict[str, str]:
    reports = run_dir / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "join_coverage.json").write_text(json.dumps(summary["trace"]["join_coverage"],
        ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    stages = []
    for operation, categories in summary["trace"]["stage_durations_ms"].items():
        for category, durations in categories.items():
            values = sorted(durations)
            stages.append({"operation": operation, "category": category,
                           "count": len(values), "mean_ms": sum(values) / len(values),
                           "p50_ms": _percentile(values, 0.50),
                           "p95_ms": _percentile(values, 0.95),
                           "p99_ms": _percentile(values, 0.99)})
    _csv(reports / "stage_summary.csv",
         ["operation", "category", "count", "mean_ms", "p50_ms", "p95_ms", "p99_ms"], stages)
    _csv(reports / "attribution_residuals.csv",
         ["request_id", "operation", "unattributed_ms", "client_service_gap_ms"],
         summary["trace"]["residuals"])
    (reports / "profiling_analysis.json").write_text(json.dumps(summary, ensure_ascii=False,
        indent=2, sort_keys=True), encoding="utf-8")
    lines = ["# Profiling Analysis", "", f"Run: `{summary['run_id']}`", "",
             f"Model mode: `{summary['model_mode']}`", "",
             f"Trace validity: `{summary['trace']['validity']}`", "", "## Overall", ""]
    for name, phase in summary["overall"].items():
        lines.append(f"- `{name}`: {phase['count']} requests, {phase['qps']:.3f} QPS, p95 {phase['p95_ms']:.3f} ms")
    lines.extend(["", "## Phases", ""])
    for name, phase in summary["phases"].items():
        lines.append(f"- `{name}`: {phase['count']} requests, {phase['qps']:.3f} QPS, p95 {phase['p95_ms']:.3f} ms")
        for user, metrics in phase["users"].items():
            lines.append(f"  - user `{user}`: {metrics['count']} requests, p95 {metrics['p95_ms']:.3f} ms")
    lines.extend(["", "## Hardware", ""])
    for name, hardware in summary["hardware"].items():
        lines.append(f"- `{name}`: {hardware['state']}" +
                     (f" ({hardware['reason']})" if hardware.get("reason") else ""))
        if hardware["state"] == "completed":
            lines.append(f"  - IPC: {hardware.get('ipc', 'unavailable')}; " +
                         ", ".join(f"{key}: {value}" for key, value in hardware["counters"].items()))
            if hardware.get("topdown_valid"):
                lines.append("  - Topdown L1: " +
                             ", ".join(f"{key}: {value}" for key, value in hardware["topdown_l1"].items()))
    lines.extend(["", "## Attribution", "",
                  f"Matched requests: {summary['trace']['join_coverage']['matched']}; "
                  f"missing: {summary['trace']['join_coverage']['missing']}; "
                  f"duplicates: {summary['trace']['join_coverage']['duplicates']}; "
                  f"extra: {summary['trace']['join_coverage']['extra']}", "",
                  f"Collector coverage: {summary['collector_coverage']['completed']}/{summary['collector_coverage']['measured']}", "",
                  "## Internal stages", ""])
    for row in stages:
        lines.append(f"- `{row['operation']}/{row['category']}`: p95 {row['p95_ms']:.3f} ms")
    lines.extend(["",
                  "## Candidate bottlenecks", ""])
    for candidate in summary["bottleneck_candidates"]:
        lines.append(f"- `{candidate['operation']}/{candidate['stage']}`: {candidate['confidence']}")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in summary["limitations"])
    (reports / "profiling_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    body = "<html><meta charset='utf-8'><title>Profiling Analysis</title><body><h1>Profiling Analysis</h1>"
    body += "<p>Model mode: " + html.escape(summary["model_mode"]) + "</p>"
    body += "<h2>Overall</h2><ul>" + "".join(
        f"<li>{html.escape(name)}: {phase['count']} requests, {phase['qps']:.3f} QPS, p95 {phase['p95_ms']:.3f} ms</li>"
        for name, phase in summary["overall"].items()) + "</ul>"
    body += "<h2>Phases</h2><ul>" + "".join(
        f"<li>{html.escape(name)}: {phase['count']} requests, {phase['qps']:.3f} QPS, p95 {phase['p95_ms']:.3f} ms"
        + ("<ul>" + "".join(f"<li>{html.escape(user)}: {item['count']} requests, p95 {item['p95_ms']:.3f} ms</li>"
                             for user, item in phase["users"].items()) + "</ul>" if phase["users"] else "") + "</li>"
        for name, phase in summary["phases"].items()) + "</ul>"
    body += "<h2>Hardware</h2><ul>" + "".join(
        f"<li>{html.escape(name)}: {html.escape(data['state'])}" +
        ("; IPC " + str(data.get("ipc", "unavailable")) + "; " +
         ", ".join(html.escape(key) + " " + str(value) for key, value in data["counters"].items())
         if data["state"] == "completed" else "") + "</li>"
        for name, data in summary["hardware"].items()) + "</ul>"
    coverage = summary["trace"]["join_coverage"]
    body += ("<h2>Attribution</h2><p>Matched: " + str(coverage["matched"]) +
             "; missing: " + str(coverage["missing"]) +
             "; duplicates: " + str(coverage["duplicates"]) +
             "; extra: " + str(coverage["extra"]) + "</p>")
    body += ("<p>Collector coverage: " + str(summary["collector_coverage"]["completed"]) +
             "/" + str(summary["collector_coverage"]["measured"]) + "</p>")
    body += "<h2>Internal stages</h2><ul>" + "".join(
        f"<li>{html.escape(row['operation'])}/{html.escape(row['category'])}: p95 {row['p95_ms']:.3f} ms</li>"
        for row in stages) + "</ul>"
    body += "<h2>Candidate bottlenecks</h2><ul>" + "".join(
        f"<li>{html.escape(item['operation'])}/{html.escape(item['stage'])}: {html.escape(item['confidence'])}</li>"
        for item in summary["bottleneck_candidates"]) + "</ul>"
    body += "<h2>Limitations</h2><ul>" + "".join(
        f"<li>{html.escape(item)}</li>" for item in summary["limitations"]) + "</ul></body></html>"
    (reports / "profiling_analysis.html").write_text(body, encoding="utf-8")
    return {format_: f"reports/profiling_analysis.{format_}" for format_ in ("json", "md", "html")}
