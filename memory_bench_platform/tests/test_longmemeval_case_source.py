import json
from pathlib import Path

from skills.benchmarks.longmemeval.scripts.build_tasks import build_cases
from skills.benchmarks.longmemeval.scripts.validate import validate


def test_longmemeval_case_source_parses_official_shape(tmp_path: Path):
    data_path = tmp_path / "longmemeval_sample.json"
    sample = [
        {
            "question_id": "q-001",
            "question_type": "temporal-reasoning",
            "question": "When did the user switch jobs?",
            "answer": "In March 2024.",
            "question_date": "2024-04-01",
            "haystack_session_ids": ["s1", "s2"],
            "haystack_dates": ["2024-03-01", "2024-03-20"],
            "haystack_sessions": [
                [{"role": "user", "content": "I switched jobs in March 2024."}]
            ],
            "answer_session_ids": ["s1"],
        }
    ]
    data_path.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")

    payload = build_cases(data_path)
    assert payload["cases"][0]["case_id"] == "q-001"
    assert payload["cases"][0]["reference"]["expected_answer"] == "In March 2024."
    assert payload["steps"][0]["step_id"] == "q-001-agent-query"

    validation = validate(data_path)
    assert validation["status"] == "ok"
    assert validation["has_haystack_sessions"] is True
