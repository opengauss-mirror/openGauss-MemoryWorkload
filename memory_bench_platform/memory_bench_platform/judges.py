from __future__ import annotations

import re

from .protocol import JudgeInput, JudgeResult


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def run_builtin_judge(run_id: str, judge_input: JudgeInput) -> JudgeResult:
    expected = _normalize(str(judge_input.reference.get("expected_answer", "")))
    expected_step_id = str(judge_input.reference.get("expected_step_id", "") or "")
    actual = ""
    step_results = judge_input.step_results
    if expected_step_id:
        prioritized = [item for item in step_results if item.get("step_id") == expected_step_id]
        remaining = [item for item in step_results if item.get("step_id") != expected_step_id]
        step_results = prioritized + remaining

    for result in step_results:
        structured = result.get("structured_output", {})
        candidate = (
            structured.get("agent_answer")
            or structured.get("text_output")
            or structured.get("stdout_text")
        )
        if candidate:
            actual = _normalize(str(candidate))
            break

    passed = False
    label = "missing-answer"
    rationale = "No agent answer extracted from step results."
    score = 0.0

    if actual:
        if actual == expected:
            passed = True
            label = "exact-match"
            rationale = "Agent answer matches expected answer after normalization."
            score = 1.0
        elif expected and expected in actual:
            passed = True
            label = "contains-match"
            rationale = "Agent answer contains expected answer after normalization."
            score = 1.0
        else:
            passed = False
            label = "mismatch"
            rationale = "Agent answer does not match expected answer."
            score = 0.0

    return JudgeResult(
        judge_id=f"{judge_input.case_id}-builtin",
        run_id=run_id,
        case_id=judge_input.case_id,
        score=score,
        label=label,
        passed=passed,
        rationale=rationale,
    )
