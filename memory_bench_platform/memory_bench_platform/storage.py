from __future__ import annotations

from pathlib import Path

from .protocol import RunRecord


class RunStorage:
    def __init__(self, runs_root: Path):
        self.runs_root = runs_root

    def init_run(self, run: RunRecord) -> Path:
        run_dir = self.runs_root / run.run_id
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (run_dir / "records").mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        (run_dir / "reports").mkdir(parents=True, exist_ok=True)
        (run_dir / "config_snapshot").mkdir(parents=True, exist_ok=True)
        self.write_run_record(run_dir, run)
        return run_dir

    def write_run_record(self, run_dir: Path, run: RunRecord) -> None:
        (run_dir / "run.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
