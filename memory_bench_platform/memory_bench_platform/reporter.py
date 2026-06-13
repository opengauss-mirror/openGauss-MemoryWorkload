from __future__ import annotations

import json
from pathlib import Path


def write_summary(run_dir: Path, summary: dict) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    (reports_dir / "summary.json").write_text(payload, encoding="utf-8")
