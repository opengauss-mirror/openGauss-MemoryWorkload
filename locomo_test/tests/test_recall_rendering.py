from locomo_test.recall_rendering import (
    build_ingest_input_message,
    build_qa_input_message,
    format_ov_recall_evidence_block,
    rerank_ov_recalled_memories,
)


def test_format_ov_recall_evidence_block_prefers_summary_and_details():
    block = format_ov_recall_evidence_block(
        [
            {
                "uri": "viking://user/demo/memories/m1.md",
                "title": "Workshop",
                "summary": "Caroline attended an LGBTQ+ counseling workshop.",
                "content": "raw detail",
                "score": 0.9,
            }
        ],
        max_items=1,
        max_chars_per_item=200,
    )
    assert "Title: Workshop" in block
    assert "Summary: Caroline attended an LGBTQ+ counseling workshop." in block
    assert "Details: raw detail" in block


def test_build_qa_input_message_includes_evidence_and_current_date(monkeypatch):
    monkeypatch.delenv("LOCOMO_QA_PROMPT_PREFIX", raising=False)
    text = build_qa_input_message(
        question="What workshop did Caroline attend?",
        question_time="2023-06-27",
        recalled_memories=[{"summary": "Caroline attended an LGBTQ+ counseling workshop."}],
    )
    assert "Current date: 2023-06-27." in text
    assert "Retrieved memory evidence:" in text
    assert "Question: What workshop did Caroline attend?" in text


def test_build_ingest_input_message_prefixes_memory_ingest_prompt(monkeypatch):
    monkeypatch.delenv("LOCOMO_INGEST_PROMPT_PREFIX", raising=False)
    text = build_ingest_input_message("Caroline: hello")
    assert text.endswith("Caroline: hello")
    assert "memory-ingestion notes" in text


def test_rerank_ov_recalled_memories_prefers_named_person_entity():
    ranked = rerank_ov_recalled_memories(
        "What is Caroline's identity?",
        [
            {"uri": "viking://user/demo/memories/events/group.md", "summary": "Caroline visited a support group."},
            {
                "uri": "viking://user/demo/memories/entities/person/Caroline.md",
                "title": "Caroline",
                "summary": "Caroline spoke about her transgender journey.",
            },
        ],
    )
    assert ranked[0]["uri"].endswith("entities/person/Caroline.md")
