from __future__ import annotations

from . import token_usage

import json
import hashlib
from contextvars import copy_context
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from .integration import MANAGED_GATEWAYS, sample_runtime_environment, validate_sample_identities, run_agent_task, run_memory_plugin_task, run_memory_task
from .judges import run_builtin_judge, run_llm_judge
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
from .workflow_inputs import InputResolutionError, build_input_scope, iter_input_references, resolve_inputs, validate_workflow
from .workflow_operators import dispatch_step_operator


_sleep = time.sleep
_monotonic = time.monotonic

_MEMORY_FAILURES = {"memory-preparation-failed", "memory-compact-timeout"}


def _memory_preparation(case: CaseRecord, steps: list[StepRecord]) -> bool:
    """Only data preparation, never plugin validation or arbitrary commands."""
    return "phase:setup" in case.labels and all(
        (step.operator_kind == "memory" and step.inputs.get("action") in {"ingest", "flush", "commit"})
        or (step.operator_kind == "poll" and step.inputs.get("probe", {}).get("operator_kind") == "memory")
        for step in steps
    )


def _memory_qa(case: CaseRecord, steps: list[StepRecord]) -> bool:
    if case.reference.get("evaluation_target") != "qa_answer" or case.capability != "memory/question-answering":
        return False
    return (len(steps) == 2 and steps[0].operator_kind == "memory"
            and steps[0].inputs.get("action") == "recall"
            and steps[1].operator_kind == "agent" and steps[1].depends_on == [steps[0].step_id])


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


def _execute_case(
    *,
    run_id: str,
    agent_id: str,
    memory_id: str | None = None,
    runtime_context: WorkflowRuntimeContext | None = None,
    case: CaseRecord,
    case_execution_status: dict[str, str],
    case_failure_kind: dict[str, str],
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
    steps_by_case: dict[str, list[StepRecord]] = {}
    for step in steps:
        steps_by_case.setdefault(step.case_id, []).append(step)

    step_results: list[StepResultRecord] = []
    traces: list[TraceEventRecord] = []
    judge_results: list[JudgeResult] = []
    metrics: list[MetricRecord] = []
    artifacts: list[ArtifactRecord] = []
    judge_job = None

    stdout_dir = run_dir / "artifacts" / "step-stdout"
    stderr_dir = run_dir / "artifacts" / "step-stderr"
    stdout_dir.mkdir(parents=True, exist_ok=True)
    stderr_dir.mkdir(parents=True, exist_ok=True)

    for case in [case]:
        case_step_results: list[StepResultRecord] = []
        results_by_step: dict[str, StepResultRecord] = {}
        preparation = _memory_preparation(case, steps_by_case.get(case.case_id, []))
        resilient_qa = _memory_qa(case, steps_by_case.get(case.case_id, []))
        plugin = runtime_context.memory_integration == "agent_plugin"
        plugin_setup = plugin and "phase:setup" in case.labels and case.capability == "agent-memory/plugin-ingest"
        plugin_qa = (plugin and case.reference.get("evaluation_target") == "qa_answer"
                     and case.capability == "memory/question-answering" and len(steps_by_case.get(case.case_id, [])) == 1
                     and steps_by_case[case.case_id][0].operator_kind == "agent"
                     and steps_by_case[case.case_id][0].inputs.get("metadata", {}).get("purpose") == "memory_qa")
        failed_session_steps: dict[str, str] = {}
        qa_phase_restored = False
        qa_degradation = {}
        recall_fallbacks = set()
        configuration_failed = False
        configuration_blocked_steps: set[str] = set()
        unsatisfied_case_dependencies = [
            dependency
            for dependency in case.depends_on_cases
            if case_execution_status.get(dependency) != "passed"
        ]
        compact_timeout_dependencies = bool(unsatisfied_case_dependencies) and all(
            case_failure_kind.get(dependency) == "memory-compact-timeout"
            for dependency in unsatisfied_case_dependencies
        )
        failed_memory_dependencies = bool(unsatisfied_case_dependencies) and all(
            case_failure_kind.get(dependency) in _MEMORY_FAILURES
            for dependency in unsatisfied_case_dependencies
        )
        inherited_plugin_failure = bool(unsatisfied_case_dependencies) and all(
            case_failure_kind.get(dep) in {"plugin-history-incomplete-qa-ready", "plugin-qa-unavailable"} for dep in unsatisfied_case_dependencies
        )
        plugin_qa_unavailable = plugin_qa and any(case_failure_kind.get(dep) == "plugin-qa-unavailable"
                                               for dep in unsatisfied_case_dependencies)
        if ((failed_memory_dependencies and (resilient_qa or preparation))
                or (inherited_plugin_failure and (plugin_qa or plugin_setup))) and not plugin_qa_unavailable:
            if resilient_qa or plugin_qa:
                qa_degradation["setup_failed"] = list(unsatisfied_case_dependencies)
            unsatisfied_case_dependencies = []
        case_failed = bool(unsatisfied_case_dependencies) or (preparation and failed_memory_dependencies)
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
            failed_dependencies = [dep for dep in step.depends_on
                                   if dep not in results_by_step or results_by_step[dep].status != "passed"]
            plugin_ingest = (plugin_setup and step.operator_kind == "agent"
                             and step.inputs.get("metadata", {}).get("purpose") == "memory_ingest")
            session_key = (step.inputs.get("metadata", {}).get("session_key") if plugin_ingest
                           else step.inputs.get("session_key"))
            session_step = (plugin_ingest or (plugin_setup and step.operator_kind == "memory_plugin"
                            and step.inputs.get("action") in {"commit", "wait_ready"}))
            restore_qa = (plugin_setup and (failed_session_steps or inherited_plugin_failure)
                          and step is executable_steps[-1] and step.operator_kind == "memory_plugin"
                          and step.inputs.get("action") == "set_phase" and step.inputs.get("phase") == "qa")
            preparation_ingest = (
                preparation and step.operator_kind == "memory" and step.inputs.get("action") == "ingest"
            )
            failed_input_dependencies: set[str] = set()
            if preparation or plugin_setup:
                for reference in iter_input_references(step.inputs):
                    root, _, path = reference.partition(".")
                    if root == "steps":
                        dependency = path.partition(".")[0]
                        result = results_by_step.get(dependency)
                        if result is None or result.status != "passed":
                            failed_input_dependencies.add(dependency)
            configuration_blocked = not configuration_blocked_steps.isdisjoint(
                set(failed_dependencies) | failed_input_dependencies
            )
            # A later session waits for the previous attempt in plan order, but
            # consumes its own transcript. Never bypass a failed data reference.
            next_session = (plugin_ingest and isinstance(session_key, str) and session_key
                            and all(dep in failed_session_steps and failed_session_steps[dep] != session_key
                                    for dep in failed_dependencies))
            independent_ingest = (preparation_ingest or next_session) and not configuration_blocked
            fallback_answer = resilient_qa and step.operator_kind == "agent" and all(
                dep in recall_fallbacks for dep in failed_dependencies)
            if (configuration_blocked or failed_input_dependencies
                    or (failed_dependencies and not independent_ingest and not fallback_answer and not (restore_qa and all(dep in failed_session_steps for dep in failed_dependencies)))):
                blocked_by = set(failed_dependencies) | failed_input_dependencies
                if session_step and session_key and blocked_by and blocked_by <= failed_session_steps.keys():
                    failed_session_steps[step.step_id] = session_key
                if configuration_blocked:
                    configuration_blocked_steps.add(step.step_id)
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
                input_scope = build_input_scope(runtime_context, case, case_step_results)
                # Substitute only the answer input. Keep the failed recall record
                # and raw response intact, and never feed error text as evidence.
                if step.operator_kind == "agent":
                    for dep in recall_fallbacks:
                        input_scope["steps"][dep] = {"output": {"evidence_text": ""}}
                resolved_inputs = resolve_inputs(step.inputs, input_scope)
                if restore_qa:
                    resolved_inputs["recover_read_only"] = True
            except InputResolutionError as exc:
                configuration_failed = True
                configuration_blocked_steps.add(step.step_id)
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
            is_ogmemory_flush = (
                runtime_context.memory_integration == "backend_direct"
                and memory_id == "ogmemory"
                and resolved_step.operator_kind == "memory"
                and resolved_step.inputs.get("action") == "flush"
            )
            is_tool_ingest = (
                resolved_step.operator_kind == "agent"
                and resolved_step.inputs.get("metadata", {}).get("purpose") == "memory_ingest"
                and (runtime_context.run_contract.get("memory_plugin_runtime") or {})
                    .get("capabilities", {}).get("agent_tool_ingest")
            )
            max_attempts = (
                1
                if resolution_error is not None or is_memory_ingest or is_ogmemory_flush or is_tool_ingest
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

                    with token_usage.scope(token_usage.context(
                        runtime_context, case_id=step.case_id, step_id=step.step_id,
                        attempt=attempt,
                    )):
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
                except Exception as exc:
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

                if (session_step and isinstance(session_key, str) and session_key and
                        final_result.status == "failed" and resolution_error is None and step.gate_policy == "hard"):
                    failed_session_steps[step.step_id] = session_key
                    break
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
            recall_failed = (resilient_qa and step.operator_kind == "memory"
                             and step.inputs.get("action") == "recall"
                             and result.status == "failed" and resolution_error is None)
            if recall_failed:
                recall_fallbacks.add(step.step_id)
                qa_degradation["recall_failed"] = step.step_id
            if restore_qa:
                qa_phase_restored = (result.status == "passed"
                                     and result.structured_output["output"].get("read_only_verified") is True)
            if (resilient_qa or plugin_qa) and step.operator_kind == "agent" and qa_degradation:
                result.structured_output["qa_degradation"] = dict(qa_degradation)
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
                if recall_failed:
                    continue
                case_failed = True
                if execution_spec.fail_fast and step.step_id not in failed_session_steps and (not preparation or resolution_error is not None):
                    break

        if plugin_setup and inherited_plugin_failure:
            case_failed = True
        case_execution_status[case.case_id] = "failed" if case_failed else "passed"
        if case_failed:
            failed_step_ids = {
                item.step_id
                for item in case_step_results
                if item.status == "failed" or item.gate_passed is False
            }
            readiness_failed = any(
                "wait-ready" in step_id or "wait_ready" in step_id
                for step_id in failed_step_ids
            )
            failed = [item for item in case_step_results if item.status == "failed"]
            compact_timed_out = (
                runtime_context.memory_integration == "backend_direct"
                and memory_id == "ogmemory"
                and "phase:setup" in case.labels
                and (compact_timeout_dependencies or (
                    len(failed) == 1
                    and failed[0].structured_output.get("raw", {}).get("error", {}).get("code") == "CompactTimeout"
                ))
            )
            case_failure_kind[case.case_id] = (
                "runtime-error" if configuration_failed else
                "plugin-history-incomplete-qa-ready" if qa_phase_restored and (
                    failed_session_steps or (inherited_plugin_failure and not failed)) else
                "plugin-qa-unavailable" if plugin_setup and not unsatisfied_case_dependencies else
                "memory-compact-timeout" if compact_timed_out
                else "memory-preparation-failed" if preparation and not unsatisfied_case_dependencies
                else "runtime-not-ready" if readiness_failed
                else "runtime-error"
            )
        if case.judge_mode == "none":
            continue

        judge_started_at = _timestamp()
        judge_input = JudgeInput(
            case_id=case.case_id,
            goal=case.goal,
            reference=case.reference,
            step_results=[item.model_dump(mode="json") for item in case_step_results],
            trace_events=[item.model_dump(mode="json") for item in traces if item.case_id == case.case_id],
            artifacts=[item.model_dump(mode="json") for item in artifacts if item.case_id == case.case_id],
        )
        if unsatisfied_case_dependencies:
            dependency_kinds = {
                case_failure_kind.get(dependency, "runtime-error")
                for dependency in unsatisfied_case_dependencies
            }
            label = (
                "runtime-not-ready"
                if "runtime-not-ready" in dependency_kinds
                else "runtime-error"
            )
            judge_result = JudgeResult(
                judge_id=f"{case.case_id}-{case.judge_mode}",
                run_id=run_id,
                case_id=case.case_id,
                passed=None,
                label=label,
                score=None,
                rationale=(
                    "Required setup case did not complete successfully: "
                    + ", ".join(unsatisfied_case_dependencies)
                ),
            )
        elif case_failed:
            judge_result = JudgeResult(
                judge_id=f"{case.case_id}-{case.judge_mode}",
                run_id=run_id,
                case_id=case.case_id,
                passed=None,
                label=case_failure_kind.get(case.case_id, "runtime-error"),
                score=None,
                rationale="One or more hard gates failed before final judge.",
            )
        else:
            judge_job = judge_input
            continue
        judge_results.append(judge_result)
        traces.extend(_judge_traces(case, judge_result, judge_started_at))

    return {
        "step_results": step_results,
        "traces": traces,
        "judge_results": judge_results,
        "metrics": metrics,
        "artifacts": artifacts,
        "_judge_job": judge_job,
        "_execution_status": case_execution_status[case.case_id],
        "_failure_kind": case_failure_kind.get(case.case_id),
    }


_RESULT_FIELDS = ("step_results", "traces", "judge_results", "metrics", "artifacts")


def _judge_case(run_id, case, judge_input, runtime_context):
    started = _timestamp()
    try:
        if case.judge_mode == "external":
            result = run_llm_judge(run_id, judge_input,
                runtime_config=runtime_context.run_contract.get("judge_runtime", {}))
        else:
            result = run_builtin_judge(run_id, judge_input)
    except Exception as exc:
        # A single scoring failure must not discard already generated answers.
        result = JudgeResult(judge_id=f"{case.case_id}-{case.judge_mode}", run_id=run_id,
            case_id=case.case_id, passed=None, score=None, label="judge-error",
            rationale=f"Judge failed ({type(exc).__name__}): {exc}")
    return result, _judge_traces(case, result, started)


def _judge_traces(case, result, started):
    return [
        TraceEventRecord(trace_id=f"{case.case_id}-judge-started", case_id=case.case_id,
            event_type="case_judge_started", timestamp=started),
        TraceEventRecord(trace_id=f"{case.case_id}-judge-finished", case_id=case.case_id,
            event_type="case_judge_finished", timestamp=_timestamp(), payload={"passed": result.passed, "label": result.label}),
    ]


def _qa_group_key(case, steps, integration):
    # Opt in only the composer's read-only QA shape, never arbitrary workflows.
    if case.reference.get("evaluation_target") != "qa_answer" or case.capability != "memory/question-answering":
        return None
    sample, checkpoint = case.reference.get("sample_id"), case.reference.get("checkpoint_id")
    if not sample or not checkpoint or not case.depends_on_cases:
        return None
    if integration == "backend_direct":
        if len(steps) != 2 or steps[0].operator_kind != "memory" or steps[0].inputs.get("action") != "recall":
            return None
        if steps[1].operator_kind != "agent" or steps[1].depends_on != [steps[0].step_id]:
            return None
    elif len(steps) != 1 or steps[0].operator_kind != "agent":
        return None
    session = steps[-1].inputs.get("metadata", {}).get("session_key")
    if not isinstance(session, str) or not session.strip():
        return None
    return (sample, checkpoint, tuple(case.depends_on_cases))


def _sample_partitions(cases, by_case, *, plugin=False):
    """Reject ambiguous isolation before starting any sample writes."""
    owners, partitions, scopes, sessions = {}, {}, {}, set()
    for case in cases:
        sample = case.source_metadata.get("sample_id") or case.reference.get("sample_id")
        if not isinstance(sample, str) or not sample:
            raise ValueError("Sample concurrency requires a sample_id on every case")
        if any(value and value != sample for value in
               (case.source_metadata.get("sample_id"), case.reference.get("sample_id"))):
            raise ValueError("Conflicting sample_id in case metadata and reference")
        owners[case.case_id] = sample
        partitions.setdefault(sample, []).append(case)
        for step in by_case[case.case_id]:
            inputs = step.inputs
            if step.operator_kind == "poll":
                inputs = inputs.get("probe", {}).get("inputs", {})
            if step.operator_kind in {"memory", "memory_plugin", "poll"} and (not plugin or "scope_id" in inputs):
                scope = inputs.get("scope_id")
                if not isinstance(scope, str) or not scope:
                    raise ValueError("Sample concurrency requires explicit memory scope_id")
                if scopes.setdefault(scope, sample) != sample:
                    raise ValueError("Concurrent samples must not share memory scope_id")
            if step.operator_kind == "agent":
                session = inputs.get("metadata", {}).get("session_key")
                if not isinstance(session, str) or not session or session.lower() in sessions:
                    raise ValueError("Concurrent samples require unique Agent session_key")
                sessions.add(session.lower())
    for case in cases:
        if any(owners.get(dep) != owners[case.case_id] for dep in case.depends_on_cases):
            raise ValueError("Sample concurrency does not support cross-sample dependencies")
    return partitions, scopes


def execute_cases(*, run_id: str, agent_id: str, memory_id: str | None = None,
                  runtime_context: WorkflowRuntimeContext | None = None,
                  cases: list[CaseRecord], steps: list[StepRecord],
                  execution_spec: ExecutionSpec, run_dir: Path,
                  phase_sink=None, target_ids: list[str] | None = None, sample_gateways=None, finalize_sample=None) -> dict[str, list]:
    runtime_context = runtime_context or WorkflowRuntimeContext(
        run_id=run_id, run_dir=str(run_dir), benchmark_id="", agent_id=agent_id, memory_id=memory_id)
    integration = runtime_context.memory_integration
    qa_limit = execution_spec.qa_per_sample or (1 if integration == "agent_plugin" else execution_spec.qa_concurrency)
    concurrent_plugin_qa = integration == "agent_plugin" and qa_limit > 1
    if qa_limit > execution_spec.qa_concurrency:
        raise ValueError("qa_per_sample cannot exceed qa_concurrency")
    if integration == "agent_plugin" and execution_spec.qa_concurrency > 1 and execution_spec.sample_concurrency == 1 and not concurrent_plugin_qa:
        raise ValueError("agent_plugin requires qa_concurrency=1 unless parallel samples have dedicated runtime bindings")
    validate_workflow(cases=cases, steps=steps, execution_spec=execution_spec,
        memory_id=memory_id, memory_plugin_id=runtime_context.memory_plugin_id)
    by_case = {case.case_id: [] for case in cases}
    for step in steps:
        by_case[step.case_id].append(step)
    parallel_samples = execution_spec.sample_concurrency > 1
    if parallel_samples and phase_sink is not None:
        raise ValueError("Phase profiling requires sample_concurrency=1")
    bound_samples = bool(runtime_context.sample_environments)
    if (integration == "agent_plugin" and runtime_context.memory_plugin_id == "openclaw-ogmemory"
            and (bound_samples or (parallel_samples and not sample_gateways))):
        raise ValueError("oGMemory plugin sample concurrency requires deployment-managed Gateways")
    if bound_samples and phase_sink is not None:
        raise ValueError("Phase profiling does not support sample runtime bindings")
    partitions, scope_owners = _sample_partitions(cases, by_case, plugin=integration == "agent_plugin") if parallel_samples or bound_samples else ({}, {})
    if integration == "agent_plugin" and (parallel_samples or bound_samples):
        environments = runtime_context.sample_environments
        if sample_gateways:
            if (runtime_context.memory_plugin_id not in MANAGED_GATEWAYS or environments
                    or len(sample_gateways) != execution_spec.sample_concurrency or not finalize_sample):
                raise ValueError("Invalid managed sample Gateway configuration")
            container_key, endpoint_key = MANAGED_GATEWAYS[runtime_context.memory_plugin_id]
            if endpoint_key:
                endpoints = {env.get(endpoint_key) for env in sample_gateways}
                if len(endpoints) != 1 or not all(endpoints):
                    raise ValueError("Managed Gateways require one shared memory endpoint")
            else:
                for key in ("MEMOS_LOCAL_RUNTIME_DIR", "MEMOS_LOCAL_VIEWER_URL"):
                    values = {env.get(key) for env in sample_gateways}
                    if len(values) != len(sample_gateways) or not all(values):
                        raise ValueError(f"Embedded MemOS Gateways require distinct {key}")
            environments = dict(enumerate(sample_gateways))
        elif set(environments) != set(partitions):
            raise ValueError("Every plugin sample requires a dedicated runtime environment")
        for key in ("OPENCLAW_GATEWAY_URL", "OPENCLAW_STATE_DIR", container_key if sample_gateways else "OPENCLAW_BIN"):
            values = [env.get(key, "").rstrip("/") for env in environments.values()]
            if any(not value for value in values) or len(set(values)) != len(values):
                raise ValueError(f"Plugin samples require distinct {key}")
        for name in ("OGMEM_PLUGIN_STATE_FILE", "MEM0_PLUGIN_STATE_FILE", "MEMOS_LOCAL_RUNTIME_DIR"):
            paths = [Path(env[name]).resolve() for env in environments.values() if env.get(name)]
            if paths and (len(paths) != len(environments) or len(set(paths)) != len(paths)):
                raise ValueError(f"Plugin samples require distinct {name}")

    if len(partitions) > 1:
        sample_scopes = {sample: set() for sample in partitions}
        for scope, sample in scope_owners.items():
            sample_scopes[sample].add(scope)
        skill = runtime_context.memory_plugin_id if integration == "agent_plugin" else memory_id
        validate_sample_identities(runtime_context, skill, sample_scopes, **({"managed_gateway": sample_gateways[0]} if sample_gateways else {}))
    groups = []
    keys = []
    sessions = set()
    for case in cases:
        key = _qa_group_key(case, by_case[case.case_id], integration)
        if key is not None and execution_spec.qa_concurrency > 1:
            session = by_case[case.case_id][-1].inputs["metadata"]["session_key"].lower()
            if session in sessions:
                raise ValueError("Concurrent QA requires a unique session_key for every question")
            sessions.add(session)
        if key is not None and keys and key == keys[-1]:
            groups[-1].append(case)
        else:
            groups.append([case])
            keys.append(key)
    def group_phase(group):
        case_steps = [by_case[case.case_id] for case in group]
        if all(steps and all(step.operator_kind == "memory" and
                             step.inputs.get("action") == "ingest" for step in steps)
               for steps in case_steps):
            return "ingest"
        if all(steps and all(step.operator_kind == "poll" for step in steps)
               for steps in case_steps):
            return "drain"
        if all(case.reference.get("evaluation_target") == "qa_answer" for case in group):
            if integration == "backend_direct" and all(
                len(steps) == 2 and steps[0].operator_kind == "memory" and
                steps[0].inputs.get("action") == "recall" and
                steps[1].operator_kind == "agent" for steps in case_steps):
                return "recall"
            if integration == "agent_plugin" and all(
                len(steps) == 1 and steps[0].operator_kind == "agent" for steps in case_steps):
                return "recall"
        if all(steps and all(step.operator_kind == "memory" and
                             step.inputs.get("action") == "recall" for step in steps)
               for steps in case_steps):
            return "recall"
        return None

    batches = []
    for group in groups:
        phase = group_phase(group)
        if batches and batches[-1][0] == phase:
            batches[-1][1].append(group)
        else:
            batches.append((phase, [group]))
    selected_phases = [phase for phase, _ in batches if phase is not None]
    if phase_sink is not None and phase_sink.session.config.mode == "required" and any(
        phase is None and any(step.operator_kind in {"memory", "memory_plugin"}
                              for group in batch for case in group for step in by_case[case.case_id])
        for phase, batch in batches
    ):
        raise ValueError("phase_boundary_unavailable")
    missing_phases = (set(phase_sink.session.config.phases) - set(selected_phases)
                      if phase_sink is not None else set())
    if integration == "backend_direct" and "recall" in selected_phases:
        missing_phases.discard("drain")
    if phase_sink is not None and phase_sink.session.config.mode == "required" and (
            len(selected_phases) != len(set(selected_phases)) or missing_phases):
        from .profiling_run import mark_phase_unavailable
        mark_phase_unavailable(phase_sink)
        raise RuntimeError("phase_boundary_unavailable")
    if phase_sink is not None and len(selected_phases) != len(set(selected_phases)):
        # Interleaved stages cannot be represented by one capture per phase.
        from .profiling_run import mark_phase_unavailable
        mark_phase_unavailable(phase_sink)
        phase_sink = None
    outputs, statuses, failures = {}, {}, {}
    progress = run_dir / "records" / "case-progress"
    progress.mkdir(parents=True, exist_ok=True)
    client_event_path = run_dir / "profiling_client_events.jsonl" if phase_sink is not None else None
    if client_event_path is not None:
        client_event_path.touch()

    def archive(case, output):
        target = progress / (hashlib.sha256(case.case_id.encode()).hexdigest() + ".json")
        payload = {"case_id": case.case_id, "execution_status": output["_execution_status"],
                   "judge_pending": output["_judge_job"] is not None,
                   **{key: [item.model_dump(mode="json") for item in output[key]] for key in _RESULT_FIELDS}}
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    with ThreadPoolExecutor(max_workers=execution_spec.qa_concurrency) as qa_pool, \
         ThreadPoolExecutor(max_workers=execution_spec.judge_concurrency) as judge_pool:
      def run_group(group, phase, statuses=statuses, failures=failures):
            # Drain this group before the next checkpoint of the same sample.
            remaining = iter(group)
            pending = {}
            qa_active = 0
            # Only read-only QA groups can contain multiple cases. All groups share the global pool.
            capacity = (1 if execution_spec.qa_concurrency == execution_spec.judge_concurrency == 1
                        else execution_spec.qa_concurrency + execution_spec.judge_concurrency * 2)
            exhausted = False
            while pending or not exhausted:
                while not exhausted and qa_active < qa_limit and len(pending) < capacity:
                    case = next(remaining, None)
                    if case is None:
                        exhausted = True
                        break
                    # Preparation runs in the sample worker, outside the global QA pool.
                    inline_setup = parallel_samples and all(
                        item.judge_mode == "none" and (
                            "phase:setup" in item.labels or not any(
                                step.operator_kind == "agent" for step in by_case[item.case_id]
                            )
                        ) for item in group
                    )
                    def submit(fn, *args, **kwargs):
                        if not inline_setup:
                            return qa_pool.submit(fn, *args, **kwargs)
                        future = Future()
                        try:
                            future.set_result(fn(*args, **kwargs))
                        except Exception as exc:
                            future.set_exception(exc)
                        return future
                    future = submit(copy_context().run, _execute_case, run_id=run_id, agent_id=agent_id, memory_id=memory_id,
                        runtime_context=runtime_context.model_copy(update={"sample_environments": {},
                            "concurrent_plugin_qa": concurrent_plugin_qa and phase == "recall"}), case=case, steps=by_case[case.case_id],
                        case_execution_status=dict(statuses), case_failure_kind=dict(failures),
                        execution_spec=execution_spec, run_dir=run_dir)
                    pending[future] = ("qa", case)
                    qa_active += 1
                if not pending:
                    continue
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    kind, case = pending.pop(future)
                    if kind == "qa":
                        qa_active -= 1
                        output = future.result()
                        outputs[case.case_id] = output
                        statuses[case.case_id] = output["_execution_status"]
                        if output["_failure_kind"]:
                            failures[case.case_id] = output["_failure_kind"]
                        archive(case, output)
                        if client_event_path is not None and phase in {"ingest", "drain", "recall"}:
                            timed = [item for item in output["step_results"]
                                     if item.started_at is not None and item.ended_at is not None]
                            operation_status = output["_execution_status"]
                            if integration == "backend_direct" and phase == "recall":
                                recall_ids = {
                                    step.step_id for step in by_case[case.case_id]
                                    if step.operator_kind == "memory" and step.inputs.get("action") == "recall"
                                }
                                timed = [item for item in timed
                                         if item.step_id in recall_ids and item.status != "skipped"]
                                operation_status = "passed" if all(item.status == "passed" for item in timed) else "failed"
                            if timed:
                                started = min(item.started_at for item in timed).astimezone(timezone.utc)
                                ended = max(item.ended_at for item in timed).astimezone(timezone.utc)
                                with client_event_path.open("a", encoding="utf-8") as stream:
                                    stream.write(json.dumps({"run_id": run_id, "request_id": case.case_id,
                                        "operation": {"ingest": "add", "drain": "drain", "recall": "search"}[phase],
                                        "started_utc": started.isoformat(), "ended_utc": ended.isoformat(),
                                        "status": "ok" if operation_status == "passed" else "failed"}) + "\n")
                        if output["_judge_job"] is not None:
                            judge = judge_pool.submit(copy_context().run, _judge_case, run_id, case, output["_judge_job"], runtime_context)
                            pending[judge] = ("judge", case)
                    else:
                        result, traces = future.result()
                        output = outputs[case.case_id]
                        output["judge_results"].append(result)
                        output["traces"].extend(traces)
                        output["_judge_job"] = None
                        archive(case, output)
      if parallel_samples or bound_samples:
        sample_groups = {sample: [] for sample in partitions}
        owners = {case.case_id: sample for sample, rows in partitions.items() for case in rows}
        for group in groups:
            sample_groups[owners[group[0].case_id]].append(group)
        def run_sample(sample, gateway=None):
            sample_statuses, sample_failures = {}, {}
            with sample_runtime_environment(runtime_context, sample, gateway):
                try:
                    for group in sample_groups[sample]:
                        run_group(group, group_phase(group), sample_statuses, sample_failures)
                finally:
                    if gateway:
                        finalize_sample(sample, gateway)

        def run_gateway(gateway, work):
            from queue import Empty
            while True:
                try:
                    sample = work.get_nowait()
                except Empty:
                    return
                run_sample(sample, gateway)
        with ThreadPoolExecutor(max_workers=execution_spec.sample_concurrency) as sample_pool:
            if sample_gateways:
                from queue import Queue
                work = Queue()
                for sample in sample_groups:
                    work.put(sample)
                pending_samples = [sample_pool.submit(copy_context().run, run_gateway, gateway, work)
                                   for gateway in sample_gateways]
            else:
                pending_samples = [sample_pool.submit(copy_context().run, run_sample, sample)
                                   for sample in sample_groups]
            for future in pending_samples:
                future.result()
      else:
        drain_recorded = False
        for phase, batch in batches:
          if (phase_sink is not None and phase == "recall" and not drain_recorded and
                  "drain" in phase_sink.session.config.phases and "drain" not in selected_phases and
                  integration == "backend_direct"):
              phase_sink.run_phase("drain", target_ids or [], lambda: None, applicable=False)
              drain_recorded = True
          def run_batch():
              for group in batch:
                  run_group(group, phase)
          if phase_sink is not None and phase in phase_sink.session.config.phases:
              phase_sink.run_phase(phase, target_ids or [], run_batch,
                                   request_count=lambda: sum(len(group) for group in batch))
              if phase_sink.session.config.mode == "required" and phase_sink.invalid_profile:
                  raise RuntimeError("invalid_profile")
          else:
              run_batch()
    if phase_sink is not None and missing_phases:
        from .profiling_run import mark_phase_unavailable
        mark_phase_unavailable(phase_sink)
    # Stable report order is independent of completion order.
    return {key: [item for case in cases for item in outputs[case.case_id][key]] for key in _RESULT_FIELDS}
