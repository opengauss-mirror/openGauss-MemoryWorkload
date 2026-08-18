from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .benchmark_scenario import BenchmarkScenario
from .manifests import BenchmarkManifest


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


def register_evaluation_profile(handler: EvaluationProfileHandler) -> None:
    """Register an explicit profile handler; duplicate names are rejected."""
    normalized = handler.profile.strip().lower()
    if not normalized or normalized in _PROFILE_HANDLERS:
        raise ValueError(f"evaluation profile already registered or invalid: {handler.profile!r}")
    _PROFILE_HANDLERS[normalized] = handler


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


def resolve_evaluation_governance(
    manifest: BenchmarkManifest,
    scenario: BenchmarkScenario,
) -> dict[str, Any]:
    official = dict(manifest.evaluation or {})
    official_profile = str(official.get("profile") or "").strip() or None
    official_target = str(official.get("target") or "").strip() or None
    actual_rows = []
    mismatches = []
    for sample in scenario.samples:
        for event in sample.timeline:
            if event.evaluation is None:
                continue
            actual_profile = event.evaluation.profile or scenario.evaluation.profile
            actual_target = event.evaluation.target or scenario.evaluation.target
            row = {
                "sample_id": sample.sample_id,
                "checkpoint_id": event.event_id,
                "profile": actual_profile,
                "target": actual_target,
            }
            actual_rows.append(row)
            if official_profile and actual_profile != official_profile:
                mismatches.append({**row, "field": "profile", "official": official_profile})
            if official_target and actual_target != official_target:
                mismatches.append({**row, "field": "target", "official": official_target})

    override = scenario.metadata.get("evaluation_override", {})
    override = override if isinstance(override, dict) else {}
    reason = str(override.get("reason") or "").strip()
    allowed = bool(override.get("enabled")) and bool(reason)
    if mismatches and not allowed:
        raise ValueError(
            "scenario evaluation differs from the official benchmark profile; "
            "declare metadata.evaluation_override.enabled=true and a non-empty reason "
            "to produce an unofficial result"
        )
    return {
        "official": not bool(mismatches),
        "official_evaluation": official,
        "judge_prompt_profile": str(manifest.judging.get("profile") or "") or None,
        "actual_checkpoints": actual_rows,
        "overrides": mismatches,
        "override_reason": reason if mismatches else None,
    }


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
