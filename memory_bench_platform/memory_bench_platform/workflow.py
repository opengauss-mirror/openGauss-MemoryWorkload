from __future__ import annotations

import json
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from .integration import run_agent_task, run_memory_plugin_task, run_memory_task
from .judges import run_builtin_judge
from .protocol import (
    ArtifactRecord,
    CaseRecord,
    ExecutionSpec,
    JudgeInput,
    JudgeResult,
    MetricRecord,
    StepRecord,
    StepResultRecord,
    TraceEventRecord,
    WorkflowRuntimeContext,
)
from .workflow_inputs import InputResolutionError, build_input_scope, resolve_inputs, validate_workflow
from .workflow_operators import dispatch_step_operator


_sleep = time.sleep
_monotonic = time.monotonic


def _timestamp() -> datetime:
    return datetime.now()


def _extract_text_answer(structured_output: dict[str, object]) -> str:
    for key in ("agent_answer", "text_output", "stdout_text"):
        value = structured_output.get(key)
        if value:
            return str(value)
    return ""


def _execute_step_operator(
    step: StepRecord,
    *,
    agent_id: str,
    memory_id: str | None,
    runtime_context: WorkflowRuntimeContext,
    poll_trace,
) -> dict:
    return dispatch_step_operator(
        step,
        agent_id=agent_id,
        memory_id=memory_id,
        memory_plugin_id=runtime_context.memory_plugin_id,
        runtime_context=runtime_context,
        agent_runner=run_agent_task,
        memory_runner=run_memory_task,
        memory_plugin_runner=run_memory_plugin_task,
        subprocess_runner=subprocess.run,
        urlopen=urllib.request.urlopen,
        sleep=_sleep,
        monotonic=_monotonic,
        poll_trace=poll_trace,
    )


def execute_cases(
    *,
    run_id: str,
    agent_id: str,
    memory_id: str | None = None,
    runtime_context: WorkflowRuntimeContext | None = None,
    cases: list[CaseRecord],
    steps: list[StepRecord],
    execution_spec: ExecutionSpec,
    run_dir: Path,
) -> dict[str, list]:
    if runtime_context is None:
        runtime_context = WorkflowRuntimeContext(
            run_id=run_id,
            run_dir=str(run_dir),
            benchmark_id="",
            agent_id=agent_id,
            memory_id=memory_id,
        )
    validate_workflow(
        cases=cases,
        steps=steps,
        execution_spec=execution_spec,
        memory_id=memory_id,
        memory_plugin_id=runtime_context.memory_plugin_id,
    )

    steps_by_case: dict[str, list[StepRecord]] = {}
    for step in steps:
        steps_by_case.setdefault(step.case_id, []).append(step)

    step_results: list[StepResultRecord] = []
    traces: list[TraceEventRecord] = []
    judge_results: list[JudgeResult] = []
    metrics: list[MetricRecord] = []
    artifacts: list[ArtifactRecord] = []
    case_execution_status: dict[str, str] = {}

    stdout_dir = run_dir / "artifacts" / "step-stdout"
    stderr_dir = run_dir / "artifacts" / "step-stderr"
    stdout_dir.mkdir(parents=True, exist_ok=True)
    stderr_dir.mkdir(parents=True, exist_ok=True)

    for case in cases:
        case_step_results: list[StepResultRecord] = []
        results_by_step: dict[str, StepResultRecord] = {}
        unsatisfied_case_dependencies = [
            dependency
            for dependency in case.depends_on_cases
            if case_execution_status.get(dependency) != "passed"
        ]
        case_failed = bool(unsatisfied_case_dependencies)
        if unsatisfied_case_dependencies:
            dependency_detail = (
                "case dependency not satisfied: "
                + ", ".join(unsatisfied_case_dependencies)
            )
            for step in steps_by_case.get(case.case_id, []):
                skipped = StepResultRecord(
                    step_result_id=f"{step.step_id}-skipped",
                    step_id=step.step_id,
                    attempt=0,
                    status="skipped",
                    gate_passed=False,
                    gate_detail=dependency_detail,
                )
                case_step_results.append(skipped)
                step_results.append(skipped)
                results_by_step[step.step_id] = skipped

        executable_steps = [] if unsatisfied_case_dependencies else steps_by_case.get(case.case_id, [])
        for step in executable_steps:
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
            final_operator_output: dict = {}
            resolution_error: InputResolutionError | None = None
            try:
                resolved_inputs = resolve_inputs(
                    step.inputs,
                    build_input_scope(runtime_context, case, case_step_results),
                )
            except InputResolutionError as exc:
                resolution_error = exc
                resolved_step = step
            else:
                effective_timeout = (
                    step.timeout_seconds
                    if step.timeout_seconds is not None
                    else execution_spec.default_timeout_seconds
                )
                resolved_step = step.model_copy(
                    update={
                        "inputs": resolved_inputs,
                        "timeout_seconds": effective_timeout,
                    }
                )

            is_memory_ingest = (
                resolved_step.operator_kind == "memory"
                and str(resolved_step.inputs.get("action", "") or "") == "ingest"
            )
            max_attempts = (
                1
                if resolution_error is not None or is_memory_ingest
                else max(
                    1,
                    step.retry_limit + 1
                    if step.retry_limit
                    else execution_spec.default_retry_limit + 1,
                )
            )
            for attempt in range(1, max_attempts + 1):
                started_at = _timestamp()
                stderr_path = stderr_dir / f"{step.step_id}-attempt-{attempt}.txt"
                stdout_path = stdout_dir / f"{step.step_id}.json"
                try:
                    if resolution_error is not None:
                        raise resolution_error

                    def record_poll_trace(payload: dict) -> None:
                        traces.append(
                            TraceEventRecord(
                                trace_id=(
                                    f"{case.case_id}-{step.step_id}-poll-"
                                    f"{attempt}-{payload.get('poll_count', 0)}"
                                ),
                                case_id=case.case_id,
                                step_id=step.step_id,
                                event_type="poll_probe",
                                timestamp=_timestamp(),
                                payload=payload,
                            )
                        )

                    operator_output = _execute_step_operator(
                        resolved_step,
                        agent_id=agent_id,
                        memory_id=memory_id,
                        runtime_context=runtime_context,
                        poll_trace=record_poll_trace,
                    )
                    final_operator_output = operator_output
                    ended_at = _timestamp()

                    stdout_path.write_text(
                        json.dumps(operator_output, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )

                    turns = operator_output.get("turns", [])
                    text_output = (
                        str(turns[0].get("text", ""))
                        if turns
                        else str(operator_output.get("stdout", "")).strip()
                    )
                    raw_unified_output = operator_output.get("output")
                    if isinstance(raw_unified_output, dict):
                        unified_output = dict(raw_unified_output)
                    elif raw_unified_output is not None:
                        unified_output = {"value": raw_unified_output}
                    elif text_output:
                        unified_output = {"text": text_output}
                    else:
                        unified_output = {}
                    if step.operator_kind == "agent" and text_output:
                        unified_output.setdefault("text", text_output)

                    operator_ok = operator_output.get("status") == "ok"
                    gate_detail = (
                        "operator returned ok"
                        if operator_ok
                        else str(
                            operator_output.get("error_message")
                            or operator_output.get("stderr")
                            or "operator failed"
                        )
                    )

                    final_result = StepResultRecord(
                        step_result_id=f"{step.step_id}-attempt-{attempt}",
                        step_id=step.step_id,
                        attempt=attempt,
                        status="passed" if operator_ok else "failed",
                        exit_code=int(operator_output.get("exit_code", 0 if operator_ok else 1)),
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_ms=int((ended_at - started_at).total_seconds() * 1000),
                        stdout_ref=str(stdout_path.relative_to(run_dir)),
                        stderr_ref=None,
                        structured_output={
                            "output": unified_output,
                            "agent_status": operator_output.get("status"),
                            "agent_answer": text_output if step.operator_kind == "agent" else "",
                            "text_output": text_output,
                            "stdout_text": str(operator_output.get("stdout", "")).strip(),
                            "raw": operator_output,
                        },
                        gate_passed=operator_ok,
                        gate_detail=gate_detail,
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
            operator_artifacts = final_operator_output.get("artifacts", [])
            if isinstance(operator_artifacts, list):
                for index, item in enumerate(operator_artifacts, start=1):
                    if not isinstance(item, dict) or not item.get("path"):
                        continue
                    artifacts.append(
                        ArtifactRecord(
                            artifact_id=str(item.get("artifact_id") or f"{step.step_id}-operator-{index}"),
                            run_id=run_id,
                            case_id=case.case_id,
                            step_id=step.step_id,
                            kind=str(item.get("kind") or "operator_artifact"),
                            path=str(item["path"]),
                            content_type=(
                                str(item["content_type"])
                                if item.get("content_type") is not None
                                else None
                            ),
                            size_bytes=(
                                int(item["size_bytes"])
                                if item.get("size_bytes") is not None
                                else None
                            ),
                            tags=[str(tag) for tag in item.get("tags", [])]
                            if isinstance(item.get("tags", []), list)
                            else [],
                        )
                    )

            operator_metrics = final_operator_output.get("metrics", [])
            if isinstance(operator_metrics, list):
                for index, item in enumerate(operator_metrics, start=1):
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    value = item.get("value", 0)
                    if not isinstance(value, (int, float, str, bool)):
                        continue
                    dimension = item.get("dimension", {})
                    metrics.append(
                        MetricRecord(
                            metric_id=str(item.get("metric_id") or f"{step.step_id}-operator-{index}"),
                            run_id=run_id,
                            case_id=case.case_id,
                            step_id=step.step_id,
                            scope="step",
                            name=str(item["name"]),
                            value=value,
                            unit=str(item["unit"]) if item.get("unit") is not None else None,
                            dimension={str(key): str(item_value) for key, item_value in dimension.items()}
                            if isinstance(dimension, dict)
                            else {},
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

        case_execution_status[case.case_id] = "failed" if case_failed else "passed"
        if case.judge_mode != "builtin":
            continue

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
        if unsatisfied_case_dependencies:
            judge_result.passed = False
            judge_result.label = "case-dependency-failed"
            judge_result.score = 0.0
            judge_result.rationale = (
                "Required setup case did not complete successfully: "
                + ", ".join(unsatisfied_case_dependencies)
            )
        elif case_failed and judge_result.passed:
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
