"""Pipeline orchestration — run steps in sequence with skip/only/resume support."""

from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

from .config import Config
from .checks import check_health, check_qa_results, check_judge_results, report_issues
from .eval import run_ingest, run_qa
from .judge import run_judge
from .stats import run_stats


class _TeeStream(io.TextIOBase):
    """Write to both the original stream and a log file."""

    def __init__(self, original: io.TextIOBase, log_file: io.TextIOBase):
        self._original = original
        self._log = log_file

    def write(self, s: str) -> int:
        self._original.write(s)
        self._log.write(s)
        self._log.flush()
        return len(s)

    def flush(self):
        self._original.flush()
        self._log.flush()


STEP_ORDER = ["health_check", "ingest", "qa", "judge", "stats"]


def _lock_path(cfg: Config, output_dir: str) -> Path:
    if cfg.run_lock_dir:
        return Path(cfg.run_lock_dir) / f"{cfg.name or 'locomo-test'}.lock"
    return Path(output_dir) / ".run.lock"


def ensure_run_lock(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        raise RuntimeError(f"existing run lock: {lock_path}")
    lock_path.write_text(str(os.getpid()), encoding="utf-8")


def release_run_lock(lock_path: Path) -> None:
    if lock_path.exists():
        lock_path.unlink()


def resolve_output_dir(cfg: Config) -> str:
    """Create and return the output directory for this run."""
    run_id = cfg.name or time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(cfg.output_dir, run_id)
    os.makedirs(out, exist_ok=True)
    return out


DATASET_MAP = {
    "small": "data/locomo_small.json",
    "locomo10": "data/locomo10.json",
}


def resolve_data_file(cfg: Config) -> str:
    """Resolve dataset name to actual file path."""
    if cfg.data_file:
        return cfg.data_file
    if cfg.dataset in DATASET_MAP:
        path = DATASET_MAP[cfg.dataset]
        if os.path.exists(path):
            return path
        print(f"Error: dataset '{cfg.dataset}' maps to {path} but file not found", file=sys.stderr)
        sys.exit(1)
    print(f"Error: unknown dataset '{cfg.dataset}'. Available: {', '.join(DATASET_MAP.keys())}", file=sys.stderr)
    print(f"  Or set data_file in [general] to an absolute path.", file=sys.stderr)
    sys.exit(1)


def run_pipeline(
    cfg: Config,
    only: list[str] | None = None,
    skip: list[str] | None = None,
    resume: bool = False,
):
    """Execute the pipeline with step control."""
    skip = set(skip or [])
    if resume:
        skip.update(["health_check", "ingest"])

    # Determine which steps to run
    active = []
    for step in STEP_ORDER:
        enabled = getattr(cfg.steps, step, True)
        if only:
            enabled = step in only
        if step in skip:
            enabled = False
        active.append((step, enabled))

    # Resolve paths
    output_dir = resolve_output_dir(cfg)
    cfg.data_file = resolve_data_file(cfg)
    lock_path = _lock_path(cfg, output_dir)
    ensure_run_lock(lock_path)

    # Tee stderr to log file
    log_path = os.path.join(output_dir, "pipeline.log")
    log_file = open(log_path, "a", encoding="utf-8")
    sys.stderr = _TeeStream(sys.stderr, log_file)

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  locomo-test-kit pipeline", file=sys.stderr)
    print(f"  name:    {cfg.name}", file=sys.stderr)
    print(f"  dataset: {cfg.data_file}", file=sys.stderr)
    print(f"  policy:  {cfg.session.policy.value}", file=sys.stderr)
    print(f"  output:  {output_dir}", file=sys.stderr)
    print(f"  steps:   {', '.join(s for s, e in active if e)}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    memory_token_totals: dict | None = None

    try:
        for step, enabled in active:
            if not enabled:
                continue

            print(f"\n--- Step: {step} ---", file=sys.stderr)
            t0 = time.time()

            if step == "health_check":
                ok = check_health(cfg)
                if not ok:
                    print("Health check failed. Are services running?", file=sys.stderr)
                    sys.exit(1)

            elif step == "ingest":
                _, memory_token_totals = run_ingest(cfg, output_dir)

            elif step == "qa":
                run_qa(cfg, output_dir)
                issues = check_qa_results(output_dir)
                report_issues("qa", issues)

            elif step == "judge":
                run_judge(cfg, output_dir)
                issues = check_judge_results(output_dir)
                report_issues("judge", issues)

            elif step == "stats":
                run_stats(cfg, output_dir, memory_token_totals=memory_token_totals)

            elapsed = time.time() - t0
            print(f"  [{step}] done in {elapsed:.1f}s", file=sys.stderr)

        print(f"\n{'='*60}", file=sys.stderr)
        print(f"  Pipeline complete. Output: {output_dir}", file=sys.stderr)
        print(f"  Log: {log_path}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
    finally:
        release_run_lock(lock_path)

    # Restore stderr
    sys.stderr = sys.stderr._original if isinstance(sys.stderr, _TeeStream) else sys.stderr
    log_file.close()
