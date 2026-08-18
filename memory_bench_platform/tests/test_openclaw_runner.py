import os

from skills.agents.openclaw.scripts.run_task import (
    build_openclaw_command,
    build_openclaw_http_request,
    build_openclaw_message,
    extract_openclaw_response_text,
    resolve_transport,
    session_id_from_key,
)


def test_openclaw_runner_builds_agent_command_from_metadata(monkeypatch):
    monkeypatch.setenv("OPENCLAW_BIN", "/tmp/openclaw")
    monkeypatch.setenv("OPENCLAW_AGENT_ID", "locomo-eval")
    request = {
        "task_id": "t1",
        "messages": [{"role": "user", "content": "Reply with OK"}],
        "metadata": {"thinking": "low", "timeout_seconds": 30},
    }
    cmd = build_openclaw_command(request)
    assert cmd[:3] == ["/tmp/openclaw", "agent", "--message"]
    assert "Reply with OK" in cmd[3]
    assert "--agent" in cmd
    assert "locomo-eval" in cmd
    assert "--json" in cmd


def test_openclaw_runner_requires_a_session_selector(monkeypatch):
    monkeypatch.setenv("OPENCLAW_BIN", "/tmp/openclaw")
    monkeypatch.delenv("OPENCLAW_AGENT_ID", raising=False)
    request = {
        "task_id": "t1",
        "messages": [{"role": "user", "content": "Reply with OK"}],
        "metadata": {},
    }
    try:
        build_openclaw_command(request)
    except ValueError as exc:
        assert "agent_id" in str(exc)
    else:
        raise AssertionError("expected runner to require a selector")


def test_openclaw_runner_flattens_full_rendered_input_into_message(monkeypatch):
    monkeypatch.setenv("OPENCLAW_BIN", "/tmp/openclaw")
    request = {
        "task_id": "t2",
        "system_prompt": "Use the provided history only.",
        "messages": [
            {"role": "user", "content": "history turn 1"},
            {"role": "assistant", "content": "history turn 2"},
            {"role": "user", "content": "final question"},
        ],
        "metadata": {"agent_id": "locomo-eval"},
    }
    prompt = build_openclaw_message(request)
    cmd = build_openclaw_command(request)
    assert "System instructions:" in prompt
    assert "[user] history turn 1" in prompt
    assert "[assistant] history turn 2" in prompt
    assert "[user] final question" in prompt
    assert cmd[3] == prompt


def test_openclaw_runner_maps_semantic_session_key_to_stable_session_id(monkeypatch):
    monkeypatch.setenv("OPENCLAW_BIN", "/tmp/openclaw")
    request = {
        "task_id": "t3",
        "messages": [{"role": "user", "content": "remember this"}],
        "metadata": {
            "agent_id": "locomo-eval",
            "session_key": "run-1:ingest:session-1",
        },
    }

    cmd = build_openclaw_command(request)

    assert "--session-key" not in cmd
    assert cmd[cmd.index("--session-id") + 1] == session_id_from_key(
        "run-1:ingest:session-1"
    )


def test_openclaw_http_runner_puts_large_context_in_request_body(monkeypatch):
    monkeypatch.setenv("OPENCLAW_TRANSPORT", "http")
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:38789")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "secret-token")
    large_context = "memory evidence\n" * 20000
    request = {
        "task_id": "large-context",
        "system_prompt": "Use recalled evidence only.",
        "messages": [{"role": "user", "content": large_context}],
        "metadata": {
            "agent_id": "main",
            "session_key": "run-1:qa:q1",
            "model": "openai/gpt-5.6-luna",
        },
    }

    url, headers, body = build_openclaw_http_request(request)

    assert resolve_transport() == "http"
    assert url == "http://127.0.0.1:38789/v1/responses"
    assert body["model"] == "openclaw/main"
    assert large_context.strip() in body["input"]
    assert len(body["input"]) > 300000
    assert headers["X-OpenClaw-Session-Key"] == "run-1:qa:q1"
    assert headers["X-OpenClaw-Model"] == "openai/gpt-5.6-luna"
    assert headers["Authorization"] == "Bearer secret-token"


def test_openclaw_runner_defaults_to_cli_without_gateway_url(monkeypatch):
    monkeypatch.delenv("OPENCLAW_TRANSPORT", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_BASE_URL", raising=False)

    assert resolve_transport() == "cli"


def test_openclaw_http_runner_extracts_responses_api_text():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "answer from OpenClaw"}],
            }
        ]
    }

    assert extract_openclaw_response_text(payload) == "answer from OpenClaw"
