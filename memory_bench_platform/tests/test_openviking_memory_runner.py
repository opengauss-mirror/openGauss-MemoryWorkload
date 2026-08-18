import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace


RUNNER_PATH = Path("skills/memories/openviking/scripts/run_operation.py")


def _load_runner():
    spec = importlib.util.spec_from_file_location("openviking_memory_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(action: str, inputs: dict) -> dict:
    return {
        "task_id": f"{action}-step",
        "action": action,
        "inputs": inputs,
        "runtime_context": {
            "run_id": "run-1",
            "run_dir": "/tmp/run-1",
            "benchmark_id": "ovtest-memory",
            "agent_id": "generic-cli",
            "memory_id": "openviking",
            "run_contract": {},
            "version_selection": {},
        },
        "idempotency_key": f"run-1:case-1:{action}-step",
    }


def _environment() -> dict[str, str]:
    return {
        "OPENVIKING_API_URL": "http://openviking.test:1933",
        "OPENVIKING_API_KEY": "secret-api-key",
        "OPENVIKING_ACCOUNT_ID": "account-1",
        "OPENVIKING_USER_ID": "user-1",
        "OPENVIKING_AGENT_ID": "agent-1",
        "OPENVIKING_BIN": "/usr/local/bin/ov",
    }


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_ingest_uses_session_message_api_without_committing():
    runner = _load_runner()
    calls = []
    content = "For systems programming I prefer Go over Python."
    request_payload = _request("ingest", {"content": content})
    expected_session_id = "mbp-" + hashlib.sha256(
        request_payload["idempotency_key"].encode("utf-8")
    ).hexdigest()[:24]

    def fake_urlopen(request, timeout):
        calls.append(
            {
                "url": request.full_url,
                "method": request.method,
                "headers": {key.lower(): value for key, value in request.header_items()},
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _JsonResponse({"status": "ok", "result": {"message_id": "message-1"}})

    def fail_command(*args, **kwargs):
        del args, kwargs
        raise AssertionError("ingest must not invoke ov")

    result = runner.run_operation(
        request_payload,
        environ=_environment(),
        command_runner=fail_command,
        urlopen=fake_urlopen,
    )

    assert [call["url"] for call in calls] == [
        f"http://openviking.test:1933/api/v1/sessions/{expected_session_id}/messages",
    ]
    assert [call["method"] for call in calls] == ["POST"]
    assert calls[0]["body"] == {"role": "user", "content": content}
    for call in calls:
        assert call["headers"]["content-type"] == "application/json"
        assert call["headers"]["x-api-key"] == "secret-api-key"
        assert call["headers"]["x-openviking-account"] == "account-1"
        assert call["headers"]["x-openviking-user"] == "user-1"
        assert call["headers"]["x-openviking-agent"] == "agent-1"
        assert call["timeout"] == 30
    assert result["status"] == "ok"
    assert result["state"] == "completed"
    assert result["operation"] == {
        "resource_id": expected_session_id,
        "session_id": expected_session_id,
        "status_probe": "none",
        "type": "memory_ingest",
    }
    serialized = json.dumps(result)
    assert content not in serialized
    assert "secret-api-key" not in serialized


def test_ingest_uses_explicit_session_id():
    runner = _load_runner()
    calls = []

    def fake_urlopen(request, timeout):
        del timeout
        calls.append(request.full_url)
        return _JsonResponse({"status": "ok", "result": {"message_id": "message-1"}})

    result = runner.run_operation(
        _request(
            "ingest",
            {"content": "remember this", "session_id": "session-explicit"},
        ),
        environ=_environment(),
        urlopen=fake_urlopen,
    )

    assert calls == [
        "http://openviking.test:1933/api/v1/sessions/session-explicit/messages",
    ]
    assert result["operation"]["session_id"] == "session-explicit"


def test_episode_scope_changes_identity_and_preserves_event_time():
    runner = _load_runner()
    captured = {}

    def fake_urlopen(request, timeout):
        del timeout
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _JsonResponse({"status": "ok", "result": {"message_id": "message-1"}})

    result = runner.run_operation(
        _request(
            "ingest",
            {
                "content": "I prefer tea.",
                "occurred_at": "2026-01-02T10:00:00Z",
                "scope_id": "run-1:persona-12",
            },
        ),
        environ=_environment(),
        urlopen=fake_urlopen,
    )

    scoped_agent = captured["headers"]["x-openviking-agent"]
    assert scoped_agent.startswith("agent-1--episode-")
    assert captured["body"]["content"] == "[Occurred at: 2026-01-02T10:00:00Z]\nI prefer tea."
    assert result["operation"]["scope_id"] == "run-1:persona-12"
    assert result["operation"]["target_identity"]["agent_id"] == scoped_agent


def test_flush_uses_commit_api_and_returns_task_operation():
    runner = _load_runner()

    def fake_urlopen(request, timeout):
        del timeout
        assert request.full_url.endswith("/api/v1/sessions/session-1/commit")
        assert json.loads(request.data.decode("utf-8")) == {"keep_recent_count": 0}
        return _JsonResponse({
            "status": "ok",
            "result": {
                "session_id": "session-1",
                "status": "accepted",
                "task_id": "task-1",
                "archive_uri": "viking://session/session-1/history/archive_001",
                "archived": True,
            },
        })

    result = runner.run_operation(
        _request("flush", {"session_id": "session-1"}),
        environ=_environment(),
        urlopen=fake_urlopen,
    )

    assert result["status"] == "ok"
    assert result["state"] == "accepted"
    assert result["operation"] == {
        "task_id": "task-1",
        "resource_id": "session-1",
        "session_id": "session-1",
        "archive_uri": "viking://session/session-1/history/archive_001",
        "status_probe": "task",
        "type": "memory_flush",
    }


def test_flush_fails_when_commit_omits_task_id():
    runner = _load_runner()
    call_count = 0

    def fake_urlopen(request, timeout):
        nonlocal call_count
        del request, timeout
        call_count += 1
        return _JsonResponse(
            {
                "status": "ok",
                "result": {
                    "session_id": "session-1",
                    "status": "accepted",
                    "archive_uri": "viking://session/session-1/history/archive_001",
                    "archived": True,
                },
            }
        )

    result = runner.run_operation(
        _request("flush", {"session_id": "session-1"}),
        environ=_environment(),
        urlopen=fake_urlopen,
    )

    assert result["status"] == "failed"
    assert result["state"] == "failed"
    assert result["operation"] == {}
    assert result["error"]["type"] == "missing_task_id"


def test_status_uses_task_api_with_openviking_identity_headers():
    runner = _load_runner()
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "status": "ok",
                    "result": {
                        "task_id": "task-1",
                        "resource_id": "session-1",
                        "status": "running",
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        captured["timeout"] = timeout
        return FakeResponse()

    result = runner.run_operation(
        _request("status", {"operation": {"task_id": "task-1"}}),
        environ=_environment(),
        urlopen=fake_urlopen,
    )

    assert captured["url"] == "http://openviking.test:1933/api/v1/tasks/task-1"
    assert captured["headers"]["x-api-key"] == "secret-api-key"
    assert captured["headers"]["x-openviking-account"] == "account-1"
    assert captured["headers"]["x-openviking-user"] == "user-1"
    assert captured["headers"]["x-openviking-agent"] == "agent-1"
    assert result["status"] == "ok"
    assert result["state"] == "running"
    assert result["operation"]["task_id"] == "task-1"
    assert result["output"]["resource_id"] == "session-1"


def test_status_without_task_id_uses_short_ov_wait_probe():
    runner = _load_runner()
    captured = {}

    def fake_command(command, **kwargs):
        del kwargs
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout='{"result":"OK"}', stderr="")

    result = runner.run_operation(
        _request("status", {"operation": {"status_probe": "wait"}}),
        environ=_environment(),
        command_runner=fake_command,
    )

    assert captured["command"] == ["/usr/local/bin/ov", "-o", "json", "wait", "--timeout", "1"]
    assert result["status"] == "ok"
    assert result["state"] == "completed"


def test_failed_status_does_not_forward_backend_error_content():
    runner = _load_runner()
    backend_content = "sensitive ingest content"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "status": "ok",
                    "result": {
                        "task_id": "task-1",
                        "status": "failed",
                        "error": backend_content,
                    },
                }
            ).encode("utf-8")

    result = runner.run_operation(
        _request("status", {"operation": {"task_id": "task-1"}}),
        environ=_environment(),
        urlopen=lambda request, timeout: FakeResponse(),
    )

    serialized = json.dumps(result)
    assert result["status"] == "failed"
    assert result["state"] == "failed"
    assert result["error"]["message"] == "OpenViking task failed"
    assert backend_content not in serialized


def test_recall_normalizes_memories_and_evidence_text():
    runner = _load_runner()
    captured = {}

    def fake_command(command, **kwargs):
        del kwargs
        captured["command"] = command
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": {
                        "memories": [
                            {"uri": "viking://memory/1", "content": "Go is preferred."},
                            {"uri": "viking://memory/2", "abstract": "Python is second."},
                        ]
                    }
                }
            ),
            stderr="",
        )

    result = runner.run_operation(
        _request("recall", {"query": "preferred systems language", "node_limit": 5}),
        environ=_environment(),
        command_runner=fake_command,
    )

    assert captured["command"] == [
        "/usr/local/bin/ov",
        "-o",
        "json",
        "find",
        "preferred systems language",
        "--node-limit",
        "5",
    ]
    assert result["status"] == "ok"
    assert result["state"] == "completed"
    assert result["output"]["count"] == 2
    assert result["output"]["evidence_text"] == "Go is preferred.\nPython is second."
    assert result["output"]["target_identity"] == {
        "account_id": "account-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
    }


def test_recall_preserves_empty_collection_as_zero_results():
    runner = _load_runner()

    def fake_command(command, **kwargs):
        del command, kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": {
                        "memories": [],
                        "resources": [],
                        "skills": [],
                        "total": 0,
                    }
                }
            ),
            stderr="",
        )

    result = runner.run_operation(
        _request("recall", {"query": "missing fact"}),
        environ=_environment(),
        command_runner=fake_command,
    )

    assert result["output"]["count"] == 0
    assert result["output"]["memories"] == []
    assert result["output"]["evidence_text"] == ""


def test_runner_redacts_content_and_api_key_from_errors():
    runner = _load_runner()
    content = "sensitive ingest content"
    environment = _environment()

    def fake_urlopen(request, timeout):
        del request, timeout
        raise RuntimeError(
            f"failed for {content} using {environment['OPENVIKING_API_KEY']}"
        )

    result = runner.run_operation(
        _request("ingest", {"content": content}),
        environ=environment,
        urlopen=fake_urlopen,
    )

    serialized = json.dumps(result)
    assert result["status"] == "failed"
    assert result["state"] == "failed"
    assert content not in serialized
    assert environment["OPENVIKING_API_KEY"] not in serialized
    assert "[REDACTED]" in serialized
