from __future__ import annotations

import json
from pathlib import Path


def write_summary(run_dir: Path, summary: dict) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    (reports_dir / "summary.json").write_text(payload, encoding="utf-8")


def write_case_results(run_dir: Path, case_results: list[dict]) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(case_results, ensure_ascii=False, indent=2)
    (reports_dir / "case_results.json").write_text(payload, encoding="utf-8")


def write_external_result_summary(run_dir: Path, imported: dict) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(imported, ensure_ascii=False, indent=2)
    (reports_dir / "external_result_summary.json").write_text(payload, encoding="utf-8")


def write_analysis_json(run_dir: Path, analysis: dict) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(analysis, ensure_ascii=False, indent=2)
    (reports_dir / "analysis.json").write_text(payload, encoding="utf-8")


def write_analysis_markdown(run_dir: Path, markdown_text: str) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "analysis.md").write_text(markdown_text, encoding="utf-8")


def write_timing_report_json(run_dir: Path, payload: dict) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "timing_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_timing_report_html(run_dir: Path, html_text: str) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "timing_report.html").write_text(html_text, encoding="utf-8")


def write_run_report_html(run_dir: Path, html_text: str) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "run_report.html").write_text(html_text, encoding="utf-8")
