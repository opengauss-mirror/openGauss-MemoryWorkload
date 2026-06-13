import json
from pathlib import Path

from memory_bench_platform.cli import main


def test_run_uses_builder_and_agent_runner(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["run", "--benchmark", "locomo", "--agent", "generic-cli"])
    run_dir = next((tmp_path / "runs").iterdir())
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    tasks_payload = json.loads((run_dir / "records" / "tasks.json").read_text(encoding="utf-8"))
    agent_output = json.loads((run_dir / "artifacts" / "agent-output.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert tasks_payload["tasks"]
    assert agent_output["status"] == "ok"
    assert run_record["status"] == "partial"
    assert summary["task_count"] > 0
    assert summary["status"] == "partial"
