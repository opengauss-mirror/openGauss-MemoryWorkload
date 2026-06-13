from pathlib import Path

from skills.benchmarks.locomo.scripts.build_tasks import build_tasks


def test_locomo_task_builder_reads_workspace_dataset():
    workspace_root = Path(__file__).resolve().parents[2]
    data_path = workspace_root / "locomo_test" / "data" / "locomo_small.json"
    payload = build_tasks(data_path)
    assert payload["tasks"]
    first = payload["tasks"][0]
    assert "task_id" in first
    assert "question" in first
    assert "expected_answer" in first
