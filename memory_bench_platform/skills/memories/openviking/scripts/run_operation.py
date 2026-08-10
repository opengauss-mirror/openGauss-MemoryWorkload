from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
import urllib.parse
import urllib.request


def run_operation(
    request: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
    command_runner: Callable[..., Any] = subprocess.run,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    environment = dict(os.environ if environ is None else environ)
    action = str(request.get("action", "") or "")
    inputs = request.get("inputs", {})
    if not isinstance(inputs, dict):
        inputs = {}
    environment = _scoped_environment(environment, inputs)
    started = time.perf_counter()
    temporary_config: Path | None = None
    temporary_home: Path | None = None
    secrets = _secret_values(environment, inputs)

    try:
        cli_environment, temporary_config, temporary_home = _prepare_cli_environment(environment)
        if action == "ingest":
            result = _run_ingest(request, inputs, environment, urlopen, secrets)
        elif action == "flush":
            result = _run_flush(inputs, environment, urlopen, secrets)
        elif action == "status":
            result = _run_status(
                inputs,
                cli_environment,
                environment,
                command_runner,
                urlopen,
            )
        elif action == "recall":
            result = _run_recall(inputs, cli_environment, environment, command_runner, secrets)
        else:
            result = _failed_result(
                "unsupported_action",
                f"OpenViking memory runner does not support action: {action}",
                secrets,
            )
    except Exception as exc:
        result = _failed_result(type(exc).__name__, str(exc), secrets)
    finally:
        if temporary_config is not None:
            try:
                temporary_config.unlink(missing_ok=True)
            except OSError:
                pass
        if temporary_home is not None:
            shutil.rmtree(temporary_home, ignore_errors=True)

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    result.setdefault("metrics", []).append(
        {"name": "memory_runner_ms", "value": elapsed_ms, "unit": "ms"}
    )
    return result


def _run_ingest(
    request: dict[str, Any],
    inputs: dict[str, Any],
    environment: dict[str, str],
    urlopen: Callable[..., Any],
    secrets: list[str],
) -> dict[str, Any]:
    content = inputs.get("content")
    if not isinstance(content, str) or not content:
        return _failed_result("invalid_input", "memory.ingest requires inputs.content", secrets)
    occurred_at = str(inputs.get("occurred_at") or inputs.get("timestamp") or "").strip()
    if occurred_at:
        content = f"[Occurred at: {occurred_at}]\n{content}"
    session_id = _session_id(request, inputs)
    base_url = str(environment.get("OPENVIKING_API_URL") or "http://127.0.0.1:1933").rstrip("/")
    encoded_session_id = urllib.parse.quote(session_id, safe="")
    timeout = float(inputs.get("request_timeout_seconds", 30))
    _post_json(
        f"{base_url}/api/v1/sessions/{encoded_session_id}/messages",
        {"role": "user", "content": content},
        environment,
        urlopen,
        timeout,
    )
    operation = {
        "resource_id": session_id,
        "session_id": session_id,
        "status_probe": "none",
        "type": "memory_ingest",
    }
    _attach_scope(operation, inputs, environment)
    return {
        "status": "ok",
        "state": "completed",
        "operation": operation,
        "output": {
            "accepted": True,
            "session_id": session_id,
        },
        "metrics": [],
        "artifacts": [],
        "error": {},
    }


def _run_flush(
    inputs: dict[str, Any],
    environment: dict[str, str],
    urlopen: Callable[..., Any],
    secrets: list[str],
) -> dict[str, Any]:
    source_operation = inputs.get("operation", {})
    if not isinstance(source_operation, dict):
        source_operation = {}
    session_id = str(inputs.get("session_id") or source_operation.get("session_id") or "").strip()
    if not session_id:
        return _failed_result("invalid_input", "memory.flush requires inputs.session_id", secrets)
    base_url = str(environment.get("OPENVIKING_API_URL") or "http://127.0.0.1:1933").rstrip("/")
    encoded_session_id = urllib.parse.quote(session_id, safe="")
    timeout = float(inputs.get("request_timeout_seconds", 30))
    commit_result = _post_json(
        f"{base_url}/api/v1/sessions/{encoded_session_id}/commit",
        {"keep_recent_count": 0},
        environment,
        urlopen,
        timeout,
    )

    task_id = _find_first_string(commit_result, {"task_id", "taskId"})
    if not task_id:
        return _failed_result(
            "missing_task_id",
            "OpenViking session commit did not return a task_id",
            secrets,
        )
    resource_id = _find_first_string(
        commit_result,
        {"resource_id", "resourceId", "session_id", "sessionId"},
    ) or session_id
    archive_uri = _find_first_string(commit_result, {"archive_uri", "archiveUri"})
    operation = {
        "task_id": task_id,
        "resource_id": resource_id,
        "session_id": session_id,
        "archive_uri": archive_uri,
        "status_probe": "task",
        "type": "memory_flush",
    }
    _attach_scope(operation, inputs, environment, source_operation)
    return {
        "status": "ok",
        "state": "accepted",
        "operation": operation,
        "output": {
            "accepted": True,
            "task_id": task_id,
            "session_id": session_id,
            "archive_uri": archive_uri,
        },
        "metrics": [],
        "artifacts": [],
        "error": {},
    }


def _session_id(request: dict[str, Any], inputs: dict[str, Any]) -> str:
    explicit = str(inputs.get("session_id") or "").strip()
    if explicit:
        return explicit
    seed = str(request.get("idempotency_key") or request.get("task_id") or "").strip()
    if not seed:
        raise ValueError("memory.ingest requires idempotency_key or task_id")
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"mbp-{digest}"


def _post_json(
    url: str,
    body: dict[str, Any],
    environment: dict[str, str],
    urlopen: Callable[..., Any],
    timeout: float,
) -> dict[str, Any]:
    headers = _request_headers(environment)
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8") or "{}")
    result = payload.get("result", payload) if isinstance(payload, dict) else {}
    if not isinstance(result, dict):
        raise ValueError("OpenViking API returned an invalid result")
    return result


def _run_status(
    inputs: dict[str, Any],
    cli_environment: dict[str, str],
    environment: dict[str, str],
    command_runner: Callable[..., Any],
    urlopen: Callable[..., Any],
) -> dict[str, Any]:
    operation = inputs.get("operation", {})
    if not isinstance(operation, dict):
        operation = {}
    task_id = str(inputs.get("task_id") or operation.get("task_id") or "")
    if not task_id:
        command = [_ov_binary(cli_environment), "-o", "json", "wait", "--timeout", "1"]
        proc = command_runner(
            command,
            text=True,
            capture_output=True,
            check=False,
            env=cli_environment,
        )
        state = "completed" if proc.returncode == 0 else "running"
        return {
            "status": "ok",
            "state": state,
            "operation": {**operation, "status_probe": "wait"},
            "output": {"backend_status": state},
            "metrics": [],
            "artifacts": [],
            "error": {},
        }

    base_url = str(environment.get("OPENVIKING_API_URL") or "http://127.0.0.1:1933").rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{urllib.parse.quote(task_id, safe='')}",
        headers=_request_headers(environment),
        method="GET",
    )
    timeout = float(inputs.get("request_timeout_seconds", 10))
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8") or "{}")
    task = payload.get("result", payload) if isinstance(payload, dict) else {}
    if not isinstance(task, dict):
        task = {}
    backend_status = str(task.get("status", "") or "")
    state = _normalize_state(backend_status)
    resource_id = str(task.get("resource_id") or operation.get("resource_id") or "")
    status = "failed" if state == "failed" else "ok"
    error = {}
    if state == "failed":
        error = {
            "type": "backend_task_failed",
            "message": "OpenViking task failed",
        }
    return {
        "status": status,
        "state": state,
        "operation": {**operation, "task_id": task_id, "resource_id": resource_id, "status_probe": "task"},
        "output": {
            "task_id": task_id,
            "resource_id": resource_id,
            "backend_status": backend_status,
        },
        "metrics": [],
        "artifacts": [],
        "error": error,
    }


def _run_recall(
    inputs: dict[str, Any],
    cli_environment: dict[str, str],
    environment: dict[str, str],
    command_runner: Callable[..., Any],
    secrets: list[str],
) -> dict[str, Any]:
    query = inputs.get("query")
    if not isinstance(query, str) or not query:
        return _failed_result("invalid_input", "memory.recall requires inputs.query", secrets)
    command = [_ov_binary(cli_environment), "-o", "json", "find", query]
    node_limit = inputs.get("node_limit")
    if node_limit is not None:
        command.extend(["--node-limit", str(node_limit)])
    payload, error = _run_cli(command, cli_environment, command_runner, secrets)
    if error is not None:
        return error

    memories = _extract_memories(payload)
    evidence = [text for text in (_memory_text(item) for item in memories) if text]
    return {
        "status": "ok",
        "state": "completed",
        "operation": {"status_probe": "none"},
        "output": {
            "count": len(memories),
            "memories": memories,
            "evidence_text": "\n".join(dict.fromkeys(evidence)),
            "scope_id": str(inputs.get("scope_id") or ""),
            "target_identity": _target_identity(environment),
        },
        "metrics": [],
        "artifacts": [],
        "error": {},
    }


def _prepare_cli_environment(
    environment: dict[str, str],
) -> tuple[dict[str, str], Path | None, Path]:
    cli_environment = dict(environment)
    cli_environment.setdefault("OPENVIKING_LANG", "en")
    temporary_home = Path(tempfile.mkdtemp(prefix="openviking-runner-home-"))
    config_dir = temporary_home / ".openviking"
    config_dir.mkdir(mode=0o700)
    settings_path = config_dir / "ovcli.settings.conf"
    settings_path.write_text(json.dumps({"language": "en"}), encoding="utf-8")
    os.chmod(settings_path, 0o600)
    cli_environment["HOME"] = str(temporary_home)
    configured_path = str(environment.get("OPENVIKING_CLI_CONFIG_FILE", "") or "")
    api_key = str(
        environment.get("OPENVIKING_API_KEY")
        or environment.get("OPENVIKING_ROOT_API_KEY")
        or ""
    )
    config: dict[str, Any] = {}
    if configured_path:
        try:
            loaded = json.loads(Path(configured_path).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    config.update({
        "url": str(environment.get("OPENVIKING_API_URL") or "http://127.0.0.1:1933"),
        "api_key": api_key,
        "account": str(environment.get("OPENVIKING_ACCOUNT_ID", "")),
        "user": str(environment.get("OPENVIKING_USER_ID", "")),
        "agent_id": str(environment.get("OPENVIKING_AGENT_ID", "")),
        "output": "json",
        "echo_command": False,
    })
    root_key = str(environment.get("OPENVIKING_ROOT_API_KEY", "") or "")
    if root_key:
        config["root_api_key"] = root_key

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="openviking-cli-",
        suffix=".json",
        dir=config_dir,
        delete=False,
    )
    path = Path(handle.name)
    try:
        os.chmod(path, 0o600)
        json.dump(config, handle)
    finally:
        handle.close()
    cli_environment["OPENVIKING_CLI_CONFIG_FILE"] = str(path)
    return cli_environment, path, temporary_home


def _run_cli(
    command: list[str],
    environment: dict[str, str],
    command_runner: Callable[..., Any],
    secrets: list[str],
) -> tuple[Any, dict[str, Any] | None]:
    proc = command_runner(
        command,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    if proc.returncode != 0:
        return None, _failed_result("command_failed", proc.stderr or "OpenViking command failed", secrets)
    try:
        return json.loads(proc.stdout or "{}"), None
    except json.JSONDecodeError:
        return None, _failed_result("invalid_json", "OpenViking CLI returned invalid JSON", secrets)


def _failed_result(error_type: str, message: str, secrets: list[str]) -> dict[str, Any]:
    return {
        "status": "failed",
        "state": "failed",
        "operation": {},
        "output": {},
        "metrics": [],
        "artifacts": [],
        "error": {
            "type": error_type,
            "message": _sanitize(message, secrets),
        },
    }


def _request_headers(environment: dict[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    api_key = str(
        environment.get("OPENVIKING_API_KEY")
        or environment.get("OPENVIKING_ROOT_API_KEY")
        or ""
    )
    if api_key:
        headers["X-API-Key"] = api_key
    for env_name, header_name in (
        ("OPENVIKING_ACCOUNT_ID", "X-OpenViking-Account"),
        ("OPENVIKING_USER_ID", "X-OpenViking-User"),
        ("OPENVIKING_AGENT_ID", "X-OpenViking-Agent"),
    ):
        value = str(environment.get(env_name, "") or "")
        if value:
            headers[header_name] = value
    return headers


def _scoped_environment(environment: dict[str, str], inputs: dict[str, Any]) -> dict[str, str]:
    operation = inputs.get("operation", {})
    if not isinstance(operation, dict):
        operation = {}
    scope_id = str(inputs.get("scope_id") or operation.get("scope_id") or "").strip()
    if not scope_id:
        return environment
    scoped = dict(environment)
    base_agent = str(environment.get("OPENVIKING_AGENT_ID") or "memory-bench")
    digest = hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:16]
    scoped["OPENVIKING_AGENT_ID"] = f"{base_agent[:40]}--episode-{digest}"
    return scoped


def _target_identity(environment: dict[str, str]) -> dict[str, str]:
    return {
        "account_id": str(environment.get("OPENVIKING_ACCOUNT_ID", "")),
        "user_id": str(environment.get("OPENVIKING_USER_ID", "")),
        "agent_id": str(environment.get("OPENVIKING_AGENT_ID", "")),
    }


def _attach_scope(
    operation: dict[str, Any],
    inputs: dict[str, Any],
    environment: dict[str, str],
    source_operation: dict[str, Any] | None = None,
) -> None:
    scope_id = str(
        inputs.get("scope_id")
        or (source_operation or {}).get("scope_id")
        or ""
    ).strip()
    if scope_id:
        operation["scope_id"] = scope_id
        operation["target_identity"] = _target_identity(environment)


def _normalize_state(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"completed", "complete", "succeeded", "success", "done"}:
        return "completed"
    if normalized in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if normalized == "accepted":
        return "accepted"
    return "running"


def _extract_memories(payload: Any) -> list[Any]:
    value = payload.get("result", payload) if isinstance(payload, dict) else payload
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    memories: list[Any] = []
    collection_found = False
    for key in ("memories", "resources", "skills", "results", "items"):
        items = value.get(key)
        if isinstance(items, list):
            collection_found = True
            memories.extend(items)
    return memories if collection_found else [value]


def _memory_text(memory: Any) -> str:
    if isinstance(memory, str):
        return memory.strip()
    if not isinstance(memory, dict):
        return ""
    for key in ("content", "text", "abstract", "overview", "summary"):
        value = memory.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _find_first_string(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item is not None:
                return str(item)
        for item in value.values():
            found = _find_first_string(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_string(item, keys)
            if found:
                return found
    return ""


def _ov_binary(environment: dict[str, str]) -> str:
    return str(environment.get("OPENVIKING_BIN") or "ov")


def _secret_values(environment: dict[str, str], inputs: dict[str, Any]) -> list[str]:
    values = [
        str(environment.get("OPENVIKING_API_KEY", "") or ""),
        str(environment.get("OPENVIKING_ROOT_API_KEY", "") or ""),
    ]
    content = inputs.get("content")
    if isinstance(content, str):
        values.append(content)
    return [value for value in values if value]


def _sanitize(message: str, secrets: list[str]) -> str:
    sanitized = message
    for secret in secrets:
        sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized[:1000]


def main() -> None:
    request = json.load(sys.stdin)
    print(json.dumps(run_operation(request), ensure_ascii=False))


if __name__ == "__main__":
    main()
