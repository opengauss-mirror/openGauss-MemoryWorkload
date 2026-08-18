from pathlib import Path

from memory_bench_platform.locomo_test_metrics_bridge import (
    check_locomo_qa_results,
    derive_locomo_ov_closure_summary,
    summarize_locomo_qa_results,
    write_locomo_qa_diagnostics,
)


def test_check_locomo_qa_results_flags_tokens_and_missing_records(tmp_path: Path):
    csv_path = tmp_path / "qa_results.csv"
    csv_path.write_text(
        "\n".join(
            [
                "sample_id,sample_idx,qi,question,expected,response,category,evidence,input_tokens,output_tokens,cacheRead,cacheWrite,total_tokens,ov_llm_prompt_tokens,ov_llm_completion_tokens,ov_llm_total_tokens,ov_embedding_tokens,ov_memories_extracted,ov_memory_write,ov_memory_edit,ov_missing_records,timestamp,jsonl_filename,result,reasoning",
                "conv-1,1,1,Q,A,R,2,[],0,0,0,0,0,0,0,0,0,0,0,0,56,2026-06-23 13:00:00,session.jsonl,,,",
            ]
        ),
        encoding="utf-8",
    )

    issues = check_locomo_qa_results(str(tmp_path))

    assert issues["openviking_tokens_all_zero"] == 1
    assert issues["openviking_index_missing_records_max"] == 56


def test_check_locomo_qa_results_recognizes_direct_recall_only_mode(tmp_path: Path):
    csv_path = tmp_path / "qa_results.csv"
    csv_path.write_text(
        "\n".join(
            [
                "sample_id,sample_idx,qi,question,expected,response,category,evidence,input_tokens,output_tokens,cacheRead,cacheWrite,total_tokens,ov_llm_prompt_tokens,ov_llm_completion_tokens,ov_llm_total_tokens,ov_embedding_tokens,ov_memories_extracted,ov_memory_write,ov_memory_edit,ov_missing_records,ov_recall_total,ov_direct_recall_count,ov_recall_hit,ov_memory_written,ov_token_emitted,ov_index_available,ov_closure_state,timestamp,jsonl_filename,result,reasoning",
                "conv-1,1,1,Q,A,R,2,[],10,5,0,0,15,0,0,0,0,0,0,0,2,5,5,true,false,false,false,qa_direct_recall_only,2026-06-23 13:00:00,session.jsonl,,,",
            ]
        ),
        encoding="utf-8",
    )

    issues = check_locomo_qa_results(str(tmp_path))

    assert issues["openviking_direct_recall_only_mode"] == 1
    assert "openviking_tokens_all_zero" not in issues


def test_summarize_and_write_locomo_qa_diagnostics(tmp_path: Path):
    csv_path = tmp_path / "qa_results.csv"
    csv_path.write_text(
        "\n".join(
            [
                "sample_id,sample_idx,qi,question,expected,response,category,evidence,input_tokens,output_tokens,cacheRead,cacheWrite,total_tokens,ov_llm_prompt_tokens,ov_llm_completion_tokens,ov_llm_total_tokens,ov_embedding_tokens,ov_memories_extracted,ov_memory_write,ov_memory_edit,ov_missing_records,ov_memory_written,ov_token_emitted,ov_index_available,ov_closure_state,timestamp,jsonl_filename,result,reasoning",
                "conv-1,1,1,Q,A,R,2,[],0,0,0,0,0,100,20,120,30,1,1,0,18,true,true,false,memory_written_but_index_unavailable,2026-06-23 13:00:00,session.jsonl,,,",
                "conv-1,1,2,Q,A,R,2,[],0,0,0,0,0,100,20,120,30,1,1,0,19,true,true,false,memory_written_but_index_unavailable,2026-06-23 13:00:01,session2.jsonl,,,",
            ]
        ),
        encoding="utf-8",
    )

    summary = summarize_locomo_qa_results(str(tmp_path))
    assert summary["ov_closure_counts"] == {"memory_written_but_index_unavailable": 2}

    written = write_locomo_qa_diagnostics(str(tmp_path))
    assert written["ov_closure_summary"]["dominant_state"] == "memory_written_but_index_unavailable"
    assert written["ov_closure_summary"]["has_direct_recall"] is False
    assert (tmp_path / "qa_diagnostics.json").exists()


def test_derive_locomo_ov_closure_summary():
    rows = [
        {
            "ov_memory_written": "true",
            "ov_token_emitted": "true",
            "ov_index_available": "false",
            "ov_direct_recall_count": "0",
        },
        {
            "ov_memory_written": "true",
            "ov_token_emitted": "true",
            "ov_index_available": "true",
            "ov_direct_recall_count": "2",
        },
    ]
    counts = {"memory_closed_loop_ready": 2}
    summary = derive_locomo_ov_closure_summary(rows, counts)
    assert summary == {
        "dominant_state": "memory_closed_loop_ready",
        "has_memory_written": True,
        "has_token_emitted": True,
        "has_direct_recall": True,
        "has_index_unavailable": True,
    }
