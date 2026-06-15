import json
from pathlib import Path

from skills.benchmarks.locomo.scripts.score_predictions import (
    export_locomo_case_results_to_csv,
    get_locomo_prompt,
)


def test_export_locomo_case_results_to_csv_writes_expected_columns(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "reports").mkdir(parents=True)
    (run_dir / "reports" / "case_results.json").write_text(
        json.dumps(
            [
                {
                    "case_id": "conv-1-q1",
                    "question": "When did Alice go?",
                    "expected_answer": "7 May 2023",
                    "response": "Yesterday relative to 8 May 2023.",
                    "category": "2",
                }
            ]
        ),
        encoding="utf-8",
    )
    out_path = export_locomo_case_results_to_csv(run_dir)
    text = out_path.read_text(encoding="utf-8")
    assert "question,expected,response,category" in text
    assert "When did Alice go?" in text


def test_get_locomo_prompt_mentions_relative_dates_are_acceptable():
    prompt = get_locomo_prompt(
        "When did Alice go?",
        "7 May 2023",
        "Yesterday relative to 8 May 2023.",
    )
    assert "relative time references" in prompt
    assert "same date" in prompt
