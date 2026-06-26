import json

from locomo_test.config import Config, SessionConfig, SessionPolicy
from locomo_test.eval import (
    _process_single_question,
    normalize_ov_task_query_mode,
    run_ingest,
    should_attempt_gateway_compact,
)


def test_normalize_ov_task_query_mode_prefers_direct_ov_stable_for_openviking():
    assert normalize_ov_task_query_mode("openviking") == "direct_ov_stable"


def test_should_attempt_gateway_compact_disabled_for_openviking_by_default(monkeypatch):
    monkeypatch.delenv("LOCOMO_OPENVIKING_FORCE_COMPACT", raising=False)
    assert should_attempt_gateway_compact("openviking") is False


def test_should_attempt_gateway_compact_can_be_forced(monkeypatch):
    monkeypatch.setenv("LOCOMO_OPENVIKING_FORCE_COMPACT", "true")
    assert should_attempt_gateway_compact("openviking") is True


def test_run_ingest_commits_openviking_session_after_message(monkeypatch, tmp_path):
    data_path = tmp_path / "locomo.json"
    data_path.write_text(
        json.dumps(
            [
                {
                    "sample_id": "conv-1",
                    "conversation": {
                        "speaker_a": "A",
                        "speaker_b": "B",
                        "session_1_date_time": "1:00 pm on 1 May, 2023",
                        "session_1": [{"speaker": "A", "text": "hello"}],
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session_file = tmp_path / "ov-session-1.jsonl"
    session_file.write_text("", encoding="utf-8")

    cfg = Config()
    cfg.data_file = str(data_path)
    cfg.memory_mode = "openviking"
    cfg.user = "eval-1"
    cfg.agent_id = "locomo-eval"
    cfg.parallel = 1
    cfg.session = SessionConfig(policy=SessionPolicy.ISOLATED, tail="[]")
    cfg.gateway.state_dir = str(tmp_path)
    cfg.openviking.api_url = "http://ov.local"
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    monkeypatch.setattr(
        "locomo_test.eval.send_message_with_retry",
        lambda *args, **kwargs: ("OK", {"input_tokens": 1, "output_tokens": 1, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 2}),
    )
    monkeypatch.setattr(
        "locomo_test.eval.get_session_id_from_key",
        lambda *args, **kwargs: (session_file.name, str(tmp_path)),
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: session_file.name)
    monkeypatch.setattr("locomo_test.eval.wait_for_ov_latest_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval.query_ov_task_token_usage", lambda *args, **kwargs: None)

    committed = []

    def _commit(*, ov_api_url, session_id, keep_recent_count=None, wait=False, **kwargs):
        committed.append((ov_api_url, session_id, keep_recent_count, wait))
        return {"status": "accepted", "task_id": ""}

    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", _commit)

    run_ingest(cfg, str(output_dir))

    assert committed == [("http://ov.local", "ov-session-1", None, False)]


def test_process_single_question_commits_openviking_session_before_archive(monkeypatch, tmp_path):
    session_file = tmp_path / "qa-session-1.jsonl"
    session_file.write_text("", encoding="utf-8")

    cfg = Config()
    cfg.memory_mode = "openviking"
    cfg.user = "eval-1"
    cfg.agent_id = "locomo-eval"
    cfg.session = SessionConfig(policy=SessionPolicy.ISOLATED, tail="[]")
    cfg.gateway.state_dir = str(tmp_path)
    cfg.openviking.api_url = "http://ov.local"

    monkeypatch.setattr(
        "locomo_test.eval.send_message_with_retry",
        lambda *args, **kwargs: ("answer", {"input_tokens": 0, "output_tokens": 0, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 0}),
    )
    monkeypatch.setattr(
        "locomo_test.eval.get_session_id_from_key",
        lambda *args, **kwargs: (session_file.name, str(tmp_path)),
    )
    monkeypatch.setattr(
        "locomo_test.eval.calculate_usage_from_jsonl",
        lambda *args, **kwargs: {"input_tokens": 3, "output_tokens": 4, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 7},
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: "archived.jsonl")
    monkeypatch.setattr(
        "locomo_test.eval.query_ov_task_token_usage",
        lambda *args, **kwargs: {
            "llm_prompt": 10,
            "llm_completion": 20,
            "llm_total": 30,
            "embedding": 40,
            "memories": 5,
            "memory_write": 4,
            "memory_edit": 1,
            "task_id": "task-1",
        },
    )
    monkeypatch.setattr("locomo_test.eval.query_ov_session_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "locomo_test.eval.query_ov_index_consistency",
        lambda *args, **kwargs: {"ok": False, "missing_record_count": 31},
    )

    committed = []

    def _commit(*, ov_api_url, session_id, keep_recent_count=None, wait=False, **kwargs):
        committed.append((ov_api_url, session_id, keep_recent_count, wait))
        return {"status": "accepted", "task_id": "task-1"}

    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", _commit)

    record = _process_single_question(
        sample_id="conv-1",
        sample_idx=1,
        qi=1,
        qa={"question": "When?", "answer": "Today", "category": "2", "evidence": []},
        cfg=cfg,
        csv_path=str(tmp_path / "qa.csv"),
        question_time="2023-06-27",
    )

    assert committed == [("http://ov.local", "qa-session-1", None, False)]
    assert record["usage"]["total_tokens"] == 7
    assert record["ov_token_usage"]["llm_total"] == 30


def test_process_single_question_tolerates_openviking_commit_404(monkeypatch, tmp_path):
    session_file = tmp_path / "qa-session-404.jsonl"
    session_file.write_text("", encoding="utf-8")

    cfg = Config()
    cfg.memory_mode = "openviking"
    cfg.user = "eval-1"
    cfg.agent_id = "locomo-eval"
    cfg.session = SessionConfig(policy=SessionPolicy.ISOLATED, tail="[]")
    cfg.gateway.state_dir = str(tmp_path)
    cfg.openviking.api_url = "http://ov.local"

    monkeypatch.setattr(
        "locomo_test.eval.send_message_with_retry",
        lambda *args, **kwargs: ("answer", {"input_tokens": 0, "output_tokens": 0, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 0}),
    )
    monkeypatch.setattr(
        "locomo_test.eval.get_session_id_from_key",
        lambda *args, **kwargs: (session_file.name, str(tmp_path)),
    )
    monkeypatch.setattr(
        "locomo_test.eval.calculate_usage_from_jsonl",
        lambda *args, **kwargs: {"input_tokens": 3, "output_tokens": 4, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 7},
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: "archived.jsonl")
    monkeypatch.setattr("locomo_test.eval.query_ov_session_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval.query_ov_index_consistency", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval.query_ov_search_find_total", lambda *args, **kwargs: 0)

    def _commit(*, ov_api_url, session_id, keep_recent_count=None, wait=False, **kwargs):
        return {"status": "not_found", "task_id": "", "error": "404"}

    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", _commit)

    record = _process_single_question(
        sample_id="conv-1",
        sample_idx=1,
        qi=1,
        qa={"question": "When?", "answer": "Today", "category": "2", "evidence": []},
        cfg=cfg,
        csv_path=str(tmp_path / "qa.csv"),
        question_time="2023-06-27",
    )

    assert record["usage"]["total_tokens"] == 7
    assert "ov_token_usage" not in record


def test_query_ov_task_token_usage_uses_scoped_headers(monkeypatch, tmp_path):
    requests_seen = []
    cfg_path = tmp_path / "openclaw.json"
    cfg_path.write_text(
        json.dumps(
            {
                "plugins": {
                    "entries": {
                        "openviking": {
                            "config": {
                                "apiKey": "ov-key",
                                "accountId": "acct-1",
                                "userId": "user-1",
                                "agentId": "acct-1",
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "result": {
                    "status": "completed",
                    "task_id": "task-1",
                    "result": {
                        "token_usage": {
                            "llm": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                            "embedding": {"total_tokens": 4},
                        },
                        "memories_extracted": {"memory_write": 1, "memory_edit": 0},
                    },
                }
            }

    def _get(url, *, headers=None, timeout=None):
        requests_seen.append((url, headers, timeout))
        return _Resp()

    monkeypatch.setattr("locomo_test.eval.requests.get", _get)

    from locomo_test.eval import query_ov_task_token_usage

    result = query_ov_task_token_usage(
        "http://ov.local",
        "task-1",
        state_dir=str(tmp_path),
        fallback_agent_id="locomo-eval",
        max_wait=1,
    )

    assert result["llm_total"] == 3
    assert requests_seen[0][1]["X-API-Key"] == "ov-key"
    assert requests_seen[0][1]["X-OpenViking-Account"] == "acct-1"
    assert requests_seen[0][1]["X-OpenViking-User"] == "user-1"
    assert requests_seen[0][1]["X-OpenViking-Agent"] == "acct-1_main"


def test_run_ingest_passes_explicit_keep_recent_count_override(monkeypatch, tmp_path):
    data_path = tmp_path / "locomo.json"
    data_path.write_text(
        json.dumps(
            [
                {
                    "sample_id": "conv-1",
                    "conversation": {
                        "speaker_a": "A",
                        "speaker_b": "B",
                        "session_1_date_time": "1:00 pm on 1 May, 2023",
                        "session_1": [{"speaker": "A", "text": "hello"}],
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session_file = tmp_path / "ov-session-1.jsonl"
    session_file.write_text("", encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    cfg = Config()
    cfg.data_file = str(data_path)
    cfg.memory_mode = "openviking"
    cfg.user = "eval-1"
    cfg.agent_id = "locomo-eval"
    cfg.parallel = 1
    cfg.session = SessionConfig(policy=SessionPolicy.ISOLATED, tail="[]")
    cfg.gateway.state_dir = str(tmp_path)
    cfg.openviking.api_url = "http://ov.local"
    cfg.openviking.keep_recent_count = 0

    monkeypatch.setattr(
        "locomo_test.eval.send_message_with_retry",
        lambda *args, **kwargs: ("OK", {"input_tokens": 1, "output_tokens": 1, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 2}),
    )
    monkeypatch.setattr(
        "locomo_test.eval.get_session_id_from_key",
        lambda *args, **kwargs: (session_file.name, str(tmp_path)),
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: session_file.name)
    monkeypatch.setattr("locomo_test.eval.wait_for_ov_latest_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval.query_ov_task_token_usage", lambda *args, **kwargs: None)

    committed = []

    def _commit(*, ov_api_url, session_id, keep_recent_count=None, wait=False, **kwargs):
        committed.append((ov_api_url, session_id, keep_recent_count, wait))
        return {"status": "accepted", "task_id": ""}

    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", _commit)

    run_ingest(cfg, str(output_dir))

    assert committed == [("http://ov.local", "ov-session-1", 0, False)]
