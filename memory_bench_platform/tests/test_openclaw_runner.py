import os

from skills.agents.openclaw.scripts.run_task import build_openclaw_command, build_openclaw_message


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
