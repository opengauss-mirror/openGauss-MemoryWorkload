import json

from locomo_test.checks import check_health, check_qa_results, write_qa_diagnostics
from locomo_test.config import Config


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


def test_check_health_bootstraps_openviking_context_by_default(monkeypatch):
    cfg = Config()
    cfg.memory_mode = "openviking"
    cfg.gateway.token = "token"
    cfg.gateway.state_dir = "/tmp/openclaw-state"
    cfg.judge_env.api_key = "judge-key"
    cfg.judge_env.model = "judge-model"

    calls = {"send": 0, "reset": 0}

    def fake_get(url, timeout):
        del timeout
        if url.endswith("/health"):
            return _Resp(200)
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, json, headers, timeout):
        del url, json, headers, timeout
        return _Resp(200)

    monkeypatch.setattr("locomo_test.checks.requests.get", fake_get)
    monkeypatch.setattr("locomo_test.checks.requests.post", fake_post)
    monkeypatch.setattr(
        "locomo_test.checks.send_message",
        lambda *args, **kwargs: calls.__setitem__("send", calls["send"] + 1) or ("OK", {}),
    )
    monkeypatch.setattr(
        "locomo_test.checks.get_session_id_from_key",
        lambda *args, **kwargs: ("bootstrap.jsonl", "/tmp/openclaw-state/agents/main/sessions"),
    )
    monkeypatch.setattr(
        "locomo_test.checks.reset_session",
        lambda *args, **kwargs: calls.__setitem__("reset", calls["reset"] + 1) or "bootstrap.jsonl.bak",
    )

    assert check_health(cfg) is True
    assert calls["send"] == 1
    assert calls["reset"] == 1


def test_check_health_can_disable_openviking_bootstrap(monkeypatch):
    cfg = Config()
    cfg.memory_mode = "openviking"
    cfg.judge_env.api_key = "judge-key"
    cfg.judge_env.model = "judge-model"

    def fake_get(url, timeout):
        del timeout
        if url.endswith("/health"):
            return _Resp(200)
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, json, headers, timeout):
        del url, json, headers, timeout
        return _Resp(200)

    monkeypatch.setenv("LOCOMO_OPENVIKING_BOOTSTRAP", "false")
    monkeypatch.setattr("locomo_test.checks.requests.get", fake_get)
    monkeypatch.setattr("locomo_test.checks.requests.post", fake_post)
    monkeypatch.setattr(
        "locomo_test.checks.send_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("bootstrap should be disabled")),
    )

    assert check_health(cfg) is True


def test_check_qa_results_flags_openviking_zero_tokens_and_missing_records(tmp_path):
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

    issues = check_qa_results(str(tmp_path))

    assert issues["openviking_tokens_all_zero"] == 1
    assert issues["openviking_index_missing_records_max"] == 56


def test_check_qa_results_flags_memory_written_but_index_unavailable(tmp_path):
    csv_path = tmp_path / "qa_results.csv"
    csv_path.write_text(
        "\n".join(
            [
                "sample_id,sample_idx,qi,question,expected,response,category,evidence,input_tokens,output_tokens,cacheRead,cacheWrite,total_tokens,ov_llm_prompt_tokens,ov_llm_completion_tokens,ov_llm_total_tokens,ov_embedding_tokens,ov_memories_extracted,ov_memory_write,ov_memory_edit,ov_missing_records,timestamp,jsonl_filename,result,reasoning",
                "conv-1,1,1,Q,A,R,2,[],0,0,0,0,0,100,20,120,30,1,1,0,18,2026-06-23 13:00:00,session.jsonl,,,",
            ]
        ),
        encoding="utf-8",
    )

    issues = check_qa_results(str(tmp_path))

    assert issues["openviking_index_missing_records_max"] == 18
    assert issues["openviking_memory_written_but_index_unavailable"] == 1


def test_write_qa_diagnostics_writes_run_level_closure_summary(tmp_path):
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

    diagnostics = write_qa_diagnostics(str(tmp_path))

    assert diagnostics["ov_closure_counts"] == {"memory_written_but_index_unavailable": 2}
    assert diagnostics["ov_closure_summary"] == {
        "dominant_state": "memory_written_but_index_unavailable",
        "has_memory_written": True,
        "has_token_emitted": True,
        "has_index_unavailable": True,
    }

    file_data = json.loads((tmp_path / "qa_diagnostics.json").read_text(encoding="utf-8"))
    assert file_data["ov_closure_summary"]["dominant_state"] == "memory_written_but_index_unavailable"
