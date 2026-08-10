from __future__ import annotations

import importlib.util
from pathlib import Path


RUNNER_PATH = Path("skills/memory_plugins/openclaw-openviking/scripts/run_lifecycle.py")


def _load_runner():
    spec = importlib.util.spec_from_file_location("openclaw_openviking_lifecycle", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(tmp_path: Path, action: str, inputs: dict | None = None) -> dict:
    return {
        "task_id": f"plugin-{action}",
        "action": action,
        "inputs": inputs or {},
        "runtime_context": {"run_id": "run-1", "run_dir": str(tmp_path)},
        "idempotency_key": f"run-1:{action}",
    }


def test_plugin_lifecycle_prepare_set_phases_and_finalize(monkeypatch, tmp_path: Path):
    runner = _load_runner()
    config = {
        "autoCapture": False,
        "autoRecall": True,
        "captureMode": "keyword",
        "commitTokenThreshold": 50,
    }
    writes = []
    unsets = []

    monkeypatch.setattr(
        runner,
        "_config_get",
        lambda path: (
            {"enabled": True, "config": dict(config)}
            if path == "plugins.entries.openviking"
            else "openviking"
            if path == "plugins.slots.contextEngine"
            else dict(config)
        ),
    )

    def set_value(path, value):
        writes.append((path, value))
        config[path.rsplit(".", 1)[-1]] = value

    monkeypatch.setattr(runner, "_config_set", set_value)
    monkeypatch.setattr(runner, "_config_unset", lambda path: unsets.append(path))

    assert runner.run(_request(tmp_path, "validate"))["status"] == "ok"
    prepared = runner.run(_request(tmp_path, "prepare", {"namespace": "run:sample-1"}))
    assert prepared["output"]["namespace"] == "run_sample-1"
    ingest = runner.run(_request(tmp_path, "set_phase", {"phase": "ingest"}))
    assert ingest["output"]["auto_capture"] is True
    assert ingest["output"]["auto_recall"] is False
    assert ingest["output"]["automatic_commit"] is False
    qa = runner.run(_request(tmp_path, "set_phase", {"phase": "qa"}))
    assert qa["output"]["qa_read_only"] is True
    finalized = runner.run(_request(tmp_path, "finalize"))

    assert finalized["status"] == "ok"
    assert finalized["output"]["restored"] is True
    assert (f"{runner.PLUGIN_CONFIG_PATH}.autoCapture", False) in writes
    assert (f"{runner.PLUGIN_CONFIG_PATH}.autoRecall", False) in writes
    assert (
        f"{runner.PLUGIN_CONFIG_PATH}.commitTokenThreshold",
        runner.AUTO_COMMIT_DISABLED_THRESHOLD,
    ) in writes
    assert (f"{runner.PLUGIN_CONFIG_PATH}.commitTokenThreshold", 50) in writes
    assert f"{runner.PLUGIN_CONFIG_PATH}.commitKeepRecentCount" in unsets
    assert f"{runner.PLUGIN_CONFIG_PATH}.agent_prefix" in unsets


def test_plugin_finalize_unsets_fields_missing_before_prepare(monkeypatch, tmp_path: Path):
    runner = _load_runner()
    config = {
        "autoCapture": False,
        "autoRecall": True,
        "commitTokenThreshold": 0,
    }
    unsets = []

    monkeypatch.setattr(runner, "_config_get", lambda _path: dict(config))
    monkeypatch.setattr(runner, "_config_set", lambda path, value: config.__setitem__(path.rsplit(".", 1)[-1], value))
    monkeypatch.setattr(runner, "_config_unset", lambda path: unsets.append(path))

    assert runner.run(_request(tmp_path, "prepare"))["status"] == "ok"
    finalized = runner.run(_request(tmp_path, "finalize"))

    assert finalized["status"] == "ok"
    assert finalized["output"]["unset_fields"] == [
        "captureMode",
        "commitKeepRecentCount",
        "agent_prefix",
    ]
    assert unsets == [
        f"{runner.PLUGIN_CONFIG_PATH}.captureMode",
        f"{runner.PLUGIN_CONFIG_PATH}.commitKeepRecentCount",
        f"{runner.PLUGIN_CONFIG_PATH}.agent_prefix",
    ]


def test_plugin_wait_settle_polls_until_stably_empty(monkeypatch, tmp_path: Path):
    runner = _load_runner()
    counts = iter([2, 0, 0])
    monkeypatch.setenv("OPENVIKING_PLUGIN_API_URL", "http://ov.test")
    monkeypatch.setenv("OPENVIKING_PLUGIN_API_KEY", "secret")
    monkeypatch.setattr(runner, "_active_task_count", lambda *_args, **_kwargs: next(counts))
    monkeypatch.setattr(runner, "_plugin_config", lambda: {})
    monkeypatch.setattr(runner.time, "sleep", lambda *_args, **_kwargs: None)

    result = runner.run(
        _request(
            tmp_path,
            "wait_settle",
            {"grace_seconds": 0, "interval_seconds": 0.5, "timeout_seconds": 10},
        )
    )

    assert result["status"] == "ok"
    assert result["output"]["method"] == "task_list_poll"
    assert result["output"]["poll_count"] == 3


def test_plugin_headers_fall_back_when_openclaw_redacts_api_key(monkeypatch):
    runner = _load_runner()
    monkeypatch.setenv("OPENVIKING_PLUGIN_API_KEY", "runtime-secret")
    monkeypatch.setattr(
        runner,
        "_plugin_config",
        lambda: {
            "apiKey": "__OPENCLAW_REDACTED__",
            "accountId": "account-1",
            "userId": "user-1",
            "agent_prefix": "run-sample",
        },
    )

    headers = runner._task_headers("locomo-eval")

    assert headers == {
        "X-API-Key": "runtime-secret",
        "X-OpenViking-Account": "account-1",
        "X-OpenViking-User": "user-1",
        "X-OpenViking-Agent": "run-sample_locomo-eval",
    }


def test_plugin_flush_delegates_extraction_to_openclaw_compact(monkeypatch, tmp_path: Path):
    runner = _load_runner()
    commands = []

    def run_openclaw(*args, **kwargs):
        commands.append((args, kwargs))
        return '{"ok":true,"compacted":true,"result":{"details":{"commit":{"task_id":"task-1"}}}}'

    monkeypatch.setattr(runner, "_run_openclaw", run_openclaw)
    result = runner.run(
        _request(
            tmp_path,
            "flush",
            {
                "session_key": "run:ingest:session-1",
                "session_handle": {
                    "session_id": "session-1",
                    "gateway_session_key": "agent:locomo-eval:explicit:session-1",
                },
                "agent_id": "locomo-eval",
            },
        )
    )

    assert result["status"] == "ok"
    assert result["state"] == "completed"
    assert result["output"]["operation"] == {
        "id": "task-1",
        "task_id": "task-1",
        "type": "agent_compact",
        "session_id": "session-1",
        "session_key": "run:ingest:session-1",
        "gateway_session_key": "agent:locomo-eval:explicit:session-1",
        "agent_id": "locomo-eval",
        "state": "completed",
    }
    assert commands[0][0][:3] == ("gateway", "call", "sessions.compact")
    assert '{"key": "agent:locomo-eval:explicit:session-1"}' in commands[0][0]


def test_plugin_wait_ready_accepts_adapter_completed_operation(monkeypatch, tmp_path: Path):
    runner = _load_runner()

    result = runner.run(
        _request(
            tmp_path,
            "wait_ready",
            {"operation": {"state": "completed", "task_id": "task-1"}},
        )
    )

    assert result["status"] == "ok"
    assert result["output"]["method"] == "adapter_completed"
    assert result["output"]["poll_count"] == 0


def test_plugin_wait_settle_polls_exact_task(monkeypatch, tmp_path: Path):
    runner = _load_runner()
    states = iter([{"status": "running"}, {"status": "completed", "result": {"memories": 4}}])
    monkeypatch.setattr(runner, "_base_url", lambda: "http://ov.test")
    monkeypatch.setattr(runner, "_task_headers", lambda _agent_id="": {"X-API-Key": "secret"})
    monkeypatch.setattr(runner, "_task_status", lambda *_args, **_kwargs: next(states))
    monkeypatch.setattr(runner.time, "sleep", lambda *_args, **_kwargs: None)

    result = runner.run(
        _request(
            tmp_path,
            "wait_settle",
            {
                "operation": {"task_id": "task-1", "agent_id": "locomo-eval"},
                "grace_seconds": 0,
                "interval_seconds": 0.5,
                "timeout_seconds": 10,
            },
        )
    )

    assert result["status"] == "ok"
    assert result["output"]["method"] == "exact_task_poll"
    assert result["output"]["task_id"] == "task-1"
    assert result["output"]["poll_count"] == 2


def test_plugin_accepts_generic_commit_and_wait_ready_aliases(monkeypatch, tmp_path: Path):
    runner = _load_runner()
    monkeypatch.setattr(runner, "_flush", lambda request: {"action": "commit", "request": request})
    monkeypatch.setattr(runner, "_wait_settle", lambda request: {"action": "wait_ready", "request": request})

    assert runner.run(_request(tmp_path, "commit"))["action"] == "commit"
    assert runner.run(_request(tmp_path, "wait_ready"))["action"] == "wait_ready"
