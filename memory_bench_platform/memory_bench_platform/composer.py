from __future__ import annotations

import re
from typing import Any

from .benchmark_scenario import BenchmarkScenario, RunBinding, ScenarioQuestion, TimelineEvent
from .evaluation_profiles import resolve_evaluation_profile


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]+", "-", value).strip("-").lower() or "item"


def _episode_scope_id(binding: RunBinding, sample_id: str, namespace_hint: str | None) -> str:
    """A ScenarioSample is one memory episode shared by all timeline stages."""
    return f"{binding.run_id}:{namespace_hint or sample_id}"


def _event_content(event: TimelineEvent) -> str:
    content = event.payload.get("content")
    if isinstance(content, str) and content:
        return content
    messages = event.payload.get("messages", [])
    if isinstance(messages, list):
        lines = []
        for message in messages:
            if isinstance(message, dict):
                role = str(message.get("role") or message.get("speaker") or "user")
                text = str(message.get("content") or message.get("text") or "")
                lines.append(f"{role}: {text}")
        if lines:
            return "\n".join(lines)
    return str(event.payload)


def _case(
    case_id: str,
    *,
    title: str,
    goal: str,
    capability: str,
    depends_on_cases: list[str],
    reference: dict[str, Any] | None = None,
    labels: list[str] | None = None,
    source_metadata: dict[str, Any] | None = None,
    judge_mode: str = "none",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "title": title,
        "goal": goal,
        "capability": capability,
        "depends_on_cases": depends_on_cases,
        "reference": reference or {},
        "labels": labels or [],
        "source_metadata": source_metadata or {},
        "judge_mode": judge_mode,
    }


def _step(
    step_id: str,
    case_id: str,
    name: str,
    operator_kind: str,
    *,
    depends_on: list[str] | None = None,
    timeout_seconds: int = 90,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "case_id": case_id,
        "name": name,
        "operator_kind": operator_kind,
        "depends_on": depends_on or [],
        "retry_limit": 0,
        "timeout_seconds": timeout_seconds,
        "gate_policy": "hard",
        "inputs": inputs or {},
    }


def _nested_capability(
    runtime_capabilities: dict[str, Any],
    runtime_kind: str,
    section: str,
    name: str,
    *,
    default: bool,
) -> bool:
    runtime = runtime_capabilities.get(runtime_kind, {})
    if not isinstance(runtime, dict):
        return default
    nested = runtime.get(section)
    if not isinstance(nested, dict) or name not in nested:
        return default
    return bool(nested.get(name))


def _direct_barrier_policy(runtime_capabilities: dict[str, Any]) -> tuple[bool, bool]:
    memory = runtime_capabilities.get("memory", {})
    async_ingest = bool(memory.get("async_ingest", True)) if isinstance(memory, dict) else True
    commit_required = _nested_capability(
        runtime_capabilities,
        "memory",
        "commit",
        "required_after_ingest",
        default=True,
    )
    readiness_required = async_ingest and _nested_capability(
        runtime_capabilities,
        "memory",
        "readiness",
        "supported",
        default=True,
    )
    return commit_required, readiness_required


def _plugin_barrier_policy(runtime_capabilities: dict[str, Any]) -> tuple[bool, bool]:
    commit_required = _nested_capability(
        runtime_capabilities,
        "memory_plugin",
        "commit",
        "required_after_ingest",
        default=True,
    )
    readiness_required = _nested_capability(
        runtime_capabilities,
        "memory_plugin",
        "readiness",
        "supported",
        default=True,
    )
    return commit_required, readiness_required


def _question_case(
    scenario: BenchmarkScenario,
    sample_id: str,
    checkpoint_id: str,
    question: ScenarioQuestion,
    question_index: int,
    setup_case_id: str,
    expected_step_id: str,
    *,
    integration: str,
    case_id: str | None = None,
    judge_mode: str = "external",
    evaluation_target: str = "qa_answer",
    evaluation_profile: str | None = None,
    evaluation_at: str | None = None,
) -> dict[str, Any]:
    return _case(
        case_id or f"{_slug(sample_id)}-{_slug(question.question_id)}",
        title=f"{scenario.benchmark_id} QA {sample_id} #{question_index}",
        goal="Answer the benchmark question using the configured memory runtime.",
        capability="memory/question-answering",
        depends_on_cases=[setup_case_id],
        reference={
            **question.metadata,
            "question_id": question.question_id,
            "question": question.question,
            "expected_answer": question.reference,
            "expected_step_id": expected_step_id,
            "category": question.category,
            "sample_id": sample_id,
            "checkpoint_id": checkpoint_id,
            "evaluation_target": evaluation_target,
            "evaluation_extractor": (
                "evidence_text" if evaluation_target == "retrieval" else "qa_answer"
            ),
            "evaluation_profile": evaluation_profile,
            "evaluation_at": evaluation_at,
        },
        labels=[
            f"source:{scenario.benchmark_id}",
            f"category:{question.category}",
            f"memory-integration:{integration.replace('_', '-')}",
        ],
        source_metadata={
            "sample_id": sample_id,
            "question_index": question_index,
            "question_id": question.question_id,
            "checkpoint_id": checkpoint_id,
        },
        judge_mode=judge_mode,
    )


def _question_case_id(
    cases: list[dict[str, Any]],
    sample_id: str,
    checkpoint_id: str,
    question_id: str,
) -> str:
    base = f"{_slug(sample_id)}-{_slug(question_id)}"
    if not any(case.get("case_id") == base for case in cases):
        return base
    return f"{base}-{_slug(checkpoint_id)}"


def _compose_backend_direct(
    scenario: BenchmarkScenario,
    binding: RunBinding,
    runtime_capabilities: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    for sample in scenario.samples:
        scope_id = _episode_scope_id(binding, sample.sample_id, sample.namespace_hint)
        pending_events: list[TimelineEvent] = []
        previous_checkpoint_cases: list[str] = []
        stage_index = 0
        for event in sample.timeline:
            if event.type != "checkpoint":
                pending_events.append(event)
                continue
            stage_index += 1
            setup_case_id = f"{_slug(sample.sample_id)}-stage-{stage_index}-setup"
            cases.append(
                _case(
                    setup_case_id,
                    title=f"{scenario.benchmark_id} memory setup {sample.sample_id} stage {stage_index}",
                    goal="Ingest the current timeline stage and wait until it is searchable.",
                    capability="memory/ingest",
                    depends_on_cases=previous_checkpoint_cases,
                    labels=[f"source:{scenario.benchmark_id}", "phase:setup"],
                    source_metadata={
                        "sample_id": sample.sample_id,
                        "stage_index": stage_index,
                        "event_count": len(pending_events),
                        "checkpoint_id": event.event_id,
                    },
                )
            )
            previous_step_id = ""
            ingested_events: list[tuple[TimelineEvent, str]] = []
            for timeline_event in pending_events:
                event_slug = _slug(timeline_event.event_id)
                ingest_step_id = f"{setup_case_id}-{event_slug}-ingest"
                steps.append(
                    _step(
                        ingest_step_id,
                        setup_case_id,
                        f"ingest_{timeline_event.event_id}",
                        "memory",
                        depends_on=[previous_step_id] if previous_step_id else [],
                        timeout_seconds=30,
                        inputs={
                            "action": "ingest",
                            "content": _event_content(timeline_event),
                            "event_id": timeline_event.event_id,
                            "timestamp": timeline_event.timestamp,
                            "occurred_at": timeline_event.timestamp,
                            "scope_id": scope_id,
                        },
                    )
                )
                ingested_events.append((timeline_event, ingest_step_id))
                previous_step_id = ingest_step_id

            commit_required, readiness_required = _direct_barrier_policy(runtime_capabilities)
            for timeline_event, ingest_step_id in ingested_events:
                event_slug = _slug(timeline_event.event_id)
                flush_step_id = f"{setup_case_id}-{event_slug}-flush"
                wait_step_id = f"{setup_case_id}-{event_slug}-wait-ready"
                readiness_operation_step_id = ingest_step_id
                if commit_required:
                    steps.append(
                        _step(
                            flush_step_id,
                            setup_case_id,
                            f"flush_{timeline_event.event_id}",
                            "memory",
                            depends_on=[previous_step_id],
                            timeout_seconds=30,
                            inputs={
                                "action": "flush",
                                "session_id": {
                                    "$ref": f"steps.{ingest_step_id}.output.session_id"
                                },
                                "operation": {
                                    "$ref": f"steps.{ingest_step_id}.output.operation"
                                },
                                "scope_id": scope_id,
                            },
                        )
                    )
                    previous_step_id = flush_step_id
                    readiness_operation_step_id = flush_step_id
                if readiness_required:
                    steps.append(
                        _step(
                            wait_step_id,
                            setup_case_id,
                            f"wait_ready_{timeline_event.event_id}",
                            "poll",
                            depends_on=[previous_step_id],
                            timeout_seconds=600,
                            inputs={
                                "interval_seconds": 1,
                                "probe": {
                                    "operator_kind": "memory",
                                    "action": "status",
                                    "inputs": {
                                        "operation": {
                                            "$ref": f"steps.{readiness_operation_step_id}.output.operation"
                                        },
                                        "scope_id": scope_id,
                                    },
                                },
                                "success_when": {"path": "state", "equals": "completed"},
                                "failure_when": {"path": "state", "equals": "failed"},
                            },
                        )
                    )
                    previous_step_id = wait_step_id
            pending_events = []

            evaluation = event.evaluation
            assert evaluation is not None
            checkpoint_cases: list[str] = []
            for question_index, question in enumerate(evaluation.questions, start=1):
                evaluation_profile = evaluation.profile or scenario.evaluation.profile
                profile_handler = resolve_evaluation_profile(evaluation_profile)
                case_id = _question_case_id(
                    cases,
                    sample.sample_id,
                    event.event_id,
                    question.question_id,
                )
                recall_step_id = f"{case_id}-memory-recall"
                answer_step_id = f"{case_id}-agent-answer"
                expected_step_id = answer_step_id if evaluation.target == "qa_answer" else recall_step_id
                cases.append(
                    _question_case(
                        scenario,
                        sample.sample_id,
                        event.event_id,
                        question,
                        question_index,
                        setup_case_id,
                        expected_step_id,
                        integration=binding.memory_integration,
                        case_id=case_id,
                        judge_mode=profile_handler.judge_mode,
                        evaluation_target=evaluation.target,
                        evaluation_profile=evaluation_profile,
                        evaluation_at=event.timestamp,
                    )
                )
                checkpoint_cases.append(case_id)
                steps.append(
                    _step(
                        recall_step_id,
                        case_id,
                        "memory_recall",
                        "memory",
                        inputs={
                            "action": "recall",
                            "query": question.question,
                            "node_limit": 10,
                            "scope_id": scope_id,
                            "evaluation_at": event.timestamp,
                        },
                    )
                )
                if evaluation.target == "qa_answer":
                    steps.append(
                        _step(
                            answer_step_id,
                            case_id,
                            "agent_answer",
                            "agent",
                            depends_on=[recall_step_id],
                            inputs={
                                "system_prompt": (
                                    "Answer the question using only the recalled memory evidence. "
                                    "If the evidence is insufficient, abstain conservatively."
                                ),
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": {
                                            "$template": (
                                                "Recalled memory evidence:\n"
                                                f"{{{{ steps.{recall_step_id}.output.evidence_text }}}}\n\n"
                                                f"Question: {question.question}"
                                            )
                                        },
                                    }
                                ],
                                "metadata": {
                                    "sample_id": sample.sample_id,
                                    "scope_id": scope_id,
                                    "question_id": question.question_id,
                                    **(
                                        {"agent_id": binding.agent_runtime_id}
                                        if binding.agent_runtime_id
                                        else {}
                                    ),
                                    "local": binding.agent_local,
                                },
                            },
                        )
                    )
                elif evaluation.target != "retrieval":
                    raise ValueError(
                        f"backend_direct composer does not yet support target {evaluation.target!r}"
                    )
            previous_checkpoint_cases = checkpoint_cases or [setup_case_id]
    return cases, steps


def _compose_agent_plugin(
    scenario: BenchmarkScenario,
    binding: RunBinding,
    runtime_capabilities: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    for sample in scenario.samples:
        scope_id = _episode_scope_id(binding, sample.sample_id, sample.namespace_hint)
        pending_events: list[TimelineEvent] = []
        previous_checkpoint_cases: list[str] = []
        stage_index = 0
        initialized = False
        namespace = scope_id.replace(":", "-")
        for event in sample.timeline:
            if event.type != "checkpoint":
                pending_events.append(event)
                continue
            stage_index += 1
            setup_case_id = f"{_slug(sample.sample_id)}-stage-{stage_index}-plugin-setup"
            cases.append(
                _case(
                    setup_case_id,
                    title=f"{scenario.benchmark_id} plugin setup {sample.sample_id} stage {stage_index}",
                    goal="Ingest the current stage through the Agent memory plugin.",
                    capability="agent-memory/plugin-ingest",
                    depends_on_cases=previous_checkpoint_cases,
                    labels=[
                        f"source:{scenario.benchmark_id}",
                        "phase:setup",
                        "memory-integration:agent-plugin",
                    ],
                    source_metadata={
                        "sample_id": sample.sample_id,
                        "stage_index": stage_index,
                        "event_count": len(pending_events),
                        "checkpoint_id": event.event_id,
                    },
                )
            )
            previous_step_id = ""
            if not initialized:
                validate_id = f"{setup_case_id}-validate"
                prepare_id = f"{setup_case_id}-prepare"
                steps.append(
                    _step(validate_id, setup_case_id, "validate_memory_plugin", "memory_plugin", inputs={"action": "validate"})
                )
                steps.append(
                    _step(
                        prepare_id,
                        setup_case_id,
                        "prepare_memory_plugin",
                        "memory_plugin",
                        depends_on=[validate_id],
                        inputs={"action": "prepare", "namespace": namespace, "scope_id": scope_id},
                    )
                )
                previous_step_id = prepare_id
                initialized = True
            ingest_phase_id = f"{setup_case_id}-set-ingest-phase"
            steps.append(
                _step(
                    ingest_phase_id,
                    setup_case_id,
                    "set_plugin_ingest_phase",
                    "memory_plugin",
                    depends_on=[previous_step_id] if previous_step_id else [],
                    inputs={"action": "set_phase", "phase": "ingest"},
                )
            )
            previous_step_id = ingest_phase_id
            ingested_events: list[tuple[TimelineEvent, str, str]] = []
            for timeline_event in pending_events:
                event_slug = _slug(timeline_event.event_id)
                agent_step_id = f"{setup_case_id}-{event_slug}-agent-ingest"
                session_key = f"{binding.run_id}:ingest-{sample.sample_id}-{timeline_event.event_id}"
                steps.append(
                    _step(
                        agent_step_id,
                        setup_case_id,
                        f"agent_ingest_{timeline_event.event_id}",
                        "agent",
                        depends_on=[previous_step_id],
                        timeout_seconds=900,
                        inputs={
                            "system_prompt": (
                                "The user message is historical content supplied for long-term memory "
                                "ingestion. Do not summarize, transform, infer, or add facts. Reply exactly INGEST_OK."
                            ),
                            "messages": [{"role": "user", "content": _event_content(timeline_event)}],
                            "metadata": {
                                "sample_id": sample.sample_id,
                                "scope_id": scope_id,
                                "event_id": timeline_event.event_id,
                                "session_key": session_key,
                                **(
                                    {"agent_id": binding.agent_runtime_id}
                                    if binding.agent_runtime_id
                                    else {}
                                ),
                                "local": binding.agent_local,
                                "timeout_seconds": 900,
                                "purpose": "memory_ingest",
                            },
                        },
                    )
                )
                ingested_events.append((timeline_event, agent_step_id, session_key))
                previous_step_id = agent_step_id

            commit_required, readiness_required = _plugin_barrier_policy(runtime_capabilities)
            for timeline_event, agent_step_id, session_key in ingested_events:
                event_slug = _slug(timeline_event.event_id)
                commit_step_id = f"{setup_case_id}-{event_slug}-commit"
                wait_step_id = f"{setup_case_id}-{event_slug}-wait-ready"
                readiness_operation_step_id: str | None = None
                if commit_required:
                    steps.append(
                        _step(
                            commit_step_id,
                            setup_case_id,
                            f"commit_{timeline_event.event_id}",
                            "memory_plugin",
                            depends_on=[previous_step_id],
                            inputs={
                                "action": "commit",
                                "session_key": session_key,
                                "session_handle": {
                                    "$ref": f"steps.{agent_step_id}.output.session_handle"
                                },
                                "agent_id": binding.agent_runtime_id or "main",
                            },
                        )
                    )
                    previous_step_id = commit_step_id
                    readiness_operation_step_id = commit_step_id
                if readiness_required:
                    wait_inputs: dict[str, Any] = {
                        "action": "wait_ready",
                        "session_key": session_key,
                        "timeout_seconds": 600,
                        "grace_seconds": 0,
                        "interval_seconds": 2,
                    }
                    if readiness_operation_step_id:
                        wait_inputs["operation"] = {
                            "$ref": f"steps.{readiness_operation_step_id}.output.operation"
                        }
                    steps.append(
                        _step(
                            wait_step_id,
                            setup_case_id,
                            f"wait_ready_{timeline_event.event_id}",
                            "memory_plugin",
                            depends_on=[previous_step_id],
                            timeout_seconds=660,
                            inputs=wait_inputs,
                        )
                    )
                    previous_step_id = wait_step_id
            pending_events = []
            qa_phase_id = f"{setup_case_id}-set-qa-phase"
            steps.append(
                _step(
                    qa_phase_id,
                    setup_case_id,
                    "set_plugin_qa_phase",
                    "memory_plugin",
                    depends_on=[previous_step_id],
                    inputs={"action": "set_phase", "phase": "qa"},
                )
            )

            evaluation = event.evaluation
            assert evaluation is not None
            if evaluation.target != "qa_answer":
                raise ValueError(
                    f"agent_plugin composer does not yet support target {evaluation.target!r}"
                )
            checkpoint_cases: list[str] = []
            for question_index, question in enumerate(evaluation.questions, start=1):
                evaluation_profile = evaluation.profile or scenario.evaluation.profile
                profile_handler = resolve_evaluation_profile(evaluation_profile)
                case_id = _question_case_id(
                    cases,
                    sample.sample_id,
                    event.event_id,
                    question.question_id,
                )
                agent_step_id = f"{case_id}-agent-answer"
                cases.append(
                    _question_case(
                        scenario,
                        sample.sample_id,
                        event.event_id,
                        question,
                        question_index,
                        setup_case_id,
                        agent_step_id,
                        integration=binding.memory_integration,
                        case_id=case_id,
                        judge_mode=profile_handler.judge_mode,
                        evaluation_target=evaluation.target,
                        evaluation_profile=evaluation_profile,
                        evaluation_at=event.timestamp,
                    )
                )
                checkpoint_cases.append(case_id)
                steps.append(
                    _step(
                        agent_step_id,
                        case_id,
                        "agent_plugin_answer",
                        "agent",
                        timeout_seconds=900,
                        inputs={
                            "system_prompt": (
                                "Answer the question using memory supplied by your context engine. "
                                "If memory is insufficient, abstain briefly."
                            ),
                            "messages": [{"role": "user", "content": f"Question: {question.question}"}],
                            "metadata": {
                                "sample_id": sample.sample_id,
                                "scope_id": scope_id,
                                "question_id": question.question_id,
                                "session_key": f"{binding.run_id}:qa-{sample.sample_id}-{question.question_id}",
                                **(
                                    {"agent_id": binding.agent_runtime_id}
                                    if binding.agent_runtime_id
                                    else {}
                                ),
                                "local": binding.agent_local,
                                "timeout_seconds": 900,
                                "purpose": "memory_qa",
                            },
                        },
                    )
                )
            previous_checkpoint_cases = checkpoint_cases or [setup_case_id]
    return cases, steps


def compose_run_plan(
    scenario: BenchmarkScenario,
    binding: RunBinding,
    runtime_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_capabilities = runtime_capabilities or {}
    if binding.memory_integration == "backend_direct":
        cases, steps = _compose_backend_direct(scenario, binding, runtime_capabilities)
    else:
        cases, steps = _compose_agent_plugin(scenario, binding, runtime_capabilities)
    return {
        "source_kind": "benchmark_scenario",
        "memory_integration": binding.memory_integration,
        "cases": cases,
        "steps": steps,
        "execution_spec": {
            "case_mode": "single_path",
            "max_parallel_steps": 1,
            "fail_fast": True,
            "default_retry_limit": 0,
            "default_timeout_seconds": 900 if binding.memory_integration == "agent_plugin" else 90,
            **scenario.execution_spec,
        },
    }
