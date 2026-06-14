from pathlib import Path

from skills.benchmarks.locomo.scripts.build_tasks import build_tasks


def test_locomo_task_builder_reads_workspace_dataset():
    workspace_root = Path(__file__).resolve().parents[2]
    data_path = workspace_root / "locomo_test" / "data" / "locomo_small.json"
    payload = build_tasks(data_path)
    assert payload["cases"]
    assert payload["steps"]
    first_case = payload["cases"][0]
    first_step = payload["steps"][0]
    assert "case_id" in first_case
    assert "reference" in first_case
    assert "expected_answer" in first_case["reference"]
    assert first_step["case_id"] == first_case["case_id"]


def test_locomo_task_builder_reads_all_samples():
    workspace_root = Path(__file__).resolve().parents[2]
    data_path = workspace_root / "locomo_test" / "data" / "locomo10.json"
    payload = build_tasks(data_path)
    sample_ids = {case["source_metadata"]["sample_id"] for case in payload["cases"]}
    assert len(sample_ids) == 10
