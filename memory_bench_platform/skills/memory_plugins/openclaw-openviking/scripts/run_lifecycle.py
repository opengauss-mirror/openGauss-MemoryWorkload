from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import urllib.parse
import urllib.request


PLUGIN_CONFIG_PATH = "plugins.entries.openviking.config"
AUTO_COMMIT_DISABLED_THRESHOLD = 100_000
MANAGED_FIELDS = (
    "autoCapture",
    "autoRecall",
    "captureMode",
    "commitTokenThreshold",
    "commitKeepRecentCount",
    "agent_prefix",
)
TERMINAL_SUCCESS = {"completed", "complete", "success", "succeeded", "done"}
TERMINAL_FAILURE = {"failed", "error", "cancelled", "canceled"}
REDACTED_SECRET_MARKERS = {
    "***",
    "[redacted]",
    "<redacted>",
    "__openclaw_redacted__",
}


def _openclaw_bin() -> str:
    configured = os.environ.get("OPENCLAW_BIN")
    if configured:
        return configured
    found = shutil.which("openclaw")
    if found:
        return found
    raise FileNotFoundError("openclaw binary not found")


def _run_openclaw(*args: str) -> str:
    proc = subprocess.run(
        [_openclaw_bin(), *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "openclaw command failed")
    return proc.stdout.strip()


def _parse_json_output(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _config_get(path: str) -> Any:
    return _parse_json_output(_run_openclaw("config", "get", path, "--json"))


def _config_set(path: str, value: Any) -> None:
    _run_openclaw("config", "set", path, json.dumps(value), "--json")


def _config_unset(path: str) -> None:
    try:
        _config_get(path)
    except RuntimeError as exc:
        if "Config path not found" in str(exc):
            return
        raise
    _run_openclaw("config", "unset", path)


def _state_path(request: dict[str, Any]) -> Path:
    runtime = request.get("runtime_context", {})
    run_dir = Path(str(runtime.get("run_dir") or "."))
    path = run_dir / "artifacts" / "memory_plugin" / "openclaw-openviking-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _ok(
    output: dict[str, Any],
    *,
    state: str = "completed",
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "state": state,
        "output": output,
        "metrics": [],
        "artifacts": artifacts or [],
        "error": {},
    }


def _validate() -> dict[str, Any]:
    entry = _config_get("plugins.entries.openviking")
    slot = _config_get("plugins.slots.contextEngine")
    enabled = isinstance(entry, dict) and entry.get("enabled") is not False
    slot_value = slot if isinstance(slot, str) else str(slot or "").strip('"')
    if not enabled:
        raise RuntimeError("OpenViking plugin is not enabled in OpenClaw")
    if slot_value != "openviking":
        raise RuntimeError(f"OpenClaw contextEngine slot is {slot_value!r}, expected 'openviking'")
    config = entry.get("config", {}) if isinstance(entry, dict) else {}
    return _ok(
        {
            "plugin_enabled": True,
            "context_engine": slot_value,
            "auto_recall": bool(config.get("autoRecall", True)),
            "auto_capture": bool(config.get("autoCapture", True)),
        }
    )


def _prepare(request: dict[str, Any]) -> dict[str, Any]:
    path = _state_path(request)
    config = _config_get(PLUGIN_CONFIG_PATH)
    if not isinstance(config, dict):
        raise RuntimeError("OpenViking plugin config is unavailable")
    if not path.exists():
        snapshot = {
            "schema_version": 1,
            "managed_fields": {
                field: {"present": field in config, "value": config.get(field)}
                for field in MANAGED_FIELDS
            },
        }
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    inputs = request.get("inputs", {})
    namespace = _sanitize_identifier(str(inputs.get("namespace") or ""))
    if namespace:
        _config_set(f"{PLUGIN_CONFIG_PATH}.agent_prefix", namespace)
    return _ok(
        {
            "namespace": namespace,
            "snapshot": str(path),
        },
        artifacts=[{"kind": "memory_plugin_state", "path": str(path), "content_type": "application/json"}],
    )


def session_id_from_key(session_key: str) -> str:
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
    return "-".join(
        [
            digest[:8],
            digest[8:12],
            digest[12:16],
            digest[16:20],
            digest[20:32],
        ]
    )


def _sanitize_identifier(raw: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def _plugin_config() -> dict[str, Any]:
    config = _config_get(PLUGIN_CONFIG_PATH)
    return config if isinstance(config, dict) else {}


def _usable_secret(value: Any) -> str:
    candidate = str(value or "").strip()
    if candidate.lower() in REDACTED_SECRET_MARKERS:
        return ""
    return candidate


def _effective_agent_id(agent_id: str, config: dict[str, Any] | None = None) -> str:
    config = config or _plugin_config()
    base = _sanitize_identifier(agent_id) or "main"
    prefix = _sanitize_identifier(str(config.get("agent_prefix") or config.get("agentId") or ""))
    return f"{prefix}_{base}" if prefix else base


def _task_headers(agent_id: str = "") -> dict[str, str]:
    config = _plugin_config()
    key = (
        _usable_secret(config.get("apiKey"))
        or _usable_secret(os.environ.get("OPENVIKING_PLUGIN_API_KEY"))
        or _usable_secret(os.environ.get("OPENVIKING_API_KEY"))
    )
    headers: dict[str, str] = {}
    if key:
        headers["X-API-Key"] = key
    account_id = str(config.get("accountId") or os.environ.get("OPENVIKING_PLUGIN_ACCOUNT_ID") or "")
    user_id = str(config.get("userId") or os.environ.get("OPENVIKING_PLUGIN_USER_ID") or "")
    base_agent_id = agent_id or os.environ.get("OPENVIKING_PLUGIN_AGENT_ID") or "main"
    if account_id:
        headers["X-OpenViking-Account"] = account_id
    if user_id:
        headers["X-OpenViking-User"] = user_id
    headers["X-OpenViking-Agent"] = _effective_agent_id(str(base_agent_id), config)
    return headers


def _base_url() -> str:
    configured = os.environ.get("OPENVIKING_PLUGIN_API_URL") or os.environ.get("OPENVIKING_API_URL")
    if configured:
        return configured.rstrip("/")
    return str(_plugin_config().get("baseUrl") or "").rstrip("/")


def _request_json(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    timeout: float,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_headers = dict(headers)
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8") or "{}")
    result = payload.get("result", payload) if isinstance(payload, dict) else {}
    if not isinstance(result, dict):
        raise ValueError("OpenViking API returned an invalid result")
    return result


def _find_first_string(value: Any, names: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in names and item is not None:
                return str(item)
        for item in value.values():
            found = _find_first_string(item, names)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_string(item, names)
            if found:
                return found
    return ""


def _active_task_count(base_url: str, timeout: float, headers: dict[str, str]) -> int:
    url = f"{base_url.rstrip('/')}/api/v1/tasks?{urllib.parse.urlencode({'limit': 200})}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8") or "{}")
    result = payload.get("result", []) if isinstance(payload, dict) else []
    tasks = result if isinstance(result, list) else result.get("items", []) if isinstance(result, dict) else []
    terminal = {"completed", "failed", "cancelled", "canceled", "success", "succeeded"}
    return sum(1 for item in tasks if str(item.get("status", "")).lower() not in terminal)


def _flush(request: dict[str, Any]) -> dict[str, Any]:
    inputs = request.get("inputs", {})
    session_key = str(inputs.get("session_key") or "").strip()
    session_id = str(inputs.get("session_id") or "").strip()
    if not session_id and session_key:
        session_id = session_id_from_key(session_key)
    if not session_id:
        raise ValueError("flush requires session_key or session_id")

    agent_id = str(inputs.get("agent_id") or "main")
    base_url = _base_url()
    headers = _task_headers(agent_id)
    if not base_url or not headers.get("X-API-Key"):
        raise RuntimeError("OpenViking plugin API URL and tenant API key are required for flush")
    timeout = float(inputs.get("request_timeout_seconds", 30))
    result = _request_json(
        f"{base_url}/api/v1/sessions/{urllib.parse.quote(session_id, safe='')}/commit",
        method="POST",
        headers=headers,
        timeout=timeout,
        body={"keep_recent_count": 0},
    )
    task_id = _find_first_string(result, {"task_id", "taskId"})
    if not task_id:
        raise RuntimeError("OpenViking plugin flush did not return a task_id")
    operation = {
        "id": task_id,
        "task_id": task_id,
        "type": "memory_commit",
        "session_id": session_id,
        "session_key": session_key,
        "agent_id": agent_id,
        "effective_agent_id": _effective_agent_id(agent_id),
    }
    return _ok(
        {
            "operation": operation,
            "accepted": True,
            "archived": bool(result.get("archived", False)),
            "task_id": task_id,
            "session_id": session_id,
        },
        state="accepted",
    )


def _task_status(
    base_url: str,
    task_id: str,
    *,
    timeout: float,
    headers: dict[str, str],
) -> dict[str, Any]:
    return _request_json(
        f"{base_url}/api/v1/tasks/{urllib.parse.quote(task_id, safe='')}",
        method="GET",
        headers=headers,
        timeout=timeout,
    )


def _wait_settle(request: dict[str, Any]) -> dict[str, Any]:
    inputs = request.get("inputs", {})
    grace = float(inputs.get("grace_seconds", os.environ.get("OPENVIKING_PLUGIN_GRACE_SECONDS", "5")))
    timeout = float(inputs.get("timeout_seconds", os.environ.get("OPENVIKING_PLUGIN_SETTLE_TIMEOUT", "600")))
    interval = max(0.5, float(inputs.get("interval_seconds", 2)))
    base_url = _base_url()
    operation = inputs.get("operation", {})
    if not isinstance(operation, dict):
        operation = {}
    task_id = str(inputs.get("task_id") or operation.get("task_id") or operation.get("id") or "")
    agent_id = str(operation.get("agent_id") or inputs.get("agent_id") or "main")
    headers = _task_headers(agent_id)
    time.sleep(max(0.0, grace))
    started = time.monotonic()
    polls = 0

    if not base_url or not headers.get("X-API-Key"):
        if task_id:
            raise RuntimeError(
                "OpenViking plugin API URL and tenant API key are required to wait for an exact task"
            )
        fallback = float(inputs.get("fallback_wait_seconds", 20))
        time.sleep(max(0.0, fallback))
        return _ok({"method": "sleep", "grace_seconds": grace, "fallback_wait_seconds": fallback})

    if task_id:
        while time.monotonic() - started < timeout:
            polls += 1
            task = _task_status(
                base_url,
                task_id,
                timeout=min(30.0, interval + 10.0),
                headers=headers,
            )
            backend_status = str(task.get("status") or "").strip().lower()
            if backend_status in TERMINAL_SUCCESS:
                return _ok(
                    {
                        "method": "exact_task_poll",
                        "task_id": task_id,
                        "backend_status": backend_status,
                        "poll_count": polls,
                        "elapsed_seconds": round(time.monotonic() - started + grace, 3),
                        "task_result": task.get("result", {}),
                    }
                )
            if backend_status in TERMINAL_FAILURE:
                raise RuntimeError(f"OpenViking plugin task {task_id} failed with status {backend_status}")
            time.sleep(interval)
        raise TimeoutError(f"OpenViking plugin task {task_id} did not settle within {timeout} seconds")

    # Backward-compatible fallback for integrations that cannot expose an exact operation id.
    stable_zero = 0
    while time.monotonic() - started < timeout:
        polls += 1
        active = _active_task_count(base_url, min(30.0, interval + 10.0), headers)
        if active == 0:
            stable_zero += 1
            if stable_zero >= 2:
                return _ok(
                    {
                        "method": "task_list_poll",
                        "poll_count": polls,
                        "active_tasks": 0,
                        "elapsed_seconds": round(time.monotonic() - started + grace, 3),
                    }
                )
        else:
            stable_zero = 0
        time.sleep(interval)
    raise TimeoutError(f"OpenViking plugin tasks did not settle within {timeout} seconds")


def _set_phase(request: dict[str, Any]) -> dict[str, Any]:
    inputs = request.get("inputs", {})
    phase = str(inputs.get("phase") or "").strip().lower()
    if phase == "ingest":
        _config_set(f"{PLUGIN_CONFIG_PATH}.autoCapture", True)
        _config_set(f"{PLUGIN_CONFIG_PATH}.autoRecall", False)
        _config_set(f"{PLUGIN_CONFIG_PATH}.captureMode", "semantic")
        _config_set(f"{PLUGIN_CONFIG_PATH}.commitTokenThreshold", AUTO_COMMIT_DISABLED_THRESHOLD)
        _config_set(f"{PLUGIN_CONFIG_PATH}.commitKeepRecentCount", 0)
        return _ok(
            {
                "phase": "ingest",
                "auto_capture": True,
                "auto_recall": False,
                "capture_mode": "semantic",
                "automatic_commit": False,
            }
        )
    if phase == "qa":
        _config_set(f"{PLUGIN_CONFIG_PATH}.autoCapture", False)
        _config_set(f"{PLUGIN_CONFIG_PATH}.autoRecall", True)
        return _ok(
            {
                "phase": "qa",
                "auto_capture": False,
                "auto_recall": True,
                "qa_read_only": True,
            }
        )
    raise ValueError(f"unsupported memory plugin phase: {phase or '<missing>'}")


def _enter_qa() -> dict[str, Any]:
    return _set_phase({"inputs": {"phase": "qa"}})


def _finalize(request: dict[str, Any]) -> dict[str, Any]:
    path = _state_path(request)
    if not path.exists():
        return _ok({"restored": False, "reason": "snapshot_missing"})
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    restored = []
    unset = []
    managed = snapshot.get("managed_fields") if isinstance(snapshot, dict) else None
    if isinstance(managed, dict):
        for field in MANAGED_FIELDS:
            field_state = managed.get(field, {})
            if field_state.get("present"):
                _config_set(f"{PLUGIN_CONFIG_PATH}.{field}", field_state.get("value"))
                restored.append(field)
            else:
                _config_unset(f"{PLUGIN_CONFIG_PATH}.{field}")
                unset.append(field)
    else:
        # Backward compatibility with snapshots written by the first prototype.
        for field in MANAGED_FIELDS:
            if field in snapshot and snapshot[field] is not None:
                _config_set(f"{PLUGIN_CONFIG_PATH}.{field}", snapshot[field])
                restored.append(field)
    return _ok(
        {
            "restored": True,
            "restored_fields": restored,
            "unset_fields": unset,
            "snapshot": str(path),
        }
    )


def run(request: dict[str, Any]) -> dict[str, Any]:
    action = str(request.get("action") or "")
    if action == "validate":
        return _validate()
    if action == "prepare":
        return _prepare(request)
    if action == "set_phase":
        return _set_phase(request)
    if action == "flush":
        return _flush(request)
    if action == "wait_settle":
        return _wait_settle(request)
    if action == "enter_qa":
        return _enter_qa()
    if action == "finalize":
        return _finalize(request)
    raise ValueError(f"unsupported memory plugin action: {action}")


def main() -> None:
    request = json.load(sys.stdin)
    try:
        result = run(request)
    except Exception as exc:
        result = {
            "status": "failed",
            "state": "failed",
            "output": {},
            "metrics": [],
            "artifacts": [],
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
