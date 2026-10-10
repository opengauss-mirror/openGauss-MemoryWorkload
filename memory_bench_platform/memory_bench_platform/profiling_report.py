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
             f"Trace validity: `{summary['trace']['validity']}`", "", "## Phases", ""]
    for name, phase in summary["phases"].items():
        lines.append(f"- `{name}`: {phase['count']} requests, {phase['qps']:.3f} QPS")
    lines.extend(["", "## Hardware", ""])
    for name, hardware in summary["hardware"].items():
        lines.append(f"- `{name}`: {hardware['state']}" +
                     (f" ({hardware['reason']})" if hardware.get("reason") else ""))
    (reports / "profiling_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    body = "<html><meta charset='utf-8'><title>Profiling Analysis</title><body><h1>Profiling Analysis</h1>"
    body += "<h2>Phases</h2><ul>" + "".join(
        f"<li>{html.escape(name)}: {phase['count']} requests, {phase['qps']:.3f} QPS</li>"
        for name, phase in summary["phases"].items()) + "</ul>"
    body += "<h2>Hardware</h2><ul>" + "".join(
        f"<li>{html.escape(name)}: {html.escape(data['state'])}</li>"
        for name, data in summary["hardware"].items()) + "</ul></body></html>"
    (reports / "profiling_analysis.html").write_text(body, encoding="utf-8")
    return {format_: f"reports/profiling_analysis.{format_}" for format_ in ("json", "md", "html")}
