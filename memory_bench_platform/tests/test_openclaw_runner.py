import os

from skills.agents.openclaw.scripts.run_task import build_openclaw_command


def test_openclaw_runner_builds_agent_command_from_metadata(monkeypatch):
    monkeypatch.setenv("OPENCLAW_BIN", "/tmp/openclaw")
    monkeypatch.setenv("OPENCLAW_AGENT_ID", "locomo-eval")
    request = {
        "task_id": "t1",
        "messages": [{"role": "user", "content": "Reply with OK"}],
        "metadata": {"thinking": "low", "timeout_seconds": 30},
    }
    cmd = build_openclaw_command(request)
    assert cmd[:4] == ["/tmp/openclaw", "agent", "--message", "Reply with OK"]
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
