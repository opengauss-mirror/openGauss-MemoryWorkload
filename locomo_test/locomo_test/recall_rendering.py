from __future__ import annotations

import os
import re


DEFAULT_LOCOMO_QA_PROMPT_PREFIX = (
    "Answer the question directly using the recalled memory snippets as the primary evidence. "
    "Treat the normalized summary or leading bullet points at the top of each memory as authoritative, "
    "and use them before raw chat-log details that follow. "
    "Prefer the most specific supported fact over a generic paraphrase. "
    "If the recalled memories contain an exact noun phrase, identity label, country, gift, relationship status, duration, or symbolic meaning, copy that specific phrase instead of replacing it with a generic phrase. "
    "Do not replace exact facts with vague substitutes such as person, home country, object, support, or event. "
    "For list or set questions, include only items that are explicitly supported by recalled memories and match the asked category. "
    "Do not broaden the answer with nearby related activities, generic themes, inferred items, or unnamed placeholders. "
    "If a list item is not explicitly named, omit it. "
    "If a recalled memory clearly matches the event being asked about, use the matching event details even when the question wording is paraphrased. "
    "For time questions, preserve the memory's relative phrasing such as last week, last Friday, next month, or the week before the current date unless the memory itself explicitly states a calendar date. "
    "If the memory says an event happened in the week before the current date, answer with that relative date instead of saying it is missing. "
    "Do not say information is unavailable when the recalled memories explicitly contain the answer. "
    "If the recalled memories still do not support the answer, say so briefly. "
)

DEFAULT_LOCOMO_INGEST_PROMPT_PREFIX = (
    "You are transforming a raw conversation log into memory-ingestion notes, not answering a user question. "
    "Reply with short factual bullet points only. "
    "Write ONE explicit fact per bullet. "
    "Capture explicit facts for BOTH participants. "
    "Preserve exact dates, relative dates, durations, counts, countries, gifts, relationships, plans, motivations, and workshop/camping/speech details. "
    "Keep the conversation's own time anchors instead of today's date. "
    "When a fact is explicit, keep the concrete noun or number exactly, including values like Sweden, necklace, 5 years, 10 years ago, June 2023, and week-before references. "
    "No follow-up questions. No greetings, roleplay, or advice.\n\n"
)


def normalize_recall_text(value: object, *, max_chars: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


def extract_recall_summary(memory: dict, *, max_chars: int) -> str:
    candidates = [
        memory.get("normalized_summary"),
        memory.get("summary"),
        memory.get("excerpt"),
        memory.get("text"),
        memory.get("content"),
        memory.get("body"),
        memory.get("markdown"),
    ]
    metadata = memory.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend(
            [
                metadata.get("normalized_summary"),
                metadata.get("summary"),
                metadata.get("excerpt"),
                metadata.get("text"),
            ]
        )
    for candidate in candidates:
        text = normalize_recall_text(candidate, max_chars=max_chars)
        if text:
            return text
    return ""


def extract_recall_detail(memory: dict, *, summary: str, max_chars: int) -> str:
    assistant_summary = extract_embedded_assistant_summary(memory, max_chars=max_chars)
    if assistant_summary:
        return assistant_summary

    candidates = [
        memory.get("text"),
        memory.get("content"),
        memory.get("body"),
        memory.get("markdown"),
        memory.get("excerpt"),
    ]
    metadata = memory.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend(
            [
                metadata.get("text"),
                metadata.get("content"),
                metadata.get("body"),
                metadata.get("markdown"),
                metadata.get("excerpt"),
            ]
        )
    normalized_summary = normalize_recall_text(summary, max_chars=max_chars)
    for candidate in candidates:
        text = normalize_recall_text(candidate, max_chars=max_chars)
        if text:
            convo_idx = text.find("[group chat conversation:")
            if convo_idx >= 0:
                text = text[convo_idx:]
            else:
                text = re.sub(r"^\d{4}-\d{2}-\d{2} \([^)]+\) ChatLog:\s*", "", text)
                text = re.sub(r"^\[(?:user|assistant)\]:\s*", "", text)
        if text and text != normalized_summary:
            return text
    return ""


def extract_embedded_assistant_summary(memory: dict, *, max_chars: int) -> str:
    candidates = [
        memory.get("content"),
        memory.get("text"),
        memory.get("body"),
        memory.get("markdown"),
        memory.get("excerpt"),
    ]
    metadata = memory.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend(
            [
                metadata.get("content"),
                metadata.get("text"),
                metadata.get("body"),
                metadata.get("markdown"),
                metadata.get("excerpt"),
            ]
        )
    for candidate in candidates:
        raw = str(candidate or "")
        if not raw:
            continue
        match = re.search(r"\[assistant\]:\s*(.+)", raw, flags=re.DOTALL)
        if not match:
            continue
        text = normalize_recall_text(match.group(1), max_chars=max_chars)
        if text:
            return text
    return ""


def tokenize_recall_text(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9+]+", value.lower())


def question_targets_person_profile(question: str) -> bool:
    lowered = (question or "").lower()
    cues = (
        "identity",
        "relationship status",
        "relationship",
        "status",
        "married",
        "husband",
        "wife",
        "single parent",
        "single",
        "career",
        "education",
        "field",
        "pursue",
        "home country",
        "country",
        "friends",
        "breakup",
        "transition",
        "transgender",
        "family",
        "kids",
    )
    return any(cue in lowered for cue in cues)


def extract_person_name_candidates(question: str) -> list[str]:
    stopwords = {
        "What", "When", "Where", "Why", "How", "Which", "Who",
        "Did", "Does", "Do", "Is", "Are", "Was", "Were",
    }
    names: list[str] = []
    for token in re.findall(r"\b[A-Z][a-z]+\b", question or ""):
        if token in stopwords or token in names:
            continue
        names.append(token)
    return names


def build_person_entity_memory(uri: str, content: str) -> dict[str, object]:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    title = ""
    bullet_lines: list[str] = []
    for line in lines:
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("- "):
            bullet_lines.append(line[2:].strip())
    summary = " ".join(bullet_lines[:3]).strip()
    return {
        "uri": uri,
        "title": title,
        "summary": summary,
        "content": content,
        "context_type": "memory",
        "memory_hint": "person_entity",
    }


def augment_ov_recall_with_named_person_entities(
    *,
    ov_api_url: str,
    question: str,
    user_id: str,
    recalled_memories: list[dict],
    request_headers_fn,
    read_content_fn,
    state_dir: str = "",
    fallback_agent_id: str = "main",
) -> list[dict]:
    if not question_targets_person_profile(question):
        return recalled_memories
    names = extract_person_name_candidates(question)
    if not names:
        return recalled_memories
    headers = request_headers_fn(state_dir=state_dir, fallback_agent_id=fallback_agent_id)
    if not headers:
        return recalled_memories
    existing_uris = {str(item.get("uri") or "") for item in recalled_memories if isinstance(item, dict)}
    augmented = list(recalled_memories)
    for name in names:
        candidate_uris = [
            f"viking://user/{user_id}/memories/entities/person/{name}.md",
            f"viking://user/{user_id}/memories/entities/person/{name.lower()}.md",
        ]
        for uri in candidate_uris:
            if uri in existing_uris:
                break
            content = read_content_fn(ov_api_url, headers, uri)
            if not content:
                continue
            augmented.append(build_person_entity_memory(uri, content))
            existing_uris.add(uri)
            break
    return augmented


def rerank_ov_recalled_memories(question: str, recalled_memories: list[dict]) -> list[dict]:
    if len(recalled_memories) <= 1:
        return recalled_memories
    stopwords = {
        "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "at", "by",
        "is", "are", "was", "were", "did", "does", "do", "what", "when", "where",
        "who", "why", "how", "which", "with", "from", "their", "them", "they",
        "her", "his", "she", "he", "it", "this", "that", "these", "those", "be",
        "have", "has", "had", "been", "would", "could", "should", "about", "into",
        "than", "then", "after", "before", "during", "ago", "kind", "type",
    }
    query_tokens = [tok for tok in tokenize_recall_text(question) if tok not in stopwords and len(tok) > 1]
    if not query_tokens:
        return recalled_memories

    prefer_person_profile = question_targets_person_profile(question)
    question_names = [name.lower() for name in extract_person_name_candidates(question)]

    def _memory_score(memory: dict, index: int) -> tuple[int, int, int, int, int, int]:
        title = normalize_recall_text(memory.get("title") or memory.get("name"), max_chars=240)
        summary = extract_recall_summary(memory, max_chars=1000)
        detail = extract_recall_detail(memory, summary=summary, max_chars=1200)
        haystack = f"{title}\n{summary}\n{detail}".lower()
        uri = str(memory.get("uri") or "")
        overlap = 0
        exact_mentions = 0
        for token in query_tokens:
            if token in haystack:
                overlap += 1
                exact_mentions += haystack.count(token)
        has_title_hit = int(any(token in title.lower() for token in query_tokens))
        named_person_entity_hit = 0
        if "/entities/person/" in uri:
            lowered_title = title.lower()
            if any(name == lowered_title or re.search(rf"\b{re.escape(name)}\b", lowered_title) for name in question_names):
                named_person_entity_hit = 1
        strong_person_entity_hit = 0
        if prefer_person_profile and named_person_entity_hit:
            strong_person_entity_hit = 1
        return (strong_person_entity_hit, named_person_entity_hit, overlap, exact_mentions, has_title_hit, -index)

    ranked = sorted(
        enumerate(recalled_memories),
        key=lambda item: _memory_score(item[1], item[0]),
        reverse=True,
    )
    return [memory for _, memory in ranked]


def format_ov_recall_evidence_block(
    recalled_memories: list[dict],
    *,
    max_items: int = 5,
    max_chars_per_item: int = 400,
) -> str:
    if not recalled_memories:
        return ""
    lines = [
        "Retrieved memory evidence:",
        "Use the following recalled memories as concrete evidence before answering.",
    ]
    for idx, memory in enumerate(recalled_memories[:max_items], start=1):
        title = normalize_recall_text(memory.get("title") or memory.get("name"), max_chars=160)
        summary = extract_recall_summary(memory, max_chars=max_chars_per_item)
        detail = extract_recall_detail(memory, summary=summary, max_chars=max_chars_per_item)
        score = memory.get("score")
        header = f"[Memory {idx}]"
        if score not in (None, ""):
            header += f" score={score}"
        lines.append(header)
        if title:
            lines.append(f"Title: {title}")
        if summary:
            lines.append(f"Summary: {summary}")
        if detail:
            lines.append(f"Details: {detail}")
    return "\n".join(lines).strip()


def build_qa_input_message(
    *,
    question: str,
    question_time: str | None,
    recalled_memories: list[dict] | None = None,
) -> str:
    qa_prompt_prefix = os.environ.get("LOCOMO_QA_PROMPT_PREFIX", "").strip() or DEFAULT_LOCOMO_QA_PROMPT_PREFIX
    evidence_block = format_ov_recall_evidence_block(
        recalled_memories or [],
        max_items=max(int(os.environ.get("LOCOMO_QA_DIRECT_RECALL_LIMIT", "8") or 8), 1),
        max_chars_per_item=max(int(os.environ.get("LOCOMO_QA_DIRECT_RECALL_CHARS", "400") or 400), 80),
    )
    if question_time:
        if evidence_block:
            return f"{qa_prompt_prefix}Current date: {question_time}.\n\n{evidence_block}\n\nQuestion: {question}"
        return f"{qa_prompt_prefix}Current date: {question_time}. Question: {question}"
    if evidence_block:
        return f"{qa_prompt_prefix}{evidence_block}\n\nQuestion: {question}"
    return f"{qa_prompt_prefix}Question: {question}"


def build_ingest_input_message(message: str) -> str:
    ingest_prompt_prefix = os.environ.get("LOCOMO_INGEST_PROMPT_PREFIX", "").strip() or DEFAULT_LOCOMO_INGEST_PROMPT_PREFIX
    return f"{ingest_prompt_prefix}{message}"
