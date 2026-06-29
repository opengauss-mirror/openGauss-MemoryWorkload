import json
from pathlib import Path

from memory_bench_platform.locomo_test_diagnostics import diagnose_locomo_test_output


def test_diagnose_locomo_test_output_extracts_nodes_and_timings(tmp_path: Path):
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "dataset": "small",
                "session_policy": "isolated",
                "total_questions": 3,
                "total_correct": 2,
                "total_graded": 3,
                "overall_accuracy": 0.6667,
                "memory_token_totals": {
                    "llm_total": 300,
                    "embedding": 40,
                    "memories": 5,
                },
                "ov_closure_counts": {
                    "memory_recalled_with_consistency_gap": 2,
                    "no_memory_signal": 1,
                },
                "ov_closure_summary": {
                    "dominant_state": "memory_recalled_with_consistency_gap",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "qa_diagnostics.json").write_text(
        json.dumps(
            {
                "issues": {
                    "openviking_index_missing_records_max": 8,
                    "openviking_memory_written_but_index_unavailable": 2,
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".ingest_record.json").write_text(
        json.dumps(
            {
                "s1": {"timestamp": 100},
                "s2": {"timestamp": 160},
                "s3": {"timestamp": 190},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "qa_results.csv").write_text(
        "\n".join(
            [
                "sample_id,qi,question,expected,response,category,total_tokens,ov_llm_total_tokens,ov_recall_total,ov_recall_hit,ov_missing_records,ov_closure_state,result,reasoning",
                "conv-1,1,Q1,A1,R1,1,0,12,5,true,8,memory_recalled_with_consistency_gap,CORRECT,ok",
                "conv-1,2,Q2,A2,R2,1,0,0,5,false,8,no_memory_signal,WRONG,bad",
                "conv-1,3,Q3,A3,R3,1,0,9,4,true,6,memory_recalled_with_consistency_gap,CORRECT,ok",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "pipeline.log").write_text(
        "\n".join(
            [
                "  [health_check] done in 10.0s",
                "  [ingest] done in 120.0s",
                "  [qa] done in 90.0s",
                "  [judge] done in 15.0s",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "chunk_diagnostics.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "session_key": "session_3",
                        "chunk_index": 1,
                        "chunk_total": 4,
                        "status": "passed",
                        "send": {"elapsed_seconds": 30.0, "attempts": 1},
                        "ov_task_wait": {"elapsed_seconds": 95.0, "timed_out": False},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "session_key": "session_4",
                        "chunk_index": 2,
                        "chunk_total": 3,
                        "status": "failed",
                        "send": {"elapsed_seconds": 181.0, "attempts": 3, "timeout_hit": True},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "report.html").write_text("<html>demo</html>", encoding="utf-8")

    result = diagnose_locomo_test_output(tmp_path)

    assert result["source"] == "locomo_test"
    assert result["nodes"]["session_construction"]["ingest_session_count"] == 3
    assert result["nodes"]["memory_capture"]["ov_llm_total"] == 300
    assert result["nodes"]["recall_query"]["recall_hit_count"] == 2
    assert result["nodes"]["recall_query"]["recall_total_max"] == 5
    assert result["nodes"]["answer_generation"]["overall_accuracy"] == 0.6667
    assert result["timing"]["steps"]["ingest_seconds"] == 120.0
    assert result["timing"]["ingest_session_span_seconds"] == 90
    assert result["chunk_diagnostics_summary"]["chunk_total"] == 2
    assert result["chunk_diagnostics_summary"]["slow_chunk_count"] == 1
    assert result["chunk_diagnostics_summary"]["timeout_chunk_count"] == 1
    assert any("index unavailable" in item for item in result["findings"])
    assert any("ingest chunk" in item for item in result["findings"])
