"""Pure helpers describing remote reset intent for LoCoMo benchmark runs."""

from __future__ import annotations

from pathlib import Path


def build_reset_plan(run_id: str, backup_root: str = "/tmp") -> dict[str, str]:
    backup_dir = Path(backup_root)
    return {
        "run_id": run_id,
        "backup_path": str(backup_dir / f"{run_id}_backup.tar.gz"),
        "ov_data_dir": "/root/.openviking/data",
        "openclaw_agent_dir": "/root/.openclaw/agents/locomo-eval",
        "gateway_config": "/root/.openclaw/openclaw.json",
        "openviking_config": "/root/.openviking/ov.conf",
    }
