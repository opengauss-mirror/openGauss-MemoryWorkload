from __future__ import annotations

import json
from typing import Any, Callable
import urllib.request

from .protocol import (
    MemoryPluginTaskInput,
    MemoryTaskInput,
    RenderedTaskInput,
    StepRecord,
    WorkflowRuntimeContext,
)


def dispatch_step_operator(
    step: StepRecord,
    *,
    agent_id: str,
    memory_id: str | None,
    memory_plugin_id: str | None,
    runtime_context: WorkflowRuntimeContext,
    agent_runner: Callable[..., dict[str, Any]],
    memory_runner: Callable[..., Any],
    memory_plugin_runner: Callable[..., Any],
    subprocess_runner: Callable[..., Any],
    urlopen: Callable[..., Any],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    poll_trace: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    if step.operator_kind == "agent":
        return _execute_agent(step, agent_id, agent_runner, runtime_context, memory_plugin_runner)
    if step.operator_kind == "wait":
        return _execute_wait(step, sleep)
    if step.operator_kind == "bash":
        return _execute_bash(step, subprocess_runner)
    if step.operator_kind == "http":
        return _execute_http(step, urlopen)
    if step.operator_kind == "memory":
        return _execute_memory(step, memory_id, runtime_context, memory_runner)
    if step.operator_kind == "memory_plugin":
        return _execute_memory_plugin(
            step,
            memory_plugin_id,
            runtime_context,
            memory_plugin_runner,
        )
    if step.operator_kind == "poll":
        return _execute_poll(
            step,
            memory_id=memory_id,
            runtime_context=runtime_context,
            memory_runner=memory_runner,
            urlopen=urlopen,
            sleep=sleep,
            monotonic=monotonic,
            poll_trace=poll_trace,
        )
    raise ValueError(f"unsupported operator_kind: {step.operator_kind}")


def _execute_agent(
    step: StepRecord,
    agent_id: str,
    agent_runner: Callable[..., dict[str, Any]],
    runtime_context: WorkflowRuntimeContext,
    memory_plugin_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    messages = step.inputs.get("messages")
    if not isinstance(messages, list) or not messages:
        messages = [{"role": "user", "content": str(step.inputs.get("question", ""))}]
    metadata = dict(step.inputs.get("metadata", {}))
    # Runtime binding is authoritative; task metadata must not select a plugin.
    metadata["memory_integration"] = runtime_context.memory_integration
    metadata["memory_plugin_id"] = runtime_context.memory_plugin_id
    metadata.setdefault("case_id", step.case_id)
    metadata.setdefault("step_id", step.step_id)
    rendered = RenderedTaskInput(
        task_id=step.step_id,
        system_prompt=str(step.inputs.get("system_prompt")) if step.inputs.get("system_prompt") else None,
        messages=messages,
        attachments=[str(item) for item in step.inputs.get("attachments", [])]
        if isinstance(step.inputs.get("attachments", []), list)
        else [],
        metadata=metadata,
    )
    # Optional checks belong to the bound plugin, not to an Agent implementation.
    plugin_id = runtime_context.memory_plugin_id
    actions = (runtime_context.run_contract.get("memory_plugin_runtime") or {}).get("actions", [])
    checks = {}
    def check(action, **inputs):
        if runtime_context.memory_integration != "agent_plugin" or not plugin_id or action not in actions:
            return {}
        if memory_plugin_runner is None:
            raise ValueError("declared Agent checks require a memory plugin runner")
        response = memory_plugin_runner(plugin_id, MemoryPluginTaskInput(
            task_id=f"{step.step_id}:{action}", action=action,
            inputs={"agent_request": rendered.model_dump(mode="json"), **inputs},
            runtime_context=runtime_context,
            idempotency_key=f"{runtime_context.run_id}:{step.step_id}:{action}",
        ))
        checks[action] = response.model_dump(mode="json")
        return response.output

    def failed_check(action, result=None):
        response = checks.get(action, {})
        if not response or (response["status"] == "ok" and response["state"] == "completed"):
            return None
        return {**(result or {}), "status": "failed", "exit_code": 1,
                "error_message": f"memory plugin {action} failed: " +
                    str(response.get("error", {}).get("message") or response["state"]),
                "plugin_checks": checks}

    check_context = check("before_agent")
    failure = failed_check("before_agent")
    if failure is not None:
        return failure
    result = agent_runner(agent_id, rendered)
    if not isinstance(result, dict):
        raise ValueError("agent runner returned a non-object response")
    if str(result.get("status") or "") not in {"ok", "failed"}:
        raise ValueError("agent runner response requires status=ok|failed")
    turns = result.get("turns", [])
    answer = str(result.get("agent_answer") or result.get("text_output") or "").strip()
    if not answer and isinstance(turns, list):
        for turn in reversed(turns):
            if isinstance(turn, dict) and turn.get("text"):
                answer = str(turn["text"]).strip()
                break
    if result["status"] == "ok" and not answer:
        raise ValueError("successful agent runner response requires a final answer text")
    normalized = dict(result)
    normalized["agent_answer"] = answer
    check("after_agent", check_context=check_context, agent_result=result)
    failure = failed_check("after_agent", normalized)
    if failure is not None:
        return failure
    if checks:
        normalized["plugin_checks"] = checks
    return normalized


def _execute_wait(step: StepRecord, sleep: Callable[[float], None]) -> dict[str, Any]:
    seconds = float(step.inputs.get("seconds", 0))
    sleep(seconds)
    return {
        "status": "ok",
        "turns": [{"text": f"waited {seconds} seconds"}],
        "metrics": [{"name": "wait_seconds", "value": seconds, "unit": "s"}],
    }


def _execute_bash(step: StepRecord, subprocess_runner: Callable[..., Any]) -> dict[str, Any]:
    cmd = step.inputs.get("cmd", [])
    if not isinstance(cmd, list) or not cmd:
        raise ValueError("bash operator requires non-empty cmd list")
    env = step.inputs.get("env")
    cwd = step.inputs.get("cwd")
    proc = subprocess_runner(
        [str(item) for item in cmd],
        text=True,
        capture_output=True,
        check=False,
        env=None if not isinstance(env, dict) else {str(k): str(v) for k, v in env.items()},
        cwd=None if cwd is None else str(cwd),
        timeout=step.timeout_seconds,
    )
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "turns": [{"text": proc.stdout.strip()}] if proc.stdout.strip() else [],
        "metrics": [],
    }


def _execute_http(step: StepRecord, urlopen: Callable[..., Any]) -> dict[str, Any]:
    method = str(step.inputs.get("method", "GET")).upper()
    url = str(step.inputs.get("url", ""))
    if not url:
        raise ValueError("http operator requires url")
    headers = step.inputs.get("headers", {})
    body = step.inputs.get("body")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
        method=method,
    )
    with urlopen(request, timeout=step.timeout_seconds) as response:
        text = response.read().decode("utf-8")
        return {
            "status": "ok" if 200 <= response.status < 300 else "failed",
            "exit_code": 0 if 200 <= response.status < 300 else 1,
            "http_status": response.status,
            "stdout": text,
            "stderr": "",
            "turns": [{"text": text.strip()}] if text.strip() else [],
            "metrics": [],
        }


def _execute_memory(
    step: StepRecord,
    memory_id: str | None,
    runtime_context: WorkflowRuntimeContext,
    memory_runner: Callable[..., Any],
) -> dict[str, Any]:
    if not memory_id:
        raise ValueError(f"memory step {step.step_id} requires memory_id")
    action = str(step.inputs.get("action", "") or "")
    inputs = {key: value for key, value in step.inputs.items() if key != "action"}
    request = MemoryTaskInput(
        task_id=step.step_id,
        action=action,
        inputs=inputs,
        runtime_context=runtime_context,
        idempotency_key=f"{runtime_context.run_id}:{step.case_id}:{step.step_id}",
        checkpoint_id=str(step.inputs.get("checkpoint_id") or "") or None,
        scope_id=str(step.inputs.get("scope_id") or "") or None,
    )
    response = memory_runner(memory_id, request)
    payload = response.model_dump(mode="json")
    unified_output = dict(payload.get("output", {}))
    unified_output["state"] = payload["state"]
    unified_output["operation"] = payload.get("operation", {})
    payload["output"] = unified_output
    if payload["status"] != "ok":
        error = payload.get("error", {})
        payload["error_message"] = str(error.get("message") or "memory operator failed")
    return payload


def _execute_memory_plugin(
    step: StepRecord,
    memory_plugin_id: str | None,
    runtime_context: WorkflowRuntimeContext,
    memory_plugin_runner: Callable[..., Any],
) -> dict[str, Any]:
    if not memory_plugin_id:
        raise ValueError(f"memory plugin step {step.step_id} requires memory_plugin_id")
    action = str(step.inputs.get("action", "") or "")
    inputs = {key: value for key, value in step.inputs.items() if key != "action"}
    request = MemoryPluginTaskInput(
        task_id=step.step_id,
        action=action,
        inputs=inputs,
        runtime_context=runtime_context,
        idempotency_key=f"{runtime_context.run_id}:{step.case_id}:{step.step_id}",
        checkpoint_id=str(step.inputs.get("checkpoint_id") or "") or None,
        scope_id=str(step.inputs.get("scope_id") or "") or None,
    )
    response = memory_plugin_runner(memory_plugin_id, request)
    payload = response.model_dump(mode="json")
    unified_output = dict(payload.get("output", {}))
    unified_output["state"] = payload["state"]
    unified_output["operation"] = payload.get("operation", {}) or unified_output.get("operation", {})
    payload["output"] = unified_output
    if payload["status"] != "ok":
        error = payload.get("error", {})
        payload["error_message"] = str(error.get("message") or "memory plugin operator failed")
    return payload


def _execute_poll(
    step: StepRecord,
    *,
    memory_id: str | None,
    runtime_context: WorkflowRuntimeContext,
    memory_runner: Callable[..., Any],
    urlopen: Callable[..., Any],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    poll_trace: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    if step.timeout_seconds is None:
        raise ValueError(f"poll step {step.step_id} requires timeout_seconds")
    probe = step.inputs.get("probe")
    if not isinstance(probe, dict):
        raise ValueError(f"poll step {step.step_id} requires a probe object")
    interval = max(0.0, float(step.inputs.get("interval_seconds", 1)))
    success_when = step.inputs.get("success_when")
    failure_when = step.inputs.get("failure_when")
    started = monotonic()
    poll_count = 0
    last_probe: dict[str, Any] = {}
    probe_metrics: list[dict[str, Any]] = []
    probe_artifacts: list[dict[str, Any]] = []

    while True:
        poll_count += 1
        last_probe = _execute_probe(
            step,
            probe,
            memory_id=memory_id,
            runtime_context=runtime_context,
            memory_runner=memory_runner,
            urlopen=urlopen,
        )
        elapsed_ms = int((monotonic() - started) * 1000)
        current_metrics = last_probe.get("metrics", [])
        if isinstance(current_metrics, list):
            for metric in current_metrics:
                if isinstance(metric, dict):
                    item = dict(metric)
                    dimension = item.get("dimension", {})
                    item["dimension"] = {
                        **(dimension if isinstance(dimension, dict) else {}),
                        "poll_probe": str(poll_count),
                    }
                    probe_metrics.append(item)
        current_artifacts = last_probe.get("artifacts", [])
        if isinstance(current_artifacts, list):
            probe_artifacts.extend(item for item in current_artifacts if isinstance(item, dict))
        poll_trace(
            {
                "poll_count": poll_count,
                "elapsed_ms": elapsed_ms,
                "status": last_probe.get("status"),
                "state": last_probe.get("state"),
            }
        )

        output = {
            "poll_count": poll_count,
            "elapsed_ms": elapsed_ms,
            "last_probe": last_probe,
        }
        metrics = [
            {"name": "poll_count", "value": poll_count, "unit": "count"},
            {"name": "poll_elapsed_ms", "value": elapsed_ms, "unit": "ms"},
            *probe_metrics,
        ]
        if _condition_matches(failure_when, last_probe):
            return {
                "status": "failed",
                "exit_code": 1,
                "error_message": "poll failure condition matched",
                "output": output,
                "metrics": metrics,
                "artifacts": probe_artifacts,
            }
        if _condition_matches(success_when, last_probe):
            return {
                "status": "ok",
                "exit_code": 0,
                "output": output,
                "metrics": metrics,
                "artifacts": probe_artifacts,
            }
        if monotonic() - started >= step.timeout_seconds:
            return {
                "status": "failed",
                "exit_code": 1,
                "error_message": f"poll timed out after {step.timeout_seconds} seconds",
                "output": output,
                "metrics": metrics,
                "artifacts": probe_artifacts,
            }
        sleep(interval)


def _execute_probe(
    poll_step: StepRecord,
    probe: dict[str, Any],
    *,
    memory_id: str | None,
    runtime_context: WorkflowRuntimeContext,
    memory_runner: Callable[..., Any],
    urlopen: Callable[..., Any],
) -> dict[str, Any]:
    operator_kind = str(probe.get("operator_kind", "") or "")
    probe_inputs = dict(probe.get("inputs", {}))
    if operator_kind == "memory":
        probe_inputs["action"] = str(probe.get("action") or probe_inputs.get("action") or "")
        probe_step = StepRecord(
            step_id=poll_step.step_id,
            case_id=poll_step.case_id,
            name=f"{poll_step.name} probe",
            operator_kind="memory",
            timeout_seconds=poll_step.timeout_seconds,
            inputs=probe_inputs,
        )
        return _execute_memory(probe_step, memory_id, runtime_context, memory_runner)
    if operator_kind == "http":
        for key in ("method", "url", "headers", "body"):
            if key in probe and key not in probe_inputs:
                probe_inputs[key] = probe[key]
        probe_step = StepRecord(
            step_id=poll_step.step_id,
            case_id=poll_step.case_id,
            name=f"{poll_step.name} probe",
            operator_kind="http",
            timeout_seconds=poll_step.timeout_seconds,
            inputs=probe_inputs,
        )
        return _execute_http(probe_step, urlopen)
    raise ValueError(f"poll probe operator is not allowed: {operator_kind or '<missing>'}")


def _condition_matches(condition: Any, payload: dict[str, Any]) -> bool:
    if condition is None:
        return False
    if not isinstance(condition, dict):
        raise ValueError("poll condition must be an object")
    path = str(condition.get("path", "") or "")
    if not path:
        raise ValueError("poll condition requires path")
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    if "equals" in condition:
        return value == condition["equals"]
    if "in" in condition and isinstance(condition["in"], list):
        return value in condition["in"]
    raise ValueError("poll condition requires equals or in")
