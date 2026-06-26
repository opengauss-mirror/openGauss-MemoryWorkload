import json

from locomo_test.config import Config
from locomo_test.stats import run_stats


def test_run_stats_writes_ov_closure_counts(tmp_path):
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    csv_path = out_dir / "qa_results.csv"
    csv_path.write_text(
        "\n".join(
            [
                "sample_id,sample_idx,qi,question,expected,response,category,evidence,input_tokens,output_tokens,cacheRead,cacheWrite,total_tokens,ov_llm_prompt_tokens,ov_llm_completion_tokens,ov_llm_total_tokens,ov_embedding_tokens,ov_memories_extracted,ov_memory_write,ov_memory_edit,ov_missing_records,ov_memory_written,ov_token_emitted,ov_index_available,ov_closure_state,timestamp,jsonl_filename,result,reasoning",
                "conv-1,1,1,Q,A,R,2,[],1,2,0,0,3,100,20,120,30,1,1,0,18,true,true,false,memory_written_but_index_unavailable,2026-06-23 13:00:00,session.jsonl,CORRECT,",
                "conv-1,1,2,Q,A,R,2,[],1,2,0,0,3,100,20,120,30,1,1,0,0,true,true,true,memory_closed_loop_ready,2026-06-23 13:00:01,session2.jsonl,CORRECT,",
            ]
        ),
        encoding="utf-8",
    )

    cfg = Config()
    cfg.name = "test-run"
    cfg.dataset = "small"
    cfg.data_file = "data/locomo_small.json"
    cfg.memory_mode = "openviking"

    run_stats(cfg, str(out_dir))

    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["ov_closure_counts"] == {
        "memory_closed_loop_ready": 1,
        "memory_written_but_index_unavailable": 1,
    }
    assert meta["ov_closure_summary"] == {
        "dominant_state": "memory_closed_loop_ready",
        "has_memory_written": True,
        "has_token_emitted": True,
        "has_index_unavailable": True,
    }
