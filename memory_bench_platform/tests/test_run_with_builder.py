import json
from pathlib import Path

from memory_bench_platform.cli import main


def test_run_uses_builder_and_agent_runner(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["run", "--benchmark", "locomo", "--agent", "generic-cli"])
    run_dir = next((tmp_path / "runs").iterdir())
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    cases_payload = json.loads((run_dir / "records" / "cases.json").read_text(encoding="utf-8"))
    steps_payload = json.loads((run_dir / "records" / "steps.json").read_text(encoding="utf-8"))
    step_results = json.loads((run_dir / "records" / "step_results.json").read_text(encoding="utf-8"))
    judge_results = json.loads((run_dir / "records" / "judge_results.json").read_text(encoding="utf-8"))
    metrics = json.loads((run_dir / "records" / "metrics.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert cases_payload
    assert steps_payload
    assert step_results
    assert judge_results
    assert metrics
    assert run_record["status"] in {"partial", "passed"}
    assert summary["case_total"] > 0
    assert summary["status"] in {"partial", "passed"}
