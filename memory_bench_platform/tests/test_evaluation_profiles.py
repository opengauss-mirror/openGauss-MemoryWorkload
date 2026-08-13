from __future__ import annotations

import pytest

from memory_bench_platform.evaluation_profiles import resolve_evaluation_profile
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
