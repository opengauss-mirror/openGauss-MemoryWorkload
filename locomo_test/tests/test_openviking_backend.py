import json

from locomo_test import openviking_backend


def test_backend_build_openviking_ingest_agent_id_is_session_scoped():
    assert openviking_backend.build_openviking_ingest_agent_id("locomo-eval", "session_1") == "locomo-eval--session_1"
    assert openviking_backend.build_openviking_ingest_agent_id("locomo eval", "session 2") == "locomo-eval--session-2"


def test_backend_ov_request_headers_prefers_configured_agent_prefix(tmp_path):
    (tmp_path / "openclaw.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "entries": {
                        "openviking": {
                            "config": {
                                "apiKey": "ov-key",
                                "accountId": "acct-1",
                                "userId": "user-1",
                                "agent_prefix": "locomo-eval",
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    headers = openviking_backend.ov_request_headers(str(tmp_path), fallback_agent_id="fallback-agent")

    assert headers == {
        "X-API-Key": "ov-key",
        "X-OpenViking-Account": "acct-1",
        "X-OpenViking-User": "user-1",
        "X-OpenViking-Agent": "locomo-eval_main",
    }


def test_backend_query_ov_task_token_usage_reports_timeout(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"status": "running"}}

    monkeypatch.setattr(openviking_backend.requests, "get", lambda *args, **kwargs: _Resp())
    monkeypatch.setattr(openviking_backend.time, "sleep", lambda *_args, **_kwargs: None)

    result, diag = openviking_backend.query_ov_task_token_usage(
        "http://ov",
        "task-1",
        max_wait=0,
        return_diag=True,
        request_headers_fn=lambda **kwargs: {"Authorization": "Bearer x"},
    )

    assert result is None
    assert diag["timed_out"] is True


def test_backend_query_ov_search_find_memories_reads_missing_content(monkeypatch, tmp_path):
    (tmp_path / "openclaw.json").write_text(
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

    class _FindResp:
        ok = True

        def json(self):
            return {
                "result": {
                    "memories": [{"uri": "viking://user/user-1/memories/m1.md"}],
                    "total": 1,
                }
            }

    class _ReadResp:
        ok = True
        content = b"1"

        def json(self):
            return {"result": {"content": "memory content"}}

    monkeypatch.setattr(openviking_backend.requests, "post", lambda *args, **kwargs: _FindResp())
    monkeypatch.setattr(openviking_backend.requests, "get", lambda *args, **kwargs: _ReadResp())

    memories = openviking_backend.query_ov_search_find_memories(
        "http://ov.local",
        "What happened?",
        "viking://user/user-1/memories",
        state_dir=str(tmp_path),
        fallback_agent_id="locomo-eval",
    )

    assert memories == [{"uri": "viking://user/user-1/memories/m1.md", "content": "memory content"}]
