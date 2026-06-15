from __future__ import annotations

import json
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from .integration import run_agent_task
from .judges import run_builtin_judge
from .protocol import (
    ArtifactRecord,
    CaseRecord,
    ExecutionSpec,
    JudgeInput,
    JudgeResult,
    MetricRecord,
    RenderedTaskInput,
    StepRecord,
    StepResultRecord,
    TraceEventRecord,
)


def _timestamp() -> datetime:
    return datetime.now()


def _extract_text_answer(structured_output: dict[str, object]) -> str:
    for key in ("agent_answer", "text_output", "stdout_text"):
        value = structured_output.get(key)
        if value:
            return str(value)
    return ""


def _execute_step_operator(step: StepRecord, agent_id: str) -> dict:
    if step.operator_kind == "agent":
        messages = step.inputs.get("messages")
        if not isinstance(messages, list) or not messages:
            messages = [{"role": "user", "content": str(step.inputs.get("question", ""))}]
        rendered = RenderedTaskInput(
            task_id=step.step_id,
            system_prompt=str(step.inputs.get("system_prompt")) if step.inputs.get("system_prompt") else None,
            messages=messages,
            attachments=[str(item) for item in step.inputs.get("attachments", [])]
            if isinstance(step.inputs.get("attachments", []), list)
            else [],
            metadata=step.inputs.get("metadata", {}),
        )
        return run_agent_task(agent_id, rendered)

    if step.operator_kind == "wait":
        seconds = float(step.inputs.get("seconds", 0))
        time.sleep(seconds)
        return {
            "status": "ok",
            "turns": [{"text": f"waited {seconds} seconds"}],
            "metrics": [{"name": "wait_seconds", "value": seconds}],
        }

    if step.operator_kind == "bash":
        cmd = step.inputs.get("cmd", [])
        if not isinstance(cmd, list) or not cmd:
            raise ValueError("bash operator requires non-empty cmd list")
        env = step.inputs.get("env")
        cwd = step.inputs.get("cwd")
        proc = subprocess.run(
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

    if step.operator_kind == "http":
        method = str(step.inputs.get("method", "GET")).upper()
        url = str(step.inputs.get("url", ""))
        if not url:
            raise ValueError("http operator requires url")
        headers = step.inputs.get("headers", {})
        body = step.inputs.get("body")
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=step.timeout_seconds) as response:
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

    raise ValueError(f"unsupported operator_kind: {step.operator_kind}")


def execute_cases(
    *,
    run_id: str,
    agent_id: str,
    cases: list[CaseRecord],
    steps: list[StepRecord],
    execution_spec: ExecutionSpec,
    run_dir: Path,
) -> dict[str, list]:
    steps_by_case: dict[str, list[StepRecord]] = {}
    for step in steps:
        steps_by_case.setdefault(step.case_id, []).append(step)

    step_results: list[StepResultRecord] = []
    traces: list[TraceEventRecord] = []
    judge_results: list[JudgeResult] = []
    metrics: list[MetricRecord] = []
    artifacts: list[ArtifactRecord] = []

    stdout_dir = run_dir / "artifacts" / "step-stdout"
    stderr_dir = run_dir / "artifacts" / "step-stderr"
    stdout_dir.mkdir(parents=True, exist_ok=True)
    stderr_dir.mkdir(parents=True, exist_ok=True)

    for case in cases:
        case_step_results: list[StepResultRecord] = []
        results_by_step: dict[str, StepResultRecord] = {}
        case_failed = False
        for step in steps_by_case.get(case.case_id, []):
            if step.depends_on and any(
                dep not in results_by_step or results_by_step[dep].status != "passed" for dep in step.depends_on
            ):
                skipped = StepResultRecord(
                    step_result_id=f"{step.step_id}-skipped",
                    step_id=step.step_id,
                    attempt=0,
                    status="skipped",
                    gate_passed=False,
                    gate_detail="dependency not satisfied",
                )
                case_step_results.append(skipped)
                step_results.append(skipped)
                results_by_step[step.step_id] = skipped
                continue

            traces.append(
                TraceEventRecord(
                    trace_id=f"{case.case_id}-{step.step_id}-started",
                    case_id=case.case_id,
                    step_id=step.step_id,
                    event_type="step_started",
                    timestamp=_timestamp(),
                )
            )
            final_result: StepResultRecord | None = None
            max_attempts = max(1, step.retry_limit + 1 if step.retry_limit else execution_spec.default_retry_limit + 1)
            for attempt in range(1, max_attempts + 1):
                started_at = _timestamp()
                stderr_path = stderr_dir / f"{step.step_id}-attempt-{attempt}.txt"
                stdout_path = stdout_dir / f"{step.step_id}.json"
                try:
                    agent_output = _execute_step_operator(step, agent_id)
                    ended_at = _timestamp()

                    stdout_path.write_text(json.dumps(agent_output, ensure_ascii=False, indent=2), encoding="utf-8")

                    turns = agent_output.get("turns", [])
                    text_output = str(turns[0].get("text", "")) if turns else str(agent_output.get("stdout", "")).strip()

                    final_result = StepResultRecord(
                        step_result_id=f"{step.step_id}-attempt-{attempt}",
                        step_id=step.step_id,
                        attempt=attempt,
                        status="passed" if agent_output.get("status") == "ok" else "failed",
                        exit_code=int(agent_output.get("exit_code", 0 if agent_output.get("status") == "ok" else 1)),
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_ms=int((ended_at - started_at).total_seconds() * 1000),
                        stdout_ref=str(stdout_path.relative_to(run_dir)),
                        stderr_ref=None,
                        structured_output={
                            "agent_status": agent_output.get("status"),
                            "agent_answer": text_output if step.operator_kind == "agent" else "",
                            "text_output": text_output,
                            "stdout_text": str(agent_output.get("stdout", "")).strip(),
                            "raw": agent_output,
                        },
                        gate_passed=agent_output.get("status") == "ok",
                        gate_detail="operator returned ok" if agent_output.get("status") == "ok" else "operator failed",
                    )
                except (
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                    FileNotFoundError,
                    ValueError,
                    urllib.error.URLError,
                    urllib.error.HTTPError,
                ) as exc:
                    ended_at = _timestamp()
                    stderr_path.write_text(str(exc), encoding="utf-8")
                    final_result = StepResultRecord(
                        step_result_id=f"{step.step_id}-attempt-{attempt}",
                        step_id=step.step_id,
                        attempt=attempt,
                        status="failed",
                        exit_code=1,
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_ms=int((ended_at - started_at).total_seconds() * 1000),
                        stdout_ref=None,
                        stderr_ref=str(stderr_path.relative_to(run_dir)),
                        structured_output={},
                        gate_passed=False,
                        gate_detail=str(exc),
                    )

                if final_result is None:
                    raise RuntimeError(f"step result missing for {step.step_id}")

                if final_result.status == "passed":
                    break

                if attempt < max_attempts:
                    traces.append(
                        TraceEventRecord(
                            trace_id=f"{case.case_id}-{step.step_id}-retry-{attempt}",
                            case_id=case.case_id,
                            step_id=step.step_id,
                            event_type="retry_scheduled",
                            timestamp=_timestamp(),
                            payload={"attempt": attempt + 1},
                        )
                    )

            result = final_result
            case_step_results.append(result)
            step_results.append(result)
            results_by_step[step.step_id] = result

            traces.append(
                TraceEventRecord(
                    trace_id=f"{case.case_id}-{step.step_id}-finished",
                    case_id=case.case_id,
                    step_id=step.step_id,
                    event_type="step_finished",
                    timestamp=_timestamp(),
                    payload={"status": result.status},
                )
            )

            traces.append(
                TraceEventRecord(
                    trace_id=f"{case.case_id}-{step.step_id}-gate",
                    case_id=case.case_id,
                    step_id=step.step_id,
                    event_type="gate_passed" if result.gate_passed else "gate_failed",
                    timestamp=_timestamp(),
                    payload={"detail": result.gate_detail},
                )
            )

            if result.stdout_ref:
                artifacts.append(
                    ArtifactRecord(
                        artifact_id=f"{step.step_id}-stdout",
                        run_id=run_id,
                        case_id=case.case_id,
                        step_id=step.step_id,
                        kind="step_stdout",
                        path=result.stdout_ref,
                        content_type="application/json",
                    )
                )
            if result.stderr_ref:
                artifacts.append(
                    ArtifactRecord(
                        artifact_id=f"{step.step_id}-stderr",
                        run_id=run_id,
                        case_id=case.case_id,
                        step_id=step.step_id,
                        kind="step_stderr",
                        path=result.stderr_ref,
                        content_type="text/plain",
                    )
                )
            metrics.append(
                MetricRecord(
                    metric_id=f"{step.step_id}-duration",
                    run_id=run_id,
                    case_id=case.case_id,
                    step_id=step.step_id,
                    scope="step",
                    name="duration_ms",
                    value=result.duration_ms or 0,
                    unit="ms",
                )
            )

            metrics.append(
                MetricRecord(
                    metric_id=f"{step.step_id}-retry-count",
                    run_id=run_id,
                    case_id=case.case_id,
                    step_id=step.step_id,
                    scope="step",
                    name="retry_count",
                    value=max(0, result.attempt - 1),
                    unit="count",
                )
            )

            if not result.gate_passed and step.gate_policy == "hard":
                case_failed = True
                if execution_spec.fail_fast:
                    break

        traces.append(
            TraceEventRecord(
                trace_id=f"{case.case_id}-judge-started",
                case_id=case.case_id,
                event_type="case_judge_started",
                timestamp=_timestamp(),
            )
        )
        judge_input = JudgeInput(
            case_id=case.case_id,
            goal=case.goal,
            reference=case.reference,
            step_results=[item.model_dump(mode="json") for item in case_step_results],
            trace_events=[item.model_dump(mode="json") for item in traces if item.case_id == case.case_id],
            artifacts=[item.model_dump(mode="json") for item in artifacts if item.case_id == case.case_id],
        )
        judge_result = run_builtin_judge(run_id, judge_input)
        if case_failed and judge_result.passed:
            judge_result.passed = False
            judge_result.label = "gate-failed"
            judge_result.score = 0.0
            judge_result.rationale = "One or more hard gates failed before final judge."
        judge_results.append(judge_result)
        traces.append(
            TraceEventRecord(
                trace_id=f"{case.case_id}-judge-finished",
                case_id=case.case_id,
                event_type="case_judge_finished",
                timestamp=_timestamp(),
                payload={"passed": judge_result.passed, "label": judge_result.label},
            )
        )

    return {
        "step_results": step_results,
        "traces": traces,
        "judge_results": judge_results,
        "metrics": metrics,
        "artifacts": artifacts,
    }
