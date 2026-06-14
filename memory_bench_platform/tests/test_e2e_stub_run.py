from pathlib import Path

from memory_bench_platform.cli import main


def test_stub_run_creates_run_json_and_summary(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(
        [
            "run",
            "--benchmark",
            "locomo",
            "--agent",
            "generic-cli",
        ]
    )
    runs = list((tmp_path / "runs").glob("*"))
    assert runs, "expected one run directory to be created"
    assert (runs[0] / "reports" / "summary.json").exists()
    assert (runs[0] / "reports" / "case_results.json").exists()
    assert (runs[0] / "reports").is_dir()
