from pathlib import Path

import pytest

from memory_bench_platform.protocol import (
    CaseRecord,
    ExecutionSpec,
    MemoryTaskInput,
    MemoryTaskOutput,
    StepRecord,
    StepResultRecord,
    WorkflowRuntimeContext,
)
from memory_bench_platform.workflow_inputs import (
    InputResolutionError,
    build_input_scope,
    resolve_inputs,
    validate_workflow,
)


def _runtime_context(tmp_path: Path) -> WorkflowRuntimeContext:
    return WorkflowRuntimeContext(
        run_id="run-1",
        run_dir=str(tmp_path),
        benchmark_id="benchmark-1",
        agent_id="agent-1",
        memory_id="memory-1",
        run_contract={"selection": {"memory_id": "memory-1"}},
        version_selection={"benchmark": {"selection_mode": "latest_official_release_tag"}},
    )


def _case(case_id: str = "case-1", **kwargs) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        run_id="run-1",
        title="case",
        goal="remember a fact",
        capability="memory/store-retrieve",
        **kwargs,
    )


def _step(step_id: str, case_id: str = "case-1", **kwargs) -> StepRecord:
    return StepRecord(
        step_id=step_id,
        case_id=case_id,
        name=step_id,
        operator_kind=kwargs.pop("operator_kind", "agent"),
        **kwargs,
    )


def test_validate_workflow_accepts_dependency_on_earlier_case():
    setup = _case("sample-setup")
    qa = _case("sample-q1", depends_on_cases=["sample-setup"])

    validate_workflow(
        cases=[setup, qa],
        steps=[],
        execution_spec=ExecutionSpec(),
        memory_id=None,
    )


def test_validate_workflow_rejects_unknown_case_dependency():
    qa = _case("sample-q1", depends_on_cases=["missing-setup"])

    with pytest.raises(ValueError, match="depends on unknown case"):
        validate_workflow(
            cases=[qa],
            steps=[],
            execution_spec=ExecutionSpec(),
            memory_id=None,
        )


def test_validate_workflow_rejects_future_case_dependency():
    qa = _case("sample-q1", depends_on_cases=["sample-setup"])
    setup = _case("sample-setup")

    with pytest.raises(ValueError, match="earlier case"):
        validate_workflow(
            cases=[qa, setup],
            steps=[],
            execution_spec=ExecutionSpec(),
            memory_id=None,
        )


def test_memory_protocol_models_preserve_structured_fields(tmp_path: Path):
    context = _runtime_context(tmp_path)
    request = MemoryTaskInput(
        task_id="ingest",
        action="ingest",
        inputs={"content": "private fact"},
        runtime_context=context,
        idempotency_key="run-1:case-1:ingest",
    )
    response = MemoryTaskOutput(
        status="ok",
        state="accepted",
        operation={"task_id": "task-1"},
        output={"resource_id": "resource-1"},
    )

    assert request.runtime_context.memory_id == "memory-1"
    assert response.operation["task_id"] == "task-1"
    assert response.metrics == []
    assert response.error == {}


def test_resolve_inputs_preserves_ref_types_and_nested_values(tmp_path: Path):
    prior = StepResultRecord(
        step_result_id="ingest-1",
        step_id="ingest",
        attempt=1,
        status="passed",
        structured_output={
            "output": {
                "operation": {"task_id": "task-1"},
                "count": 2,
                "ready": True,
            }
        },
    )
    scope = build_input_scope(_runtime_context(tmp_path), _case(), [prior])

    resolved = resolve_inputs(
        {
            "operation": {"$ref": "steps.ingest.output.operation"},
            "nested": [{"$ref": "steps.ingest.output.count"}],
            "ready": {"$ref": "steps.ingest.output.ready"},
        },
        scope,
    )

    assert resolved == {
        "operation": {"task_id": "task-1"},
        "nested": [2],
        "ready": True,
    }


def test_resolve_inputs_renders_scalar_templates(tmp_path: Path):
    prior = StepResultRecord(
        step_result_id="recall-1",
        step_id="recall",
        attempt=1,
        status="passed",
        structured_output={"output": {"evidence_text": "Go is preferred.", "count": 1}},
    )
    scope = build_input_scope(_runtime_context(tmp_path), _case(), [prior])

    resolved = resolve_inputs(
        {
            "question": {
                "$template": "Evidence ({{ steps.recall.output.count }}): {{ steps.recall.output.evidence_text }}"
            }
        },
        scope,
    )

    assert resolved["question"] == "Evidence (1): Go is preferred."


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"$ref": "env.API_KEY"}, "unsupported reference root"),
        ({"$ref": "steps.missing.output"}, "missing reference path"),
        ({"$template": "{{ steps.ingest.output.operation }}"}, "scalar values"),
    ],
)
def test_resolve_inputs_rejects_invalid_references(tmp_path: Path, value: dict, message: str):
    prior = StepResultRecord(
        step_result_id="ingest-1",
        step_id="ingest",
        attempt=1,
        status="passed",
        structured_output={"output": {"operation": {"task_id": "task-1"}}},
    )
    scope = build_input_scope(_runtime_context(tmp_path), _case(), [prior])

    with pytest.raises(InputResolutionError, match=message):
        resolve_inputs(value, scope)


def test_validate_workflow_accepts_prior_same_case_references():
    ingest = _step(
        "ingest",
        operator_kind="memory",
        inputs={"action": "ingest", "content": "fact"},
    )
    recall = _step(
        "recall",
        operator_kind="memory",
        depends_on=["ingest"],
        inputs={
            "action": "recall",
            "query": "fact",
            "operation": {"$ref": "steps.ingest.output.operation"},
        },
    )

    validate_workflow(
        cases=[_case()],
        steps=[ingest, recall],
        execution_spec=ExecutionSpec(case_mode="single_path"),
        memory_id="memory-1",
    )


@pytest.mark.parametrize(
    ("cases", "steps", "memory_id", "message"),
    [
        ([_case(), _case()], [], "memory-1", "duplicate case_id"),
        ([_case()], [_step("same"), _step("same")], "memory-1", "duplicate step_id"),
        ([_case()], [_step("orphan", case_id="other")], "memory-1", "unknown case_id"),
        (
            [_case()],
            [_step("second", depends_on=["future"]), _step("future")],
            "memory-1",
            "must reference an earlier step",
        ),
        (
            [_case()],
            [
                _step("first", inputs={"value": {"$ref": "steps.future.output.value"}}),
                _step("future"),
            ],
            "memory-1",
            "future step",
        ),
        (
            [_case("case-1"), _case("case-2")],
            [
                _step("first", case_id="case-1"),
                _step(
                    "second",
                    case_id="case-2",
                    inputs={"value": {"$ref": "steps.first.output.value"}},
                ),
            ],
            "memory-1",
            "cross-case",
        ),
        (
            [_case()],
            [
                _step(
                    "ingest",
                    operator_kind="memory",
                    retry_limit=1,
                    inputs={"action": "ingest", "content": "fact"},
                )
            ],
            "memory-1",
            "memory.ingest cannot retry",
        ),
        (
            [_case()],
            [_step("recall", operator_kind="memory", inputs={"action": "recall", "query": "fact"})],
            None,
            "requires memory_id",
        ),
    ],
)
def test_validate_workflow_rejects_invalid_shapes(
    cases: list[CaseRecord],
    steps: list[StepRecord],
    memory_id: str | None,
    message: str,
):
    with pytest.raises(ValueError, match=message):
        validate_workflow(
            cases=cases,
            steps=steps,
            execution_spec=ExecutionSpec(case_mode="single_path"),
            memory_id=memory_id,
        )


@pytest.mark.parametrize(
    ("poll_inputs", "message"),
    [
        (
            {
                "interval_seconds": -1,
                "probe": {"operator_kind": "memory", "action": "status", "inputs": {}},
                "success_when": {"path": "state", "equals": "completed"},
            },
            "interval_seconds",
        ),
        (
            {
                "probe": {"operator_kind": "memory", "action": "status", "inputs": {}},
            },
            "success_when",
        ),
        (
            {
                "probe": {"operator_kind": "memory", "action": "status", "inputs": {}},
                "success_when": {"path": "state"},
            },
            "requires equals or in",
        ),
        (
            {
                "probe": {"operator_kind": "memory", "action": "ingest", "inputs": {}},
                "success_when": {"path": "state", "equals": "completed"},
            },
            "read-only",
        ),
        (
            {
                "probe": {"operator_kind": "http", "method": "POST", "inputs": {}},
                "success_when": {"path": "http_status", "equals": 200},
            },
            "GET or HEAD",
        ),
        (
            {
                "probe": {"operator_kind": "agent", "inputs": {}},
                "success_when": {"path": "status", "equals": "ok"},
            },
            "not allowed",
        ),
    ],
)
def test_validate_workflow_rejects_unsafe_poll_configuration(poll_inputs: dict, message: str):
    poll = _step(
        "poll",
        operator_kind="poll",
        timeout_seconds=5,
        inputs=poll_inputs,
    )

    with pytest.raises(ValueError, match=message):
        validate_workflow(
            cases=[_case()],
            steps=[poll],
            execution_spec=ExecutionSpec(case_mode="single_path"),
            memory_id="memory-1",
        )
