from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LocomoTestArtifacts:
    output_dir: Path
    meta: dict[str, Any] = field(default_factory=dict)
    qa_diagnostics: dict[str, Any] = field(default_factory=dict)
    ingest_record: dict[str, Any] = field(default_factory=dict)
    qa_rows: list[dict[str, str]] = field(default_factory=list)
    pipeline_log: str = ""
    chunk_diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def artifact_paths(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        for key, path in {
            "meta_json": self.output_dir / "meta.json",
            "qa_diagnostics_json": self.output_dir / "qa_diagnostics.json",
            "qa_results_csv": self.output_dir / "qa_results.csv",
            "pipeline_log": self.output_dir / "pipeline.log",
            "ingest_record_json": self.output_dir / ".ingest_record.json",
            "chunk_diagnostics_jsonl": self.output_dir / "chunk_diagnostics.jsonl",
            "report_html": self.output_dir / "report.html",
        }.items():
            if path.exists():
                payload[key] = str(path)
        return payload


def load_locomo_test_artifacts(output_dir: Path) -> LocomoTestArtifacts:
    output_dir = Path(output_dir)
    return LocomoTestArtifacts(
        output_dir=output_dir,
        meta=_load_optional_json(output_dir / "meta.json"),
        qa_diagnostics=_load_optional_json(output_dir / "qa_diagnostics.json"),
        ingest_record=_load_optional_json(output_dir / ".ingest_record.json"),
        qa_rows=_load_csv_rows(output_dir / "qa_results.csv"),
        pipeline_log=(output_dir / "pipeline.log").read_text(encoding="utf-8", errors="ignore")
        if (output_dir / "pipeline.log").exists()
        else "",
        chunk_diagnostics=_load_jsonl_rows(output_dir / "chunk_diagnostics.jsonl"),
    )


def _load_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows
