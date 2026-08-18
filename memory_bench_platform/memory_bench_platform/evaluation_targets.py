from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvaluationTargetContract:
    target: str
    supported_integrations: frozenset[str]
    required_memory_actions: frozenset[str] = frozenset()
    extractor: str | None = None

    def supports(self, memory_integration: str) -> bool:
        return memory_integration in self.supported_integrations


_TARGET_CONTRACTS = {
    "qa_answer": EvaluationTargetContract(
        target="qa_answer",
        supported_integrations=frozenset({"backend_direct", "agent_plugin"}),
        required_memory_actions=frozenset({"recall"}),
        extractor="qa_answer",
    ),
    "retrieval": EvaluationTargetContract(
        target="retrieval",
        supported_integrations=frozenset({"backend_direct"}),
        required_memory_actions=frozenset({"recall"}),
        extractor="evidence_text",
    ),
    # These targets are part of the Scenario vocabulary, but the current
    # Composer has no executable plan and scorer contract for them yet.
    "memory_extraction": EvaluationTargetContract(
        target="memory_extraction",
        supported_integrations=frozenset(),
        required_memory_actions=frozenset({"inspect_memory"}),
    ),
    "memory_update": EvaluationTargetContract(
        target="memory_update",
        supported_integrations=frozenset(),
        required_memory_actions=frozenset({"inspect_memory"}),
    ),
    "agent_action": EvaluationTargetContract(
        target="agent_action",
        supported_integrations=frozenset(),
    ),
}


def resolve_evaluation_target(target: str) -> EvaluationTargetContract:
    try:
        return _TARGET_CONTRACTS[target]
    except KeyError as exc:
        supported = ", ".join(sorted(_TARGET_CONTRACTS))
        raise ValueError(
            f"unsupported evaluation target {target!r}; known targets: {supported}"
        ) from exc


def unsupported_evaluation_targets(
    targets: set[str], memory_integration: str
) -> set[str]:
    return {
        target
        for target in targets
        if not resolve_evaluation_target(target).supports(memory_integration)
    }


def required_memory_actions_for_targets(
    targets: set[str], memory_integration: str
) -> set[str]:
    actions: set[str] = set()
    for target in targets:
        contract = resolve_evaluation_target(target)
        if contract.supports(memory_integration):
            actions.update(contract.required_memory_actions)
    return actions
