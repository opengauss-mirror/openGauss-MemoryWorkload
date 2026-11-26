"""Convert released BEAM conversations into a Workload benchmark scenario."""
from __future__ import annotations

import ast
import json
import hashlib
import os
import sys
from datetime import datetime
from itertools import chain
from pathlib import Path

# Bound each ordinary write without cutting a message or a user/assistant turn.
BATCH_CHARS = 12000


def _turns(chat):
    turn = []
    for message in chat:
        if message["role"] not in {"user", "assistant"} or not isinstance(message["content"], str):
            raise ValueError("BEAM chat must contain text messages with user/assistant roles")
        if turn and message["role"] == "user":
            yield turn
            turn = []
        turn.append(message)
    if turn:
        yield turn


def _chat_segments(chat, time_anchor=None):
    if isinstance(chat, list):
        if not chat:
            return
        if all(isinstance(message, dict) and "role" in message for message in chat):
            yield [{**message, "time_anchor": message.get("time_anchor") or time_anchor}
                   for message in chat]
        else:
            for child in chat:
                yield from _chat_segments(child, time_anchor)
        return
    if not isinstance(chat, dict):
        raise ValueError("BEAM chat must contain message lists, turn batches or plan groups")
    anchor = chat.get("time_anchor") or time_anchor
    if "turns" in chat:
        yield list(chain.from_iterable(_chat_segments(chat["turns"], anchor)))
    elif "role" in chat:
        yield [{**chat, "time_anchor": anchor}]
    elif chat and all(key.startswith("plan-") for key in chat):
        for batches in chat.values():
            yield from _chat_segments(batches, anchor)
    else:
        raise ValueError("Unsupported BEAM chat structure")


def _historical_timestamp(anchor):
    if not isinstance(anchor, str) or not anchor.strip():
        return None
    value = anchor.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    for fmt in ("%B-%d-%Y", "%B %d, %Y", "%B %d, %Y, %H:%M", "%B %d, %Y %H:%M"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            continue
    return None


def _batches(chat):
    messages, lines, size = [], [], 0
    batch_anchor = None
    for turn in _turns(chat):
        rendered = []
        for message in turn:
            anchor = message.get("time_anchor")
            date = f"[{anchor}] " if anchor else ""
            rendered.append(f"{date}{message['role']}: {message['content']}")
        turn_size = sum(len(text) + 1 for text in rendered)
        anchor = turn[0].get("time_anchor")
        if messages and (size + turn_size > BATCH_CHARS or anchor != batch_anchor):
            yield messages, "\n".join(lines), _historical_timestamp(batch_anchor)
            messages, lines, size = [], [], 0
        batch_anchor = anchor
        messages.extend({"role": m["role"], "content": m["content"]} for m in turn)
        lines.extend(rendered)
        size += turn_size
    if messages:
        yield messages, "\n".join(lines), _historical_timestamp(batch_anchor)


def build_scenario(data_path: Path) -> dict:
    source_bytes = data_path.read_bytes()
    text = source_bytes.decode("utf-8")
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.msg != "Extra data":
            raise
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(rows, dict):
        rows = [rows]
    samples = []
    for row in rows:
        sample_id = str(row["conversation_id"])
        chat = row.get("chat")
        if not isinstance(chat, list) or not chat:
            raise ValueError(f"{sample_id}: expected a nonempty BEAM chat")
        groups = row["probing_questions"]
        if isinstance(groups, str):
            try:
                groups = json.loads(groups)
            except json.JSONDecodeError:
                groups = ast.literal_eval(groups)
        if not isinstance(groups, dict):
            raise ValueError(f"{sample_id}: expected probing_questions grouped by category")
        timeline = [
            {"event_id": f"batch-{index}", "type": "conversation", "timestamp": timestamp,
             "payload": {"content": content, "messages": messages}}
            for index, (messages, content, timestamp) in enumerate(
                chain.from_iterable(_batches(segment) for segment in _chat_segments(chat)), start=1
            )
        ]
        if not timeline:
            raise ValueError(f"{sample_id}: no BEAM history messages")
        questions = []
        for category, probes in groups.items():
            if probes is None:
                continue
            for index, probe in enumerate(probes, start=1):
                rubric = probe["rubric"]
                if not isinstance(rubric, list) or not rubric or any(
                    not isinstance(item, str) or not item.strip() for item in rubric
                ):
                    raise ValueError(f"{sample_id}/{category}/{index}: expected text rubric criteria")
                questions.append({"question_id": f"{category}-{index}", "question": probe["question"],
                                  "reference": rubric, "category": category})
        if not questions:
            raise ValueError(f"{sample_id}: no BEAM probing questions")
        timeline.append({"event_id": "final-qa", "type": "checkpoint",
                         "evaluation": {"target": "qa_answer", "questions": questions}})
        samples.append({"sample_id": sample_id, "timeline": timeline})
    return {"source_kind": "benchmark_scenario", "benchmark_id": "beam",
            "evaluation": {"target": "qa_answer", "profile": "llm_judge@1",
                           "primary_metric": "mean_category_score"},
            "metadata": {"dataset": {"input_name": data_path.name,
                                     "sha256": hashlib.sha256(source_bytes).hexdigest(),
                                     "revision": os.environ.get("BEAM_DATA_REVISION")}},
            "samples": samples}


if __name__ == "__main__":
    print(json.dumps(build_scenario(Path(sys.argv[1])), ensure_ascii=True))
