from pathlib import Path


SKILL = (
    Path(__file__).resolve().parents[1]
    / "skills/instrumentation/memory-system-auto-instrumentation/SKILL.md"
)
PROMPT = SKILL.parent / "prompts/meta-agent.md"
README = Path(__file__).resolve().parents[2] / "README.md"


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


def test_meta_agent_prompt_requires_analysis_before_edits():
    text = PROMPT.read_text(encoding="utf-8")
    for marker in (
        "TARGET_REPO",
        "ADD_ENTRYPOINT",
        "SEARCH_ENTRYPOINT",
        "REQUEST_ID_CONTRACT",
        "TRACE_ENABLEMENT",
        "TRACE_OUTPUT",
        "TEST_COMMANDS",
    ):
        assert marker in text
    assert text.index("call graph") < text.index("edit source")
    assert "covered" in text
    assert "absent" in text
    assert "missing_request_id" in text
    assert "unverified" in text


def test_readme_distinguishes_guidance_and_runtime_skills():
    text = README.read_text(encoding="utf-8")
    assert "skills/instrumentation/" in text
    assert "指导型 Skill" in text
    assert "不由 Integration Skill loader 加载" in text
