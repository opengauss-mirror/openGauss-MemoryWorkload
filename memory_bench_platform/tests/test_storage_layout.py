from pathlib import Path

from memory_bench_platform.protocol import RunRecord
from memory_bench_platform.storage import RunStorage


def test_run_storage_creates_expected_layout(tmp_path: Path):
    storage = RunStorage(tmp_path)
    run = RunRecord(
        run_id="run-001",
        source_id="locomo",
        source_kind="benchmark_case_source",
        operator_targets=["openclaw"],
        status="pending",
    )
    run_dir = storage.init_run(run)
    assert (run_dir / "run.json").exists()
    assert (run_dir / "artifacts").is_dir()
    assert (run_dir / "records").is_dir()
    assert (run_dir / "logs").is_dir()
    assert (run_dir / "reports").is_dir()
    assert (run_dir / "config_snapshot").is_dir()
