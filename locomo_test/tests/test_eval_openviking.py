import json

from locomo_test.config import Config, SessionConfig, SessionPolicy
from locomo_test.eval import (
    _send_message_with_retry_diagnostics,
    _process_single_question,
    augment_ov_recall_with_named_person_entities,
    build_openviking_ingest_agent_id,
    build_ingest_input_message,
    build_qa_input_message,
    format_ov_recall_evidence_block,
    normalize_ov_task_query_mode,
    query_ov_search_find_memories,
    query_ov_task_token_usage,
    rerank_ov_recalled_memories,
    run_ingest,
    run_qa,
    should_skip_openviking_qa_commit,
    should_attempt_gateway_compact,
)

def test_normalize_ov_task_query_mode_prefers_direct_ov_stable_for_openviking():
    assert normalize_ov_task_query_mode("openviking") == "direct_ov_stable"


def test_build_openviking_ingest_agent_id_is_session_scoped():
    assert build_openviking_ingest_agent_id("locomo-eval", "session_1") == "locomo-eval--session_1"
    assert build_openviking_ingest_agent_id("locomo eval", "session 2") == "locomo-eval--session-2"


def test_ov_request_headers_prefers_explicit_fallback_agent_over_env(monkeypatch, tmp_path):
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
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENVIKING_AGENT_ID", "locomo-eval")

    from locomo_test.eval import _ov_request_headers

    headers = _ov_request_headers(str(tmp_path), fallback_agent_id="locomo-eval--session_1")
    assert headers["X-OpenViking-Agent"] == "locomo-eval--session_1"


def test_should_attempt_gateway_compact_disabled_for_openviking_by_default(monkeypatch):
    monkeypatch.delenv("LOCOMO_OPENVIKING_FORCE_COMPACT", raising=False)
    assert should_attempt_gateway_compact("openviking") is False


def test_should_attempt_gateway_compact_can_be_forced(monkeypatch):
    monkeypatch.setenv("LOCOMO_OPENVIKING_FORCE_COMPACT", "true")
    assert should_attempt_gateway_compact("openviking") is True


def test_build_qa_input_message_uses_benchmark_prompt_by_default(monkeypatch):
    monkeypatch.delenv("LOCOMO_QA_PROMPT_PREFIX", raising=False)
    text = build_qa_input_message(
        question="When did Caroline go to the LGBTQ support group?",
        question_time="2023-06-27",
    )
    assert "Current date: 2023-06-27." in text
    assert "recalled memory snippets as the primary evidence" in text
    assert "Do not replace exact facts with vague substitutes" in text
    assert "Question: When did Caroline go to the LGBTQ support group?" in text


def test_build_ingest_input_message_uses_memory_summary_prompt(monkeypatch):
    monkeypatch.delenv("LOCOMO_INGEST_PROMPT_PREFIX", raising=False)
    text = build_ingest_input_message("[group chat conversation: 1:56 pm on 8 May, 2023]\nCaroline: hello")
    assert "memory-ingestion notes" in text
    assert "No follow-up questions." in text
    assert "BOTH participants" in text
    assert "Write ONE explicit fact per bullet." in text
    assert "Sweden, necklace, 5 years, 10 years ago, June 2023" in text
    assert len(text.split("\n\n", 1)[0]) < 900
    assert text.endswith("Caroline: hello")


def test_should_skip_openviking_qa_commit_defaults_to_true_for_qa_sessions(monkeypatch):
    monkeypatch.delenv("LOCOMO_OPENVIKING_QA_COMMIT", raising=False)
    assert should_skip_openviking_qa_commit("qa-conv-26-q1") is True
    assert should_skip_openviking_qa_commit("ingest-conv-26-session_1") is False


def test_should_skip_openviking_qa_commit_can_be_overridden(monkeypatch):
    monkeypatch.setenv("LOCOMO_OPENVIKING_QA_COMMIT", "true")
    assert should_skip_openviking_qa_commit("qa-conv-26-q1") is False


def test_send_message_with_retry_diagnostics_reports_attempts(monkeypatch):
    seen = {"count": 0}

    def _send(*args, **kwargs):
        seen["count"] += 1
        if seen["count"] < 2:
            raise RuntimeError("temporary failure")
        return "OK", {"total_tokens": 3}

    monkeypatch.setattr("locomo_test.eval.send_message", _send)
    monkeypatch.setenv("LOCOMO_GATEWAY_RETRY_BACKOFF_SECONDS", "0.01")

    reply, usage, diag = _send_message_with_retry_diagnostics("u", "t", "user", "msg", 2, "agent", "sess")

    assert reply == "OK"
    assert usage["total_tokens"] == 3
    assert diag["attempts"] == 2
    assert diag["retries_configured"] == 2
    assert diag["elapsed_seconds"] >= 0


def test_query_ov_task_token_usage_return_diag_reports_timeout(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"status": "running"}}

    monkeypatch.setattr("locomo_test.eval._ov_request_headers", lambda **kwargs: {"Authorization": "Bearer x"})
    monkeypatch.setattr("locomo_test.eval.requests.get", lambda *args, **kwargs: _Resp())
    monkeypatch.setattr("locomo_test.eval.time.sleep", lambda *_args, **_kwargs: None)

    result, diag = query_ov_task_token_usage(
        "http://ov",
        "task-1",
        max_wait=0,
        return_diag=True,
    )

    assert result is None
    assert diag["timed_out"] is True


def test_build_qa_input_message_includes_direct_recall_evidence(monkeypatch):
    monkeypatch.delenv("LOCOMO_QA_PROMPT_PREFIX", raising=False)
    text = build_qa_input_message(
        question="What made Caroline happy?",
        question_time="2023-06-27",
        recalled_memories=[
            {
                "uri": "viking://user/demo/memories/events/happy.md",
                "title": "Happy moment",
                "summary": "Caroline felt happy after joining the support group.",
                "score": 0.91,
            }
        ],
    )
    assert "Retrieved memory evidence:" in text
    assert "Summary: Caroline felt happy after joining the support group." in text
    assert "Question: What made Caroline happy?" in text


def test_rerank_ov_recalled_memories_prefers_exact_keyword_overlap():
    ranked = rerank_ov_recalled_memories(
        "What workshop did Caroline attend recently?",
        [
            {
                "uri": "viking://user/demo/memories/events/support_group.md",
                "summary": "On 2023-05-07, Caroline attended a LGBTQ support group.",
            },
            {
                "uri": "viking://user/demo/memories/events/workshop.md",
                "summary": "On 2023-06-23, Caroline attended an LGBTQ+ counseling workshop.",
            },
        ],
    )
    assert ranked[0]["uri"].endswith("workshop.md")


def test_rerank_ov_recalled_memories_prefers_named_person_entity_for_profile_question():
    ranked = rerank_ov_recalled_memories(
        "What is Caroline's identity?",
        [
            {
                "uri": "viking://user/demo/memories/events/support_group.md",
                "summary": "Caroline visited an LGBTQ support group and felt accepted.",
            },
            {
                "uri": "viking://user/demo/memories/entities/person/Caroline.md",
                "title": "Caroline",
                "summary": "Caroline has been transitioning for 3 years and spoke about her transgender journey.",
            },
        ],
    )
    assert ranked[0]["uri"].endswith("entities/person/Caroline.md")


def test_rerank_ov_recalled_memories_prefers_named_person_entity_for_named_person_question():
    ranked = rerank_ov_recalled_memories(
        "What did Caroline research?",
        [
            {
                "uri": "viking://user/demo/memories/events/career.md",
                "summary": "Caroline said she would continue her education and explore counseling or mental health work.",
            },
            {
                "uri": "viking://user/demo/memories/entities/person/Caroline.md",
                "title": "Caroline",
                "summary": "Caroline was researching adoption agencies because she wanted to give kids a loving home.",
            },
        ],
    )
    assert ranked[0]["uri"].endswith("entities/person/Caroline.md")


def test_augment_ov_recall_with_named_person_entities_adds_matching_entity_memory(monkeypatch):
    monkeypatch.setattr("locomo_test.eval._ov_request_headers", lambda **kwargs: {"Authorization": "Bearer x"})
    def _read(_ov_api_url, _headers, uri):
        if uri.endswith("/caroline.md"):
            return "# Caroline\n- Has been transitioning for 3 years.\n- Said her friends are her rocks.\n"
        return ""

    monkeypatch.setattr("locomo_test.eval._ov_read_content_by_uri", _read)

    augmented = augment_ov_recall_with_named_person_entities(
        ov_api_url="http://ov.local",
        question="What is Caroline's identity?",
        user_id="demo",
        recalled_memories=[
            {"uri": "viking://user/demo/memories/events/support_group.md", "summary": "Caroline visited a support group."}
        ],
        state_dir="/tmp/demo",
        fallback_agent_id="locomo-eval",
    )

    assert len(augmented) == 2
    assert augmented[-1]["uri"].endswith("entities/person/caroline.md")
    assert augmented[-1]["title"] == "Caroline"


def test_augment_ov_recall_with_named_person_entities_skips_non_profile_question(monkeypatch):
    monkeypatch.setattr("locomo_test.eval._ov_request_headers", lambda **kwargs: {"Authorization": "Bearer x"})
    seen = {"count": 0}

    def _read(*args, **kwargs):
        seen["count"] += 1
        return ""

    monkeypatch.setattr("locomo_test.eval._ov_read_content_by_uri", _read)

    augmented = augment_ov_recall_with_named_person_entities(
        ov_api_url="http://ov.local",
        question="When did Melanie run a charity race?",
        user_id="demo",
        recalled_memories=[
            {"uri": "viking://user/demo/memories/events/charity_race.md", "summary": "Melanie ran a charity race on 2023-05-20."}
        ],
        state_dir="/tmp/demo",
        fallback_agent_id="locomo-eval",
    )

    assert len(augmented) == 1
    assert seen["count"] == 0


def test_query_ov_search_find_memories_skips_directory_overview_and_unready_abstract(monkeypatch):
    class _Resp:
        def __init__(self, payload):
            self.ok = True
            self._payload = payload
            self.content = b"x"

        def json(self):
            return self._payload

    monkeypatch.setattr("locomo_test.eval._ov_request_headers", lambda **kwargs: {"Authorization": "Bearer x"})
    monkeypatch.setattr(
        "locomo_test.eval.requests.post",
        lambda *args, **kwargs: _Resp(
            {
                "result": {
                    "memories": [
                        {
                            "uri": "viking://user/demo/memories/entities/person/.overview.md",
                            "abstract": "# viking://user/demo/memories/entities/person [Directory abstract is not ready]",
                        },
                        {
                            "uri": "viking://user/demo/memories/events/workshop.md",
                            "summary": "Caroline attended an LGBTQ+ counseling workshop.",
                        },
                    ]
                }
            }
        ),
    )

    memories = query_ov_search_find_memories(
        "http://ov.local",
        "What workshop did Caroline attend?",
        "viking://user/demo/memories",
        state_dir="/tmp/demo",
        fallback_agent_id="locomo-eval",
        limit=8,
    )

    assert len(memories) == 1
    assert memories[0]["uri"].endswith("workshop.md")


def test_format_ov_recall_evidence_block_prefers_normalized_summary():
    block = format_ov_recall_evidence_block(
        [
            {
                "uri": "viking://user/demo/memories/m1.md",
                "normalized_summary": "Caroline visited the LGBTQ support group the week before 2023-06-27.",
                "content": "raw verbose content",
            }
        ],
        max_items=1,
        max_chars_per_item=200,
    )
    assert "normalized_summary" not in block
    assert "Caroline visited the LGBTQ support group the week before 2023-06-27." in block
    assert "Details: raw verbose content" in block


def test_format_ov_recall_evidence_block_strips_synthetic_chatlog_date_header():
    block = format_ov_recall_evidence_block(
        [
            {
                "uri": "viking://user/demo/memories/m1.md",
                "summary": "On 2023-05-07, Caroline attended an LGBTQ support group.",
                "content": "2026-06-27 (Saturday) ChatLog: [user]: [group chat conversation: 1:56 pm on 8 May, 2023] Caroline: I went yesterday.",
            }
        ],
        max_items=1,
        max_chars_per_item=300,
    )
    assert "2026-06-27 (Saturday) ChatLog" not in block
    assert "Details: [group chat conversation: 1:56 pm on 8 May, 2023] Caroline: I went yesterday." in block


def test_format_ov_recall_evidence_block_prefers_embedded_assistant_summary():
    block = format_ov_recall_evidence_block(
        [
            {
                "uri": "viking://user/demo/memories/m1.md",
                "summary": "On 2023-05-25, Caroline researched adoption agencies.",
                "content": (
                    "2026-06-27 (Saturday) ChatLog:\n"
                    "[user]: [group chat conversation: 1:14 pm on 25 May, 2023] "
                    "Melanie: I ran a charity race last Saturday.\n"
                    "[assistant]: - On 25 May 2023, Melanie said she ran a charity race for mental health last Saturday. "
                    "- Melanie said she was planning to go camping next month."
                ),
            }
        ],
        max_items=1,
        max_chars_per_item=400,
    )
    assert "Details: - On 25 May 2023, Melanie said she ran a charity race for mental health last Saturday." in block
    assert "[group chat conversation: 1:14 pm on 25 May, 2023]" not in block


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
        "locomo_test.eval._send_message_with_retry_diagnostics",
        lambda *args, **kwargs: (
            "OK",
            {"input_tokens": 1, "output_tokens": 1, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 2},
            {"attempts": 1, "retries_configured": 2, "elapsed_seconds": 1.0, "request_timeout_seconds": 180.0, "wait_schedule_seconds": [], "timeout_hit": False, "final_error": ""},
        ),
    )
    monkeypatch.setattr(
        "locomo_test.eval.get_session_id_from_key",
        lambda *args, **kwargs: (session_file.name, str(tmp_path)),
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: session_file.name)
    monkeypatch.setattr(
        "locomo_test.eval.wait_for_ov_latest_task",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "timeout"},
        ) if kwargs.get("return_diag") else None,
    )
    monkeypatch.setattr(
        "locomo_test.eval.query_ov_task_token_usage",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "no_task"},
        ) if kwargs.get("return_diag") else None,
    )

    committed = []

    def _commit(*, ov_api_url, session_id, keep_recent_count=None, wait=False, **kwargs):
        committed.append((ov_api_url, session_id, keep_recent_count, wait, kwargs.get("fallback_agent_id")))
        return {"status": "accepted", "task_id": ""}

    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", _commit)

    run_ingest(cfg, str(output_dir))

    assert committed == [("http://ov.local", "ov-session-1", None, False, "locomo-eval--session_1")]


def test_run_ingest_retries_session_lookup_before_failing(monkeypatch, tmp_path):
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
    session_file = tmp_path / "ov-session-2.jsonl"
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
        "locomo_test.eval._send_message_with_retry_diagnostics",
        lambda *args, **kwargs: (
            "OK",
            {"input_tokens": 1, "output_tokens": 1, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 2},
            {"attempts": 1, "retries_configured": 2, "elapsed_seconds": 1.0, "request_timeout_seconds": 180.0, "wait_schedule_seconds": [], "timeout_hit": False, "final_error": ""},
        ),
    )

    lookup_calls = {"count": 0}

    def _lookup(*args, **kwargs):
        lookup_calls["count"] += 1
        if lookup_calls["count"] < 3:
            return None
        return (session_file.name, str(tmp_path))

    monkeypatch.setattr("locomo_test.eval.get_session_id_from_key", _lookup)
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: session_file.name)
    monkeypatch.setattr(
        "locomo_test.eval.wait_for_ov_latest_task",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "timeout"},
        ) if kwargs.get("return_diag") else None,
    )
    monkeypatch.setattr(
        "locomo_test.eval.query_ov_task_token_usage",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "no_task"},
        ) if kwargs.get("return_diag") else None,
    )
    monkeypatch.setattr("locomo_test.eval.time.sleep", lambda *args, **kwargs: None)

    committed = []

    def _commit(*, ov_api_url, session_id, keep_recent_count=None, wait=False, **kwargs):
        committed.append((ov_api_url, session_id, keep_recent_count, wait))
        return {"status": "accepted", "task_id": ""}

    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", _commit)

    run_ingest(cfg, str(output_dir))

    assert lookup_calls["count"] >= 3
    assert committed == [("http://ov.local", "ov-session-2", None, False)]


def test_run_ingest_falls_back_to_latest_session_file_when_index_missing(monkeypatch, tmp_path):
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
    session_dir = tmp_path / "agents" / "locomo-eval" / "sessions"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "latest-session.jsonl"
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
        "locomo_test.eval._send_message_with_retry_diagnostics",
        lambda *args, **kwargs: (
            "OK",
            {"input_tokens": 1, "output_tokens": 1, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 2},
            {"attempts": 1, "retries_configured": 2, "elapsed_seconds": 1.0, "request_timeout_seconds": 180.0, "wait_schedule_seconds": [], "timeout_hit": False, "final_error": ""},
        ),
    )
    monkeypatch.setattr("locomo_test.eval.get_session_id_from_key", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: session_file.name)
    monkeypatch.setattr(
        "locomo_test.eval.wait_for_ov_latest_task",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "timeout"},
        ) if kwargs.get("return_diag") else None,
    )
    monkeypatch.setattr(
        "locomo_test.eval.query_ov_task_token_usage",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "no_task"},
        ) if kwargs.get("return_diag") else None,
    )
    monkeypatch.setattr("locomo_test.eval.time.sleep", lambda *args, **kwargs: None)

    committed = []

    def _commit(*, ov_api_url, session_id, keep_recent_count=None, wait=False, **kwargs):
        committed.append((ov_api_url, session_id, keep_recent_count, wait))
        return {"status": "accepted", "task_id": ""}

    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", _commit)

    run_ingest(cfg, str(output_dir))

    assert committed == [("http://ov.local", "latest-session", None, False)]


def test_run_ingest_falls_back_to_session_id_from_openclaw_log(monkeypatch, tmp_path):
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
        "locomo_test.eval._send_message_with_retry_diagnostics",
        lambda *args, **kwargs: (
            "OK",
            {"input_tokens": 1, "output_tokens": 1, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 2},
            {"attempts": 1, "retries_configured": 2, "elapsed_seconds": 1.0, "request_timeout_seconds": 180.0, "wait_schedule_seconds": [], "timeout_hit": False, "final_error": ""},
        ),
    )
    monkeypatch.setattr("locomo_test.eval.get_session_id_from_key", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval.time.sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval._find_latest_session_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "locomo_test.eval._find_session_id_in_openclaw_log",
        lambda *args, **kwargs: ("from-log-session.jsonl", str(tmp_path)),
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: "from-log-session.jsonl")
    monkeypatch.setattr(
        "locomo_test.eval.wait_for_ov_latest_task",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "timeout"},
        ) if kwargs.get("return_diag") else None,
    )
    monkeypatch.setattr(
        "locomo_test.eval.query_ov_task_token_usage",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "no_task"},
        ) if kwargs.get("return_diag") else None,
    )

    committed = []

    def _commit(*, ov_api_url, session_id, keep_recent_count=None, wait=False, **kwargs):
        committed.append((ov_api_url, session_id, keep_recent_count, wait))
        return {"status": "accepted", "task_id": ""}

    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", _commit)

    run_ingest(cfg, str(output_dir))

    assert committed == [("http://ov.local", "from-log-session", None, False)]


def test_process_single_question_skips_openviking_commit_for_qa_session_by_default(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        "locomo_test.eval.query_ov_search_find_memories",
        lambda *args, **kwargs: [{"uri": "viking://user/eval-1/memories/m1.md", "summary": "Direct recall summary."}],
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
    monkeypatch.setattr(
        "locomo_test.eval.commit_openviking_session",
        lambda *args, **kwargs: committed.append((args, kwargs)) or {"status": "accepted", "task_id": "task-1"},
    )

    record = _process_single_question(
        sample_id="conv-1",
        sample_idx=1,
        qi=1,
        qa={"question": "When?", "answer": "Today", "category": "2", "evidence": []},
        cfg=cfg,
        csv_path=str(tmp_path / "qa.csv"),
        question_time="2023-06-27",
    )

    assert committed == []
    assert record["usage"]["total_tokens"] == 7
    assert record["ov_direct_recall_count"] == 1
    assert record["ov_closure_state"] == "qa_direct_recall_only"
    assert record["ov_memory_written"] == "true"
    assert record["ov_index_available"] == "true"


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
    monkeypatch.setattr("locomo_test.eval.query_ov_search_find_memories", lambda *args, **kwargs: [])

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


def test_process_single_question_falls_back_to_api_usage_when_jsonl_usage_is_zero(monkeypatch, tmp_path):
    session_file = tmp_path / "qa-session-zero.jsonl"
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
        lambda *args, **kwargs: ("answer", {"input_tokens": 10, "output_tokens": 20, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 30}),
    )
    monkeypatch.setattr(
        "locomo_test.eval.get_session_id_from_key",
        lambda *args, **kwargs: (session_file.name, str(tmp_path)),
    )
    monkeypatch.setattr(
        "locomo_test.eval.calculate_usage_from_jsonl",
        lambda *args, **kwargs: {"input_tokens": 0, "output_tokens": 0, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 0},
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: "archived.jsonl")
    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", lambda *args, **kwargs: {"status": "accepted", "task_id": ""})
    monkeypatch.setattr("locomo_test.eval.query_ov_session_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval.query_ov_index_consistency", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval.query_ov_search_find_total", lambda *args, **kwargs: 0)
    monkeypatch.setattr("locomo_test.eval.query_ov_search_find_memories", lambda *args, **kwargs: [])

    record = _process_single_question(
        sample_id="conv-1",
        sample_idx=1,
        qi=1,
        qa={"question": "When?", "answer": "Today", "category": "2", "evidence": []},
        cfg=cfg,
        csv_path=str(tmp_path / "qa.csv"),
        question_time="2023-06-27",
    )

    assert record["usage"]["total_tokens"] == 30
    assert record["usage"]["input_tokens"] == 10


def test_process_single_question_injects_direct_recall_into_qa_message(monkeypatch, tmp_path):
    session_file = tmp_path / "qa-session-msg.jsonl"
    session_file.write_text("", encoding="utf-8")

    cfg = Config()
    cfg.memory_mode = "openviking"
    cfg.user = "eval-1"
    cfg.agent_id = "locomo-eval"
    cfg.session = SessionConfig(policy=SessionPolicy.ISOLATED, tail="[]")
    cfg.gateway.state_dir = str(tmp_path)
    cfg.openviking.api_url = "http://ov.local"

    sent_messages = []

    def _send(*args, **kwargs):
        sent_messages.append(args[3])
        return ("answer", {"input_tokens": 1, "output_tokens": 2, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 3})

    monkeypatch.setattr("locomo_test.eval.send_message_with_retry", _send)
    monkeypatch.setattr(
        "locomo_test.eval.get_session_id_from_key",
        lambda *args, **kwargs: (session_file.name, str(tmp_path)),
    )
    monkeypatch.setattr(
        "locomo_test.eval.calculate_usage_from_jsonl",
        lambda *args, **kwargs: {"input_tokens": 1, "output_tokens": 2, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 3},
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: "archived.jsonl")
    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", lambda *args, **kwargs: {"status": "accepted", "task_id": ""})
    monkeypatch.setattr("locomo_test.eval.query_ov_session_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval.query_ov_index_consistency", lambda *args, **kwargs: None)
    monkeypatch.setattr("locomo_test.eval.query_ov_search_find_total", lambda *args, **kwargs: 2)
    monkeypatch.setattr(
        "locomo_test.eval.query_ov_search_find_memories",
        lambda *args, **kwargs: [
            {
                "uri": "viking://user/eval-1/memories/events/support-group.md",
                "summary": "Caroline felt encouraged after attending the LGBTQ support group.",
            }
        ],
    )

    _process_single_question(
        sample_id="conv-1",
        sample_idx=1,
        qi=2,
        qa={"question": "What made Caroline feel encouraged?", "answer": "The support group", "category": "2", "evidence": []},
        cfg=cfg,
        csv_path=str(tmp_path / "qa.csv"),
        question_time="2023-06-27",
    )

    assert len(sent_messages) == 1
    assert "Retrieved memory evidence:" in sent_messages[0]
    assert "Caroline felt encouraged after attending the LGBTQ support group." in sent_messages[0]


def test_query_ov_search_find_memories_returns_memory_rows(monkeypatch, tmp_path):
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
        ok = True

        def json(self):
            return {
                "result": {
                    "memories": [
                        {"uri": "viking://user/user-1/memories/m1.md"},
                        {"uri": "viking://user/user-1/memories/m2.md"},
                    ],
                    "total": 2,
                }
            }

    class _ReadResp:
        ok = True
        content = b"1"

        def json(self):
            return {"result": {"content": "[group chat conversation: 1:56 pm on 8 May, 2023] detail"}}

    def _post(url, *, headers=None, json=None, timeout=None):
        assert url.endswith("/api/v1/search/find")
        assert json["target_uri"] == "viking://user/user-1/memories"
        return _Resp()

    def _get(url, *, headers=None, params=None, timeout=None):
        assert url.endswith("/api/v1/content/read")
        assert params["uri"].endswith("m1.md") or params["uri"].endswith("m2.md")
        return _ReadResp()

    monkeypatch.setattr("locomo_test.eval.requests.post", _post)
    monkeypatch.setattr("locomo_test.eval.requests.get", _get)

    result = query_ov_search_find_memories(
        "http://ov.local",
        "What happened?",
        "viking://user/user-1/memories",
        state_dir=str(tmp_path),
        fallback_agent_id="locomo-eval",
        limit=2,
    )

    assert len(result) == 2
    assert result[0]["uri"].endswith("m1.md")
    assert result[0]["content"].startswith("[group chat conversation:")


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


def test_query_ov_task_token_usage_handles_null_nested_result(monkeypatch, tmp_path):
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
                    "result": None,
                }
            }

    monkeypatch.setattr("locomo_test.eval.requests.get", lambda *args, **kwargs: _Resp())

    from locomo_test.eval import query_ov_task_token_usage

    result = query_ov_task_token_usage(
        "http://ov.local",
        "task-1",
        state_dir=str(tmp_path),
        fallback_agent_id="locomo-eval",
        max_wait=1,
    )

    assert result["task_id"] == "task-1"
    assert result["llm_total"] == 0
    assert result["embedding"] == 0
    assert result["memories"] == 0


def test_query_ov_task_token_usage_falls_back_when_direct_task_is_empty(monkeypatch, tmp_path):
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
    calls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _get(url, *, headers=None, timeout=None, params=None):
        calls.append((url, params))
        if url.endswith("/api/v1/tasks/task-1"):
            return _Resp(
                {
                    "result": {
                        "status": "completed",
                        "task_id": "task-1",
                        "result": {
                            "token_usage": {
                                "llm": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                "embedding": {"total_tokens": 0},
                            },
                            "memories_extracted": {"memory_write": 0, "memory_edit": 0},
                        },
                    }
                }
            )
        assert params == {"task_type": "session_commit", "status": "completed", "limit": 1, "resource_id": "session-1"}
        return _Resp(
            {
                "result": [
                    {
                        "task_id": "task-1b",
                        "status": "completed",
                        "result": {
                            "token_usage": {
                                "llm": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                                "embedding": {"total_tokens": 4},
                            },
                            "memories_extracted": {"memory_write": 2, "memory_edit": 1},
                        },
                    }
                ]
            }
        )

    monkeypatch.setattr("locomo_test.eval.requests.get", _get)

    from locomo_test.eval import query_ov_task_token_usage

    result = query_ov_task_token_usage(
        "http://ov.local",
        "task-1",
        state_dir=str(tmp_path),
        fallback_agent_id="locomo-eval",
        max_wait=1,
        resource_id="session-1",
    )

    assert result["task_id"] == "task-1b"
    assert result["llm_total"] == 3
    assert result["embedding"] == 4
    assert result["memories"] == 3
    assert len(calls) == 2


def test_query_ov_task_token_usage_returns_none_while_task_still_running(monkeypatch, tmp_path):
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
                    "status": "running",
                    "task_id": "task-1",
                    "result": None,
                }
            }

    monkeypatch.setattr("locomo_test.eval.requests.get", lambda *args, **kwargs: _Resp())
    monkeypatch.setattr("locomo_test.eval.time.sleep", lambda *_args, **_kwargs: None)
    clock = {"t": 0.0}

    def _time():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr("locomo_test.eval.time.time", _time)

    from locomo_test.eval import query_ov_task_token_usage

    result = query_ov_task_token_usage(
        "http://ov.local",
        "task-1",
        state_dir=str(tmp_path),
        fallback_agent_id="locomo-eval",
        max_wait=1,
        resource_id="session-1",
    )

    assert result is None


def test_run_qa_reindexes_openviking_memory_root_before_questions(monkeypatch, tmp_path):
    data_path = tmp_path / "locomo.json"
    data_path.write_text(
        json.dumps(
            [
                {
                    "sample_id": "conv-1",
                    "qa": [{"question": "When?", "answer": "Today", "category": "2", "evidence": []}],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cfg = Config()
    cfg.data_file = str(data_path)
    cfg.dataset = "small"
    cfg.memory_mode = "openviking"
    cfg.user = "eval-1"
    cfg.agent_id = "locomo-eval"
    cfg.parallel = 1
    cfg.session = SessionConfig(policy=SessionPolicy.ISOLATED, tail="[]")
    cfg.gateway.state_dir = str(tmp_path)
    cfg.openviking.api_url = "http://ov.local"

    calls = []

    monkeypatch.setattr("locomo_test.eval.load_executed_records", lambda *args, **kwargs: set())
    monkeypatch.setattr("locomo_test.eval.get_sample_question_time", lambda *args, **kwargs: "2023-06-27")
    monkeypatch.setattr(
        "locomo_test.eval._process_single_question",
        lambda *args, **kwargs: {"usage": {"input_tokens": 1, "output_tokens": 2, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 3}},
    )

    def _reindex(*, ov_api_url, user_id, state_dir, fallback_agent_id, timeout):
        calls.append((ov_api_url, user_id, state_dir, fallback_agent_id, timeout))
        return {"ok": True, "target_uri": f"viking://user/{user_id}/memories"}

    monkeypatch.setattr("locomo_test.eval.reindex_ov_memory_root", _reindex)

    usage = run_qa(cfg, str(tmp_path))

    assert usage["total_tokens"] == 3
    assert calls == [("http://ov.local", "eval-1", str(tmp_path), "locomo-eval", 120.0)]
    assert json.loads((tmp_path / "qa_reindex.json").read_text(encoding="utf-8"))["ok"] is True


def test_reindex_ov_memory_root_retries_tree_lock_conflict(monkeypatch, tmp_path):
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

    calls = {"count": 0}

    class _Resp:
        def __init__(self, ok, payload, status_code=200, text=""):
            self.ok = ok
            self._payload = payload
            self.status_code = status_code
            self.text = text
            self.content = b"x"

        def json(self):
            return self._payload

    def _post(url, headers=None, json=None, timeout=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return _Resp(
                False,
                {"error": {"message": "Failed to acquire tree lock for ['/local/acct-1/user/user-1/memories']"}},
                status_code=409,
                text="tree lock",
            )
        return _Resp(True, {"result": {"status": "completed", "uri": json["uri"]}})

    monkeypatch.setattr("locomo_test.eval.requests.post", _post)
    monkeypatch.setattr("locomo_test.eval.time.sleep", lambda *_args, **_kwargs: None)
    times = iter([0.0, 0.1, 0.2, 0.3])
    monkeypatch.setattr("locomo_test.eval.time.monotonic", lambda: next(times))

    from locomo_test.eval import reindex_ov_memory_root

    result = reindex_ov_memory_root(
        ov_api_url="http://ov.local",
        user_id="user-1",
        state_dir=str(tmp_path),
        fallback_agent_id="locomo-eval",
        timeout=5.0,
    )

    assert result["ok"] is True
    assert calls["count"] == 2


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
        "locomo_test.eval._send_message_with_retry_diagnostics",
        lambda *args, **kwargs: (
            "OK",
            {"input_tokens": 1, "output_tokens": 1, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 2},
            {"attempts": 1, "retries_configured": 2, "elapsed_seconds": 1.0, "request_timeout_seconds": 180.0, "wait_schedule_seconds": [], "timeout_hit": False, "final_error": ""},
        ),
    )
    monkeypatch.setattr(
        "locomo_test.eval.get_session_id_from_key",
        lambda *args, **kwargs: (session_file.name, str(tmp_path)),
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: session_file.name)
    monkeypatch.setattr(
        "locomo_test.eval.wait_for_ov_latest_task",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "timeout"},
        ) if kwargs.get("return_diag") else None,
    )
    monkeypatch.setattr(
        "locomo_test.eval.query_ov_task_token_usage",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "no_task"},
        ) if kwargs.get("return_diag") else None,
    )

    committed = []

    def _commit(*, ov_api_url, session_id, keep_recent_count=None, wait=False, **kwargs):
        committed.append((ov_api_url, session_id, keep_recent_count, wait))
        return {"status": "accepted", "task_id": ""}

    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", _commit)

    run_ingest(cfg, str(output_dir))

    assert committed == [("http://ov.local", "ov-session-1", 0, False)]


def test_run_ingest_uses_longer_openviking_task_wait_override(monkeypatch, tmp_path):
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

    monkeypatch.setenv("LOCOMO_OPENVIKING_INGEST_TASK_WAIT_SECONDS", "900")
    monkeypatch.setattr(
        "locomo_test.eval._send_message_with_retry_diagnostics",
        lambda *args, **kwargs: (
            "OK",
            {"input_tokens": 1, "output_tokens": 1, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 2},
            {"attempts": 1, "retries_configured": 2, "elapsed_seconds": 1.0, "request_timeout_seconds": 180.0, "wait_schedule_seconds": [], "timeout_hit": False, "final_error": ""},
        ),
    )
    monkeypatch.setattr(
        "locomo_test.eval.get_session_id_from_key",
        lambda *args, **kwargs: (session_file.name, str(tmp_path)),
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: session_file.name)
    monkeypatch.setattr("locomo_test.eval.query_ov_index_consistency", lambda *args, **kwargs: None)

    seen = {}

    def _query_task(*args, **kwargs):
        seen["max_wait"] = kwargs.get("max_wait")
        return (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "no_task"},
        ) if kwargs.get("return_diag") else None

    def _wait_latest(*args, **kwargs):
        seen["fallback_max_wait"] = kwargs.get("max_wait")
        return (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "timeout"},
        ) if kwargs.get("return_diag") else None

    monkeypatch.setattr("locomo_test.eval.query_ov_task_token_usage", _query_task)
    monkeypatch.setattr("locomo_test.eval.wait_for_ov_latest_task", _wait_latest)
    monkeypatch.setattr(
        "locomo_test.eval.commit_openviking_session",
        lambda *args, **kwargs: {"status": "accepted", "task_id": "task-1"},
    )

    run_ingest(cfg, str(output_dir))

    assert seen["max_wait"] == 900
    assert seen["fallback_max_wait"] == 900


def test_run_ingest_drains_oldest_openviking_session_when_pending_limit_exceeded(monkeypatch, tmp_path):
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
                        "session_1": [{"speaker": "A", "text": "hello-1"}],
                        "session_2_date_time": "1:00 pm on 2 May, 2023",
                        "session_2": [{"speaker": "A", "text": "hello-2"}],
                        "session_3_date_time": "1:00 pm on 3 May, 2023",
                        "session_3": [{"speaker": "A", "text": "hello-3"}],
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
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

    monkeypatch.setenv("LOCOMO_OPENVIKING_MAX_PENDING_INGEST_SESSIONS", "1")
    monkeypatch.setattr(
        "locomo_test.eval._send_message_with_retry_diagnostics",
        lambda *args, **kwargs: (
            "OK",
            {"input_tokens": 1, "output_tokens": 1, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 2},
            {"attempts": 1, "retries_configured": 2, "elapsed_seconds": 1.0, "request_timeout_seconds": 180.0, "wait_schedule_seconds": [], "timeout_hit": False, "final_error": ""},
        ),
    )

    session_files = {
        "ingest-conv-1-session_1": ("ov-session-1.jsonl", str(tmp_path)),
        "ingest-conv-1-session_2": ("ov-session-2.jsonl", str(tmp_path)),
        "ingest-conv-1-session_3": ("ov-session-3.jsonl", str(tmp_path)),
    }
    monkeypatch.setattr(
        "locomo_test.eval.get_session_id_from_key",
        lambda session_key, *args, **kwargs: session_files[session_key],
    )
    monkeypatch.setattr("locomo_test.eval.reset_session", lambda *args, **kwargs: "ignored")
    monkeypatch.setattr("locomo_test.eval.query_ov_index_consistency", lambda *args, **kwargs: None)

    operations = []
    task_ids = iter(["task-1", "task-2", "task-3"])

    def _commit(*, session_id, **kwargs):
        operations.append(f"commit:{session_id}")
        return {"status": "accepted", "task_id": next(task_ids)}

    def _query_task(*args, **kwargs):
        task_id = args[1]
        operations.append(f"query:{task_id}")
        return (
            {"llm_total": 10, "embedding": 5, "memories": 1},
            {"poll_count": 1, "elapsed_seconds": 0.1, "timed_out": False, "fallback_used": False, "final_status": "completed"},
        ) if kwargs.get("return_diag") else {"llm_total": 10, "embedding": 5, "memories": 1}

    monkeypatch.setattr("locomo_test.eval.commit_openviking_session", _commit)
    monkeypatch.setattr("locomo_test.eval.query_ov_task_token_usage", _query_task)
    monkeypatch.setattr(
        "locomo_test.eval.wait_for_ov_latest_task",
        lambda *args, **kwargs: (
            None,
            {"poll_count": 0, "elapsed_seconds": 0.0, "timed_out": False, "fallback_used": False, "final_status": "no_task"},
        ) if kwargs.get("return_diag") else None,
    )

    run_ingest(cfg, str(output_dir))

    assert operations[:5] == [
        "commit:ov-session-1",
        "commit:ov-session-2",
        "query:task-1",
        "commit:ov-session-3",
        "query:task-2",
    ]
