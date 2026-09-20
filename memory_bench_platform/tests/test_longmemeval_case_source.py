import json
from pathlib import Path

from skills.benchmarks.longmemeval.scripts.build_tasks import build_cases
from skills.benchmarks.longmemeval.scripts.build_scenario import build_scenario
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
    assert payload["steps"][0]["inputs"]["system_prompt"]
    assert len(payload["steps"][0]["inputs"]["messages"]) >= 2
    assert "2024-03-01" in payload["steps"][0]["inputs"]["messages"][0]["content"]
    assert payload["steps"][0]["inputs"]["messages"][-1]["content"] == "When did the user switch jobs?"

    validation = validate(data_path)
    assert validation["status"] == "ok"
    assert validation["has_haystack_sessions"] is True


def test_longmemeval_golden_scenario_matches_checked_fixture():
    golden = Path("skills/benchmarks/longmemeval/tests/golden")
    actual = build_scenario(golden / "source_sample.json")
    expected = json.loads((golden / "expected_scenario.json").read_text(encoding="utf-8"))

    assert actual == expected
    serialized = json.dumps(actual, ensure_ascii=False)
    assert "memory_integration" not in serialized
    assert "flush" not in serialized
    assert "wait_ready" not in serialized
    assert "openclaw" not in serialized.lower()
    assert "openviking" not in serialized.lower()


def test_native_dates_reach_ogmemory_as_iso(tmp_path):
    from skills.memories.ogmemory.scripts.run_operation import _created_at
    from skills.benchmarks.longmemeval.scripts.build_scenario import normalize_timestamp
    import pytest
    source = tmp_path / "native.json"
    source.write_text(json.dumps([{"question_id": "native", "question": "When?",
        "question_date": "2023/05/21 (Sun) 04:00",
        "haystack_dates": ["2023/05/20 (Sat) 02:21"],
        "haystack_sessions": [[{"role": "user", "content": "I moved today."}]]}]))
    event = build_scenario(source)["samples"][0]["timeline"][0]
    assert event["timestamp"] == "2023-05-20T02:21:00+00:00"
    assert _created_at(event["timestamp"]) == event["timestamp"]
    assert "2023/05/20 (Sat) 02:21" in event["payload"]["content"]
    assert normalize_timestamp("") is None
    assert normalize_timestamp("2024-03-01") == "2024-03-01"
    with pytest.raises(ValueError, match="LongMemEval timestamp"):
        normalize_timestamp("yesterday")
