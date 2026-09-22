from pathlib import Path

from skills.agents.openclaw.scripts.run_task import (
    build_openclaw_command,
    build_openclaw_http_request,
    build_openclaw_message,
    collect_openclaw_session_artifacts,
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
    assert body["input"] == [{"type": "message", "role": "user", "content": large_context}]
    assert body["instructions"] == "Use recalled evidence only."
    assert "System instructions:" not in str(body["input"])
    assert headers["X-OpenClaw-Session-Key"] == "run-1:qa:q1"
    assert headers["X-OpenClaw-Model"] == "openai/gpt-5.6-luna"
    assert headers["Authorization"] == "Bearer secret-token"


def test_openclaw_http_runner_propagates_trace_correlation(monkeypatch):
    monkeypatch.setenv("TRACE_RUN_ID", "run-1")
    request = {
        "task_id": "step-1",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {
            "agent_id": "main",
            "case_id": "case-1",
            "request_id": "request-1",
            "session_id": "session-1",
        },
    }

    _url, headers, _body = build_openclaw_http_request(request)

    assert headers["X-Trace-Run-ID"] == "run-1"
    assert headers["X-Trace-Case-ID"] == "case-1"
    assert headers["X-Trace-Step-ID"] == "step-1"
    assert headers["X-Trace-Session-ID"] == "session-1"
    assert headers["X-Request-ID"] == "request-1"


def test_openclaw_session_collector_returns_explicit_jsonl(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session"}\n', encoding="utf-8")
    artifacts = collect_openclaw_session_artifacts(
        {"metadata": {"session_jsonl": str(session)}},
        agent_id="main",
        session_id="session-1",
    )

    assert artifacts == [
        {
            "kind": "openclaw_session_jsonl",
            "path": str(session),
            "content_type": "application/x-ndjson",
            "size_bytes": session.stat().st_size,
            "tags": ["openclaw", "session", "trace-source"],
        }
    ]


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


def test_http_preserves_message_roles_and_keeps_instructions_separate():
    request = {"system_prompt": "Question date: 2023-06-27. Follow these instructions.",
               "messages": [{"role": "user", "content": "First question"},
                            {"role": "assistant", "content": "First answer"},
                            {"role": "user", "content": "Next question"}],
               "metadata": {"agent_id": "main"}}
    _, _, body = build_openclaw_http_request(request)
    assert body["instructions"] == request["system_prompt"]
    assert [m["role"] for m in body["input"]] == ["user", "assistant", "user"]
    assert body["input"][-1]["content"] == "Next question"


def test_http_reads_actual_gateway_uuid_and_rejects_missing_mapping(monkeypatch, tmp_path):
    import json
    import pytest
    from io import BytesIO
    from types import SimpleNamespace
    from skills.agents.openclaw.scripts import run_task as runner
    monkeypatch.setenv("OGMEM_PLUGIN_STATE_FILE", str(tmp_path / "phase.json"))
    monkeypatch.setenv("OGMEM_GATEWAY_CONTAINER", "test-gateway")
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    store = tmp_path / "agents/main/sessions/sessions.json"
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({"agent:main:run:session": {"sessionId": "actual-uuid"}}))
    monkeypatch.setattr(runner.urllib.request, "urlopen", lambda *a, **k: BytesIO(b'{"status":"completed","output_text":"ok"}'))
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="", stderr=""))
    request = {"messages": [{"role": "user", "content": "Hello"}],
               "metadata": {"agent_id": "main", "session_key": "run:session",
                             "memory_integration": "agent_plugin", "memory_plugin_id": "openclaw-ogmemory"}}
    output = runner.run_openclaw_http(request)["output"]
    assert output["session_id"] == "actual-uuid"
    assert output["session_handle"]["gateway_session_key"] == "agent:main:run:session"
    store.write_text('{}')
    assert runner.run_openclaw_http(request)["output"]["session_id"] == ""


def test_http_failed_payload_is_not_a_successful_answer(monkeypatch):
    import pytest
    from io import BytesIO
    from skills.agents.openclaw.scripts import run_task as runner
    monkeypatch.setattr(runner.urllib.request, "urlopen", lambda *a, **k: BytesIO(b'{"status":"failed","error":{"message":"bad"}}'))
    with pytest.raises(RuntimeError, match="failed or incomplete"):
        runner.run_openclaw_http({"metadata": {"agent_id": "main"}, "messages": []})


import pytest


@pytest.mark.parametrize("transport", ["cli", "http"])
@pytest.mark.parametrize("binding", [
    {"memory_integration": "backend_direct", "memory_plugin_id": None},
    {"memory_integration": "agent_plugin", "memory_plugin_id": "openclaw-openviking"},
    {},
])
def test_unrelated_binding_ignores_stale_ogmemory_environment(monkeypatch, tmp_path, capsys, transport, binding):
    import io
    import json
    from types import SimpleNamespace
    from skills.agents.openclaw.scripts import run_task as runner

    # A deleted old runtime must not be opened, nor its container logs queried.
    monkeypatch.setenv("OGMEM_PLUGIN_STATE_FILE", str(tmp_path / "deleted-runtime/phase.json"))
    monkeypatch.setenv("OPENCLAW_TRANSPORT", transport)
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("OGMEM_GATEWAY_CONTAINER", raising=False)
    request = {"system_prompt": "Answer from evidence", "messages": [{"role": "user", "content": "Q"}],
               "metadata": {"agent_id": "main", "session_key": "new-run:qa", "local": True, **binding}}
    monkeypatch.setattr(runner.sys, "stdin", io.StringIO(json.dumps(request)))
    def execute(cmd, **kwargs):
        assert transport == "cli", "HTTP run must not execute Docker plugin checks"
        return SimpleNamespace(stdout='{"output_text":"ok"}', stderr="")
    monkeypatch.setattr(runner.subprocess, "run", execute)
    monkeypatch.setattr(runner, "build_openclaw_command", lambda req: ["openclaw"])
    monkeypatch.setattr(runner.urllib.request, "urlopen", lambda *a, **kw:
                        io.BytesIO(b'{"status":"completed","output_text":"ok"}'))
    runner.main()
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


@pytest.mark.parametrize("mode,plugin", [
    ("backend_direct", None),
    ("agent_plugin", "openclaw-ogmemory"),
    ("agent_plugin", "openclaw-openviking"),
])
def test_agent_operator_uses_runtime_binding_not_task_metadata(mode, plugin):
    from memory_bench_platform.protocol import StepRecord, WorkflowRuntimeContext
    from memory_bench_platform.workflow_operators import _execute_agent
    context = WorkflowRuntimeContext(run_id="run", run_dir="/tmp/run", benchmark_id="locomo",
        agent_id="openclaw", memory_integration=mode, memory_plugin_id=plugin)
    step = StepRecord(step_id="qa", case_id="case", name="answer", operator_kind="agent",
        inputs={"messages": [{"role": "user", "content": "Q"}],
                "metadata": {"memory_integration": "agent_plugin", "memory_plugin_id": "stale-plugin"}})
    seen = []
    def invoke(agent, task):
        seen.append(task.metadata)
        return {"status": "ok", "turns": [{"text": "answer"}]}
    _execute_agent(step, "openclaw", invoke, context)
    assert seen[0]["memory_integration"] == mode
    assert seen[0]["memory_plugin_id"] == plugin
