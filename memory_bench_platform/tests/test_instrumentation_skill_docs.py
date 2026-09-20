from pathlib import Path


SKILL = (
    Path(__file__).resolve().parents[1]
    / "skills/instrumentation/memory-system-auto-instrumentation/SKILL.md"
)


def test_auto_instrumentation_skill_pins_stage_and_safety_contract():
    text = SKILL.read_text(encoding="utf-8")
    for heading in (
        "## Discovery",
        "## Stage Map",
        "## Instrumentation Contract",
        "## Safety",
        "## Verification",
        "## Coverage Report",
    ):
        assert heading in text
    for stage in (
        "extract.preprocess",
        "extract.llm",
        "extract.vectorize",
        "extract.write",
        "retrieve.query_vectorize",
        "retrieve.vector_search",
        "retrieve.keyword_search",
        "retrieve.fusion",
        "retrieve.rerank",
        "retrieve.format",
    ):
        assert stage in text
    assert "non-overlapping leaf" in text
    assert "wrapper" in text
    assert "request_id" in text
    assert "default off" in text
    assert "re-raise" in text
    assert "Do not log" in text
    assert "absent" in text
    assert "backend_call" in text
