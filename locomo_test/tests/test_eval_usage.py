import csv
import json

from locomo_test.eval import (
    _normalize_usage,
    calculate_usage_from_jsonl,
    derive_ov_closure_status,
    save_record_to_csv,
)


def test_normalize_usage_supports_openai_and_openclaw_shapes():
    assert _normalize_usage(
        {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    ) == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cacheRead": 0,
        "cacheWrite": 0,
        "total_tokens": 18,
    }

    assert _normalize_usage(
        {"input": 5, "output": 3, "cacheRead": 2, "cacheWrite": 1, "totalTokens": 8}
    ) == {
        "input_tokens": 5,
        "output_tokens": 3,
        "cacheRead": 2,
        "cacheWrite": 1,
        "total_tokens": 8,
    }


def test_calculate_usage_from_jsonl_accepts_prompt_completion_fields(tmp_path):
    jsonl_path = tmp_path / "session.jsonl"
    rows = [
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "usage": {"prompt_tokens": 13, "completion_tokens": 9, "total_tokens": 22},
            },
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "usage": {"input": 4, "output": 2, "totalTokens": 6, "cacheRead": 1},
            },
        },
    ]
    jsonl_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    usage = calculate_usage_from_jsonl(str(jsonl_path))

    assert usage["input_tokens"] == 17
    assert usage["output_tokens"] == 11
    assert usage["total_tokens"] == 28
    assert usage["cacheRead"] == 1


def test_save_record_to_csv_flattens_ov_token_usage(tmp_path):
    csv_path = tmp_path / "qa.csv"
    save_record_to_csv(
        str(csv_path),
        {
            "sample_id": "conv-1",
            "sample_idx": 1,
            "qi": 1,
            "question": "Q",
            "expected": "A",
            "response": "R",
            "category": "2",
            "evidence": [],
            "usage": {"input_tokens": 1, "output_tokens": 2, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 3},
            "ov_token_usage": {
                "llm_prompt": 10,
                "llm_completion": 20,
                "llm_total": 30,
                "embedding": 40,
                "memories": 5,
                "memory_write": 4,
                "memory_edit": 1,
            },
            "ov_missing_records": 12,
        },
    )
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["ov_llm_prompt_tokens"] == "10"
    assert row["ov_llm_completion_tokens"] == "20"
    assert row["ov_llm_total_tokens"] == "30"
    assert row["ov_embedding_tokens"] == "40"
    assert row["ov_memories_extracted"] == "5"
    assert row["ov_memory_write"] == "4"
    assert row["ov_memory_edit"] == "1"
    assert row["ov_missing_records"] == "12"


def test_save_record_to_csv_writes_ov_closure_fields(tmp_path):
    csv_path = tmp_path / "qa.csv"
    save_record_to_csv(
        str(csv_path),
        {
            "sample_id": "conv-1",
            "sample_idx": 1,
            "qi": 1,
            "question": "Q",
            "expected": "A",
            "response": "R",
            "category": "2",
            "evidence": [],
            "usage": {"input_tokens": 1, "output_tokens": 2, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 3},
            "ov_memory_written": "true",
            "ov_token_emitted": "true",
            "ov_index_available": "false",
            "ov_closure_state": "memory_written_but_index_unavailable",
        },
    )
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["ov_memory_written"] == "true"
    assert row["ov_token_emitted"] == "true"
    assert row["ov_index_available"] == "false"
    assert row["ov_closure_state"] == "memory_written_but_index_unavailable"


def test_derive_ov_closure_status_prefers_memory_written_but_index_unavailable():
    state = derive_ov_closure_status(
        {
            "llm_total": 120,
            "embedding": 30,
            "memories": 1,
            "memory_write": 1,
            "memory_edit": 0,
        },
        {"ok": False, "missing_record_count": 18},
    )
    assert state["memory_written"] == "true"
    assert state["token_emitted"] == "true"
    assert state["index_available"] == "false"
    assert state["closure_state"] == "memory_written_but_index_unavailable"


def test_derive_ov_closure_status_prefers_recall_hit_over_consistency_gap():
    state = derive_ov_closure_status(
        {
            "llm_total": 120,
            "embedding": 30,
            "memories": 1,
            "memory_write": 1,
            "memory_edit": 0,
        },
        {"ok": False, "missing_record_count": 10},
        recall_total=3,
        response_text="Caroline attended the LGBTQ support group on May 7, 2023.",
    )
    assert state["memory_written"] == "true"
    assert state["token_emitted"] == "true"
    assert state["index_available"] == "false"
    assert state["recall_hit"] == "true"
    assert state["closure_state"] == "memory_recalled_with_consistency_gap"
