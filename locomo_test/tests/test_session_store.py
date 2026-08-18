import json

from locomo_test.session_store import (
    calculate_usage_from_jsonl,
    load_ingest_record,
    mark_ingested,
)


def test_load_ingest_record_returns_empty_dict_for_missing_file(tmp_path):
    assert load_ingest_record(str(tmp_path / "missing.json")) == {}


def test_mark_ingested_records_success_and_meta():
    record = {}
    mark_ingested("agent-1", "user-1", "sample-1", "session_1", record, {"accepted": True})

    payload = record["agent-1:user-1:sample-1:session_1"]
    assert payload["success"] is True
    assert payload["meta"]["accepted"] is True
    assert isinstance(payload["timestamp"], int)


def test_calculate_usage_from_jsonl_accumulates_assistant_usage(tmp_path):
    jsonl_path = tmp_path / "session.jsonl"
    rows = [
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "usage": {"input_tokens": 3, "output_tokens": 2, "cacheRead": 1, "cacheWrite": 0, "total_tokens": 5},
            },
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "cache_read": 2, "cache_write": 1, "total_tokens": 5},
            },
        },
        {"type": "message", "message": {"role": "user", "usage": {"total_tokens": 99}}},
    ]
    jsonl_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows), encoding="utf-8")

    usage = calculate_usage_from_jsonl(str(jsonl_path))

    assert usage == {
        "input_tokens": 7,
        "output_tokens": 3,
        "cacheRead": 3,
        "cacheWrite": 1,
        "total_tokens": 10,
    }
