from __future__ import annotations

import re
from typing import Any

from .benchmark_scenario import BenchmarkScenario, RunBinding, ScenarioQuestion, TimelineEvent


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]+", "-", value).strip("-").lower() or "item"


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
) -> dict[str, Any]:
    return _case(
        f"{_slug(sample_id)}-{_slug(question.question_id)}",
        title=f"{scenario.benchmark_id} QA {sample_id} #{question_index}",
        goal="Answer the benchmark question using the configured memory runtime.",
        capability="memory/question-answering",
        depends_on_cases=[setup_case_id],
        reference={
            "question": question.question,
            "expected_answer": question.reference,
            "expected_step_id": expected_step_id,
            "category": question.category,
            "sample_id": sample_id,
            "checkpoint_id": checkpoint_id,
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
        judge_mode="external",
    )


def _compose_backend_direct(
    scenario: BenchmarkScenario,
    binding: RunBinding,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    for sample in scenario.samples:
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
            for timeline_event in pending_events:
                event_slug = _slug(timeline_event.event_id)
                ingest_step_id = f"{setup_case_id}-{event_slug}-ingest"
                flush_step_id = f"{setup_case_id}-{event_slug}-flush"
                wait_step_id = f"{setup_case_id}-{event_slug}-wait-ready"
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
                        },
                    )
                )
                steps.append(
                    _step(
                        flush_step_id,
                        setup_case_id,
                        f"flush_{timeline_event.event_id}",
                        "memory",
                        depends_on=[ingest_step_id],
                        timeout_seconds=30,
                        inputs={
                            "action": "flush",
                            "session_id": {
                                "$ref": f"steps.{ingest_step_id}.output.session_id"
                            },
                            "operation": {
                                "$ref": f"steps.{ingest_step_id}.output.operation"
                            },
                        },
                    )
                )
                steps.append(
                    _step(
                        wait_step_id,
                        setup_case_id,
                        f"wait_ready_{timeline_event.event_id}",
                        "poll",
                        depends_on=[flush_step_id],
                        timeout_seconds=600,
                        inputs={
                            "interval_seconds": 1,
                            "probe": {
                                "operator_kind": "memory",
                                "action": "status",
                                "inputs": {
                                    "operation": {
                                        "$ref": f"steps.{flush_step_id}.output.operation"
                                    }
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
                case_id = f"{_slug(sample.sample_id)}-{_slug(question.question_id)}"
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
                    )
                )
                checkpoint_cases.append(case_id)
                steps.append(
                    _step(
                        recall_step_id,
                        case_id,
                        "memory_recall",
                        "memory",
                        inputs={"action": "recall", "query": question.question, "node_limit": 10},
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    for sample in scenario.samples:
        pending_events: list[TimelineEvent] = []
        previous_checkpoint_cases: list[str] = []
        stage_index = 0
        initialized = False
        namespace = f"{binding.run_id}-{sample.namespace_hint or sample.sample_id}"
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
                        inputs={"action": "prepare", "namespace": namespace},
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
            for timeline_event in pending_events:
                event_slug = _slug(timeline_event.event_id)
                agent_step_id = f"{setup_case_id}-{event_slug}-agent-ingest"
                commit_step_id = f"{setup_case_id}-{event_slug}-commit"
                wait_step_id = f"{setup_case_id}-{event_slug}-wait-ready"
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
                steps.append(
                    _step(
                        commit_step_id,
                        setup_case_id,
                        f"commit_{timeline_event.event_id}",
                        "memory_plugin",
                        depends_on=[agent_step_id],
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
                steps.append(
                    _step(
                        wait_step_id,
                        setup_case_id,
                        f"wait_ready_{timeline_event.event_id}",
                        "memory_plugin",
                        depends_on=[commit_step_id],
                        timeout_seconds=660,
                        inputs={
                            "action": "wait_ready",
                            "operation": {"$ref": f"steps.{commit_step_id}.output.operation"},
                            "timeout_seconds": 600,
                            "grace_seconds": 0,
                            "interval_seconds": 2,
                        },
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
                case_id = f"{_slug(sample.sample_id)}-{_slug(question.question_id)}"
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


def compose_run_plan(scenario: BenchmarkScenario, binding: RunBinding) -> dict[str, Any]:
    if binding.memory_integration == "backend_direct":
        cases, steps = _compose_backend_direct(scenario, binding)
    else:
        cases, steps = _compose_agent_plugin(scenario, binding)
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
