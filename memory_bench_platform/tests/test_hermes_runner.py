import json

from skills.agents.hermes.scripts.run_task import build_hermes_command, render_hermes_prompt


def test_render_hermes_prompt_includes_system_and_messages():
    request = {
        "task_id": "t1",
        "system_prompt": "Use memory carefully.",
        "messages": [
            {"role": "user", "content": "history line"},
            {"role": "assistant", "content": "assistant line"},
            {"role": "user", "content": "final question"},
        ],
        "metadata": {},
    }
    prompt = render_hermes_prompt(request)
    assert "System instructions" in prompt
    assert "Use memory carefully." in prompt
    assert "[user] history line" in prompt
    assert "[assistant] assistant line" in prompt
    assert "[user] final question" in prompt


def test_build_hermes_command_uses_oneshot_mode(monkeypatch):
    monkeypatch.setenv("HERMES_BIN", "/tmp/hermes")
    request = {
        "task_id": "t1",
        "system_prompt": "Use memory carefully.",
        "messages": [{"role": "user", "content": "Reply with OK"}],
        "metadata": {"model": "MiniMax-M3", "provider": "minimax"},
    }
    cmd = build_hermes_command(request)
    assert cmd[:4] == ["/tmp/hermes", "chat", "-q", cmd[3]]
    assert "-Q" in cmd
    assert "--model" in cmd
    assert "MiniMax-M3" in cmd
    assert "--provider" in cmd
    assert "minimax" in cmd
    assert "--ignore-rules" in cmd


def test_build_hermes_command_supports_env_fallback_provider_and_model(monkeypatch):
    monkeypatch.setenv("HERMES_BIN", "/tmp/hermes")
    monkeypatch.setenv("HERMES_PROVIDER", "minimax-cn")
    monkeypatch.setenv("HERMES_MODEL", "MiniMax-M3")
    request = {
        "task_id": "t1",
        "system_prompt": "",
        "messages": [{"role": "user", "content": "Reply with OK"}],
        "metadata": {},
    }
    cmd = build_hermes_command(request)
    assert "--provider" in cmd
    assert "minimax-cn" in cmd
    assert "--model" in cmd
    assert "MiniMax-M3" in cmd
