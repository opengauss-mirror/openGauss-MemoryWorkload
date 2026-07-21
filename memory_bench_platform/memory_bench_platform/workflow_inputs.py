from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Iterable

from .protocol import (
    CaseRecord,
    ExecutionSpec,
    StepRecord,
    StepResultRecord,
    WorkflowRuntimeContext,
)


_TEMPLATE_PATTERN = re.compile(r"{{\s*([^{}]+?)\s*}}")
_ALLOWED_ROOTS = {"run", "case", "steps"}
_POLL_MEMORY_ACTIONS = {"status", "recall", "consistency"}


class InputResolutionError(ValueError):
    pass


def build_input_scope(
    runtime_context: WorkflowRuntimeContext,
    case: CaseRecord,
    step_results: Iterable[StepResultRecord],
) -> dict[str, Any]:
    return {
        "run": runtime_context.model_dump(mode="json"),
        "case": case.model_dump(mode="json"),
        "steps": {
            result.step_id: deepcopy(result.structured_output)
            for result in step_results
        },
    }


def resolve_inputs(value: Any, scope: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [resolve_inputs(item, scope) for item in value]
    if not isinstance(value, dict):
        return value

    if "$ref" in value:
        if set(value) != {"$ref"} or not isinstance(value["$ref"], str):
            raise InputResolutionError("$ref must be the only key and contain a string path")
        return deepcopy(_resolve_path(value["$ref"], scope))

    if "$template" in value:
        if set(value) != {"$template"} or not isinstance(value["$template"], str):
            raise InputResolutionError("$template must be the only key and contain a string")
        return _render_template(value["$template"], scope)

    return {key: resolve_inputs(item, scope) for key, item in value.items()}


def validate_workflow(
    *,
    cases: list[CaseRecord],
    steps: list[StepRecord],
    execution_spec: ExecutionSpec,
    memory_id: str | None,
) -> None:
    case_ids = [case.case_id for case in cases]
    duplicate_case_ids = _duplicates(case_ids)
    if duplicate_case_ids:
        raise ValueError(f"duplicate case_id: {duplicate_case_ids[0]}")

    step_ids = [step.step_id for step in steps]
    duplicate_step_ids = _duplicates(step_ids)
    if duplicate_step_ids:
        raise ValueError(f"duplicate step_id: {duplicate_step_ids[0]}")

    case_id_set = set(case_ids)
    case_positions = {case.case_id: index for index, case in enumerate(cases)}
    for index, case in enumerate(cases):
        for dependency in case.depends_on_cases:
            if dependency not in case_id_set:
                raise ValueError(f"case {case.case_id} depends on unknown case: {dependency}")
            if case_positions[dependency] >= index:
                raise ValueError(
                    f"case {case.case_id} dependency must reference an earlier case: {dependency}"
                )

    step_positions = {step.step_id: index for index, step in enumerate(steps)}
    steps_by_id = {step.step_id: step for step in steps}
    uses_typed_runtime = False

    for index, step in enumerate(steps):
        if step.case_id not in case_id_set:
            raise ValueError(f"step {step.step_id} has unknown case_id: {step.case_id}")

        for dependency in step.depends_on:
            dependency_step = steps_by_id.get(dependency)
            if dependency_step is None:
                raise ValueError(f"step {step.step_id} depends on unknown step: {dependency}")
            if dependency_step.case_id != step.case_id:
                raise ValueError(f"step {step.step_id} dependency must stay in the same case: {dependency}")
            if step_positions[dependency] >= index:
                raise ValueError(f"step {step.step_id} dependency must reference an earlier step: {dependency}")

        for reference in iter_input_references(step.inputs):
            _validate_reference_order(reference, step, index, steps_by_id, step_positions)

        if step.operator_kind == "memory":
            uses_typed_runtime = True
            if not memory_id:
                raise ValueError(f"memory step {step.step_id} requires memory_id")
            action = str(step.inputs.get("action", "") or "")
            if action == "ingest" and step.retry_limit > 0:
                raise ValueError("memory.ingest cannot retry")
        elif step.operator_kind == "poll":
            uses_typed_runtime = True
            _validate_poll_step(step, execution_spec, memory_id)

    if uses_typed_runtime and execution_spec.case_mode != "single_path":
        raise ValueError("memory and poll operators require execution_spec.case_mode=single_path")


def iter_input_references(value: Any) -> Iterable[str]:
    if isinstance(value, list):
        for item in value:
            yield from iter_input_references(item)
        return
    if not isinstance(value, dict):
        return
    if set(value) == {"$ref"} and isinstance(value["$ref"], str):
        yield value["$ref"]
        return
    if set(value) == {"$template"} and isinstance(value["$template"], str):
        yield from (match.group(1).strip() for match in _TEMPLATE_PATTERN.finditer(value["$template"]))
        return
    for item in value.values():
        yield from iter_input_references(item)


def _render_template(template: str, scope: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        resolved = _resolve_path(match.group(1).strip(), scope)
        if isinstance(resolved, (dict, list)):
            raise InputResolutionError("$template placeholders only accept scalar values")
        return "" if resolved is None else str(resolved)

    return _TEMPLATE_PATTERN.sub(replace, template)


def _resolve_path(path: str, scope: dict[str, Any]) -> Any:
    parts = [part for part in path.split(".") if part]
    if not parts or parts[0] not in _ALLOWED_ROOTS:
        raise InputResolutionError(f"unsupported reference root in path: {path}")

    current: Any = scope
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        raise InputResolutionError(f"missing reference path: {path}")
    return current


def _validate_reference_order(
    reference: str,
    step: StepRecord,
    current_index: int,
    steps_by_id: dict[str, StepRecord],
    step_positions: dict[str, int],
) -> None:
    parts = reference.split(".")
    if not parts or parts[0] not in _ALLOWED_ROOTS:
        raise ValueError(f"unsupported reference root in path: {reference}")
    if parts[0] != "steps":
        return
    if len(parts) < 2 or not parts[1]:
        raise ValueError(f"invalid step reference: {reference}")

    referenced_step = steps_by_id.get(parts[1])
    if referenced_step is None:
        raise ValueError(f"reference points to unknown step: {parts[1]}")
    if referenced_step.case_id != step.case_id:
        raise ValueError(f"cross-case step reference is not allowed: {reference}")
    if step_positions[referenced_step.step_id] >= current_index:
        raise ValueError(f"reference points to future step: {reference}")


def _validate_poll_step(
    step: StepRecord,
    execution_spec: ExecutionSpec,
    memory_id: str | None,
) -> None:
    effective_timeout = step.timeout_seconds or execution_spec.default_timeout_seconds
    if effective_timeout is None or effective_timeout <= 0:
        raise ValueError(f"poll step {step.step_id} requires a positive timeout")

    try:
        interval = float(step.inputs.get("interval_seconds", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"poll step {step.step_id} interval_seconds must be numeric") from exc
    if interval < 0:
        raise ValueError(f"poll step {step.step_id} interval_seconds must be non-negative")

    if "success_when" not in step.inputs:
        raise ValueError(f"poll step {step.step_id} requires success_when")
    _validate_poll_condition(step.inputs["success_when"], "success_when")
    if "failure_when" in step.inputs:
        _validate_poll_condition(step.inputs["failure_when"], "failure_when")

    probe = step.inputs.get("probe")
    if not isinstance(probe, dict):
        raise ValueError(f"poll step {step.step_id} requires a probe object")
    operator_kind = str(probe.get("operator_kind", "") or "")
    probe_inputs = probe.get("inputs", {})
    if not isinstance(probe_inputs, dict):
        raise ValueError(f"poll step {step.step_id} probe.inputs must be an object")

    if operator_kind == "memory":
        if not memory_id:
            raise ValueError(f"poll memory probe {step.step_id} requires memory_id")
        action = str(probe.get("action") or probe_inputs.get("action") or "")
        if action not in _POLL_MEMORY_ACTIONS:
            raise ValueError(f"poll memory probe action must be read-only: {action or '<missing>'}")
        return

    if operator_kind == "http":
        method = str(probe.get("method") or probe_inputs.get("method") or "GET").upper()
        if method not in {"GET", "HEAD"}:
            raise ValueError(f"poll HTTP probe method must be GET or HEAD: {method}")
        return

    raise ValueError(f"poll probe operator is not allowed: {operator_kind or '<missing>'}")


def _validate_poll_condition(condition: Any, name: str) -> None:
    if not isinstance(condition, dict) or not str(condition.get("path", "") or ""):
        raise ValueError(f"poll {name} requires a path")
    has_equals = "equals" in condition
    has_in = isinstance(condition.get("in"), list)
    if has_equals == has_in:
        raise ValueError(f"poll {name} requires equals or in")


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates
