import json

from locomo_test.config import Config
from locomo_test.pipeline import run_pipeline


def test_run_pipeline_writes_qa_diagnostics_without_stats(monkeypatch, tmp_path):
    data_path = tmp_path / "locomo.json"
    data_path.write_text("[]", encoding="utf-8")

    cfg = Config()
    cfg.name = "qa-diag-run"
    cfg.output_dir = str(tmp_path)
    cfg.data_file = str(data_path)
    cfg.dataset = "small"
    cfg.memory_mode = "openviking"

    def fake_run_qa(_cfg, output_dir):
        csv_path = tmp_path / "qa-diag-run" / "qa_results.csv"
        csv_path.write_text(
            "\n".join(
                [
                    "sample_id,sample_idx,qi,question,expected,response,category,evidence,input_tokens,output_tokens,cacheRead,cacheWrite,total_tokens,ov_llm_prompt_tokens,ov_llm_completion_tokens,ov_llm_total_tokens,ov_embedding_tokens,ov_memories_extracted,ov_memory_write,ov_memory_edit,ov_missing_records,ov_memory_written,ov_token_emitted,ov_index_available,ov_closure_state,timestamp,jsonl_filename,result,reasoning",
                    "conv-1,1,1,Q,A,R,2,[],0,0,0,0,0,100,20,120,30,1,1,0,18,true,true,false,memory_written_but_index_unavailable,2026-06-23 13:00:00,session.jsonl,,,",
                ]
            ),
            encoding="utf-8",
        )
        return {"input_tokens": 0, "output_tokens": 0, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 0}

    monkeypatch.setattr("locomo_test.pipeline.run_qa", fake_run_qa)

    run_pipeline(cfg, only=["qa"])

    diagnostics_path = tmp_path / "qa-diag-run" / "qa_diagnostics.json"
    payload = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    assert payload["ov_closure_summary"]["dominant_state"] == "memory_written_but_index_unavailable"
