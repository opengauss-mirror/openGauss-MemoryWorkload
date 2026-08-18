import json
from pathlib import Path

from skills.benchmarks.longmemeval.scripts.score_predictions import (
    export_hypotheses_from_case_results,
    get_anscheck_prompt,
)


def test_export_hypotheses_from_case_results_writes_jsonl(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "reports").mkdir(parents=True)
    (run_dir / "reports" / "case_results.json").write_text(
        json.dumps(
            [
                {
                    "case_id": "q-001",
                    "response": "answer one",
                },
                {
                    "case_id": "q-002",
                    "response": "answer two",
                },
            ]
        ),
        encoding="utf-8",
    )

    out_path = export_hypotheses_from_case_results(run_dir)
    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"question_id": "q-001", "hypothesis": "answer one"}
    assert json.loads(lines[1]) == {"question_id": "q-002", "hypothesis": "answer two"}


def test_get_anscheck_prompt_handles_temporal_reasoning():
    prompt = get_anscheck_prompt(
        "temporal-reasoning",
        "When did I switch jobs?",
        "In March 2024.",
        "You switched jobs in March 2024.",
    )
    assert "off-by-one errors" in prompt
    assert "When did I switch jobs?" in prompt

