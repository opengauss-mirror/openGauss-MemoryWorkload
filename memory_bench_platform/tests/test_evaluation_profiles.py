from __future__ import annotations

import pytest

from memory_bench_platform.evaluation_profiles import (
    EvaluationProfileHandler,
    register_evaluation_profile,
    resolve_evaluation_profile,
    resolve_evaluation_governance,
)
from memory_bench_platform.benchmark_scenario import BenchmarkScenario
from memory_bench_platform.integration import get_benchmark_manifest
from memory_bench_platform.judges import run_builtin_judge
from memory_bench_platform.protocol import JudgeInput


def _input(
    profile: str, expected: str, actual: str, *, extractor: str = "qa_answer"
) -> JudgeInput:
    return JudgeInput(
        case_id="case-1",
        reference={
            "expected_answer": expected,
            "expected_step_id": "answer-step",
            "evaluation_profile": profile,
            "evaluation_extractor": extractor,
        },
        step_results=[
            {
                "step_id": "answer-step",
                "status": "passed",
                "structured_output": (
                    {"output": {"evidence_text": actual}}
                    if extractor == "evidence_text"
                    else {"agent_answer": actual}
                ),
            }
        ],
    )


def test_exact_match_does_not_accept_contains_match():
    result = run_builtin_judge(
        "run-1", _input("exact_match@1", "tea", "The user prefers tea")
    )
    assert result.passed is False


def test_retrieval_profile_accepts_expected_evidence_in_larger_text():
    result = run_builtin_judge(
        "run-1",
        _input(
            "retrieval@1",
            "prefers tea",
            "Memory: the user prefers tea.",
            extractor="evidence_text",
        ),
    )
    assert result.passed is True
    assert result.label == "contains-match"


def test_classification_requires_normalized_label_equality():
    assert run_builtin_judge(
        "run-1", _input("classification@1", "Positive", " positive ")
    ).passed is True
    assert run_builtin_judge(
        "run-1", _input("classification@1", "Positive", "likely positive")
    ).passed is False


def test_external_profile_cannot_run_through_builtin_judge():
    with pytest.raises(ValueError, match="requires an external judge"):
        run_builtin_judge("run-1", _input("llm_judge@1", "tea", "tea"))


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="unsupported evaluation profile"):
        resolve_evaluation_profile("custom@99")


def test_profile_registry_accepts_versioned_custom_handler():
    register_evaluation_profile(
        EvaluationProfileHandler(
            profile="demo_semantic@1", judge_mode="external"
        )
    )
    assert resolve_evaluation_profile("demo_semantic@1").judge_mode == "external"


def test_official_benchmark_profile_rejects_silent_scenario_override():
    manifest = get_benchmark_manifest("locomo")
    scenario = BenchmarkScenario.model_validate(
        {
            "benchmark_id": "locomo",
            "evaluation": {"target": "qa_answer", "profile": "exact_match@1"},
            "samples": [
                {
                    "sample_id": "sample-1",
                    "timeline": [
                        {
                            "event_id": "checkpoint-1",
                            "type": "checkpoint",
                            "evaluation": {
                                "target": "qa_answer",
                                "profile": "exact_match@1",
                                "questions": [],
                            },
                        }
                    ],
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="official benchmark profile"):
        resolve_evaluation_governance(manifest, scenario)

    scenario.metadata["evaluation_override"] = {
        "enabled": True,
        "reason": "ablation",
    }
    governance = resolve_evaluation_governance(manifest, scenario)
    assert governance["official"] is False
    assert governance["override_reason"] == "ablation"
    assert governance["judge_prompt_profile"] == "locomo_qa@1"
