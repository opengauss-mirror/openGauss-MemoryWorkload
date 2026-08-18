from __future__ import annotations

from .protocol import JudgeInput


def extract_observation(judge_input: JudgeInput) -> str:
    """Extract the observation declared by the Benchmark evaluation contract."""
    extractor = str(judge_input.reference.get("evaluation_extractor") or "qa_answer")
    if extractor not in {"qa_answer", "evidence_text"}:
        raise ValueError(f"unknown evaluation extractor: {extractor}")

    expected_step_id = str(judge_input.reference.get("expected_step_id", "") or "")
    results = list(judge_input.step_results)
    if expected_step_id:
        results.sort(key=lambda item: item.get("step_id") != expected_step_id)

    for result in results:
        structured = result.get("structured_output", {})
        if not isinstance(structured, dict):
            continue
        if extractor == "qa_answer":
            candidate = (
                structured.get("agent_answer")
                or structured.get("text_output")
                or structured.get("stdout_text")
            )
        else:
            output = structured.get("output", {})
            candidate = output.get("evidence_text") if isinstance(output, dict) else None
            candidate = candidate or structured.get("evidence_text")
        if candidate:
            return str(candidate).strip()
    return ""
