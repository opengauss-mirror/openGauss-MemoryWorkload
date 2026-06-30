from locomo_test.memory_backend_adapter import OpenVikingMemoryBackend


def test_openviking_backend_adapter_accepts_ingest_session():
    calls = []

    def _commit(**kwargs):
        calls.append(kwargs)
        return {"status": "accepted", "task_id": "task-1"}

    backend = OpenVikingMemoryBackend(
        api_url="http://ov",
        state_dir="/state",
        user_id="user-1",
        agent_id="agent-1",
        keep_recent_count=0,
        commit_session_fn=_commit,
    )

    accepted = backend.accept_ingest_session("session-1", fallback_agent_id="agent-1--session_1")

    assert accepted.session_id == "session-1"
    assert accepted.task_id == "task-1"
    assert accepted.commit_result["status"] == "accepted"
    assert calls[0]["keep_recent_count"] == 0
    assert calls[0]["fallback_agent_id"] == "agent-1--session_1"


def test_openviking_backend_adapter_waits_ingest_completion_with_latest_fallback():
    def _query_task(*args, **kwargs):
        return ({}, {"poll_count": 1, "elapsed_seconds": 0.2, "timed_out": False})

    def _wait_latest(*args, **kwargs):
        return (
            {"llm_total": 10, "embedding": 5, "memories": 1},
            {"poll_count": 2, "elapsed_seconds": 0.3, "timed_out": False},
        )

    def _consistency(*args, **kwargs):
        return {"ok": True, "missing_record_count": 0}

    backend = OpenVikingMemoryBackend(
        api_url="http://ov",
        state_dir="/state",
        user_id="user-1",
        agent_id="agent-1",
        query_task_usage_fn=_query_task,
        wait_latest_task_fn=_wait_latest,
        consistency_fn=_consistency,
    )

    completion = backend.wait_ingest_completion(
        session_id="session-1",
        task_id="task-1",
        fallback_agent_id="agent-1--session_1",
        max_wait=30,
    )

    assert completion.event == "completed"
    assert completion.token_usage["llm_total"] == 10
    assert completion.wait_diag["poll_count"] == 2
    assert completion.consistency["ok"] is True


def test_openviking_backend_adapter_recalls_question_with_total_and_memories():
    backend = OpenVikingMemoryBackend(
        api_url="http://ov",
        state_dir="/state",
        user_id="user-1",
        agent_id="agent-1",
        search_memories_fn=lambda *args, **kwargs: [{"uri": "m1"}],
        search_total_fn=lambda *args, **kwargs: 3,
    )

    recall = backend.recall_for_question("What happened?", limit=8)

    assert recall.target_uri == "viking://user/user-1/memories"
    assert recall.memories == [{"uri": "m1"}]
    assert recall.total == 3


def test_openviking_backend_adapter_reads_task_usage_with_session_fallback():
    backend = OpenVikingMemoryBackend(
        api_url="http://ov",
        state_dir="/state",
        user_id="user-1",
        agent_id="agent-1",
        query_task_usage_fn=lambda *args, **kwargs: None,
        query_session_usage_fn=lambda *args, **kwargs: {"llm_total": 7, "embedding": 2, "memories": 1},
    )

    usage = backend.read_task_usage(
        session_id="session-1",
        task_id="task-1",
        max_wait=30,
    )

    assert usage["source"] == "session_meta"
    assert usage["usage"]["llm_total"] == 7


def test_openviking_backend_adapter_checks_memory_root_consistency():
    backend = OpenVikingMemoryBackend(
        api_url="http://ov",
        state_dir="/state",
        user_id="user-1",
        agent_id="agent-1",
        consistency_fn=lambda *args, **kwargs: {"ok": False, "missing_record_count": 2},
    )

    consistency = backend.check_consistency()

    assert consistency == {"ok": False, "missing_record_count": 2}
