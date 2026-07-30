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


def test_plugin_flush_returns_exact_operation(monkeypatch, tmp_path: Path):
    runner = _load_runner()
    monkeypatch.setattr(runner, "_base_url", lambda: "http://ov.test")
    monkeypatch.setattr(
        runner,
        "_task_headers",
        lambda agent_id="": {
            "X-API-Key": "secret",
            "X-OpenViking-Agent": f"namespace_{agent_id}",
        },
    )
    monkeypatch.setattr(runner, "_effective_agent_id", lambda agent_id: f"namespace_{agent_id}")
    requests = []

    def request_json(url, **kwargs):
        requests.append((url, kwargs))
        return {"status": "accepted", "archived": True, "task_id": "task-1"}

    monkeypatch.setattr(runner, "_request_json", request_json)
    result = runner.run(
        _request(
            tmp_path,
            "flush",
            {"session_key": "run:ingest:session-1", "agent_id": "locomo-eval"},
        )
    )

    expected_session_id = runner.session_id_from_key("run:ingest:session-1")
    assert result["status"] == "ok"
    assert result["state"] == "accepted"
    assert result["output"]["operation"] == {
        "id": "task-1",
        "task_id": "task-1",
        "type": "memory_commit",
        "session_id": expected_session_id,
        "session_key": "run:ingest:session-1",
        "agent_id": "locomo-eval",
        "effective_agent_id": "namespace_locomo-eval",
    }
    assert requests[0][0].endswith(f"/api/v1/sessions/{expected_session_id}/commit")
    assert requests[0][1]["body"] == {"keep_recent_count": 0}


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
