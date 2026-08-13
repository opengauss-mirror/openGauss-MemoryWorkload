from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvaluationProfileHandler:
    profile: str
    judge_mode: str
    builtin_strategy: str | None = None


_PROFILE_HANDLERS = {
    "exact_match@1": EvaluationProfileHandler(
        profile="exact_match@1",
        judge_mode="builtin",
        builtin_strategy="exact_match",
    ),
    "classification@1": EvaluationProfileHandler(
        profile="classification@1",
        judge_mode="builtin",
        builtin_strategy="classification",
    ),
    "retrieval@1": EvaluationProfileHandler(
        profile="retrieval@1",
        judge_mode="builtin",
        builtin_strategy="contains_match",
    ),
    "llm_judge@1": EvaluationProfileHandler(
        profile="llm_judge@1",
        judge_mode="external",
    ),
}


def resolve_evaluation_profile(profile: str | None) -> EvaluationProfileHandler:
    """Resolve a declared Scenario profile without silently changing its semantics."""
    if profile is None or not str(profile).strip():
        # Backward compatibility for hand-written native workflows that predate profiles.
        return EvaluationProfileHandler(
            profile="legacy_builtin@1",
            judge_mode="builtin",
            builtin_strategy="contains_match",
        )
    normalized = str(profile).strip().lower()
    try:
        return _PROFILE_HANDLERS[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(_PROFILE_HANDLERS))
        raise ValueError(
            f"unsupported evaluation profile {profile!r}; supported profiles: {supported}"
        ) from exc


def metric_envelope(
    *,
    primary_metric: str,
    metrics: list[dict[str, Any]],
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the common scorer envelope used by benchmark adapters."""
    return {
        "primary_metric": primary_metric,
        "metrics": metrics,
        "artifacts": artifacts or [],
    }
