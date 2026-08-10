from __future__ import annotations

import argparse
import json
from pathlib import Path


def _session_keys(sample: dict) -> list[str]:
    conversation = sample.get("conversation", {})
    keys = [
        key
        for key, value in conversation.items()
        if key.startswith("session_")
        and not key.endswith("_date_time")
        and isinstance(value, list)
    ]
    return sorted(keys, key=lambda key: int(key.split("_")[1]))


def _conversation_event(sample: dict, session_key: str) -> dict:
    conversation = sample.get("conversation", {})
    messages = []
    for turn in conversation.get(session_key, []):
        messages.append(
            {
                "role": "user" if not messages else "assistant",
                "speaker": str(turn.get("speaker", "")),
                "content": str(turn.get("text", "")),
                "dia_id": str(turn.get("dia_id", "")),
            }
        )
    formatted_lines = [
        "LoCoMo conversation session for memory ingestion.",
        f"Speaker A: {conversation.get('speaker_a', '')}",
        f"Speaker B: {conversation.get('speaker_b', '')}",
        f"{session_key} @ {conversation.get(f'{session_key}_date_time', '')}",
    ]
    formatted_lines.extend(
        f"[{turn.get('dia_id', '')}] {turn.get('speaker', '')}: {turn.get('text', '')}"
        for turn in conversation.get(session_key, [])
    )
    return {
        "event_id": session_key,
        "type": "conversation",
        "timestamp": str(conversation.get(f"{session_key}_date_time", "")) or None,
        "payload": {
            "content": "\n".join(formatted_lines),
            "speaker_a": str(conversation.get("speaker_a", "")),
            "speaker_b": str(conversation.get("speaker_b", "")),
            "messages": messages,
        },
        "metadata": {"session_key": session_key},
    }


def build_scenario(data_path: Path) -> dict:
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    samples = []
    for sample_index, sample in enumerate(raw):
        sample_id = str(sample.get("sample_id") or f"sample-{sample_index}")
        timeline = [
            _conversation_event(sample, session_key)
            for session_key in _session_keys(sample)
        ]
        questions = []
        for qa_index, qa in enumerate(sample.get("qa", []), start=1):
            category = str(qa.get("category", ""))
            if category == "5":
                continue
            questions.append(
                {
                    "question_id": f"q{qa_index}",
                    "question": str(qa.get("question", "")),
                    "reference": str(qa.get("answer", "")),
                    "category": category,
                    "metadata": {"question_index": qa_index},
                }
            )
        timeline.append(
            {
                "event_id": "final-qa",
                "type": "checkpoint",
                "evaluation": {
                    "target": "qa_answer",
                    "profile": "llm_judge@1",
                    "primary_metric": "accuracy",
                    "questions": questions,
                },
            }
        )
        samples.append(
            {
                "sample_id": sample_id,
                "namespace_hint": sample_id,
                "timeline": timeline,
                "metadata": {
                    "sample_index": sample_index,
                    "session_count": len(timeline) - 1,
                    "question_count": len(questions),
                },
            }
        )

    return {
        "source_kind": "benchmark_scenario",
        "benchmark_id": "locomo",
        "requirements": {
            "agent": {"multi_turn": True, "stateful_session": True},
            "memory": {"actions": ["ingest", "recall"]},
        },
        "evaluation": {
            "target": "qa_answer",
            "profile": "llm_judge@1",
            "primary_metric": "accuracy",
        },
        "samples": samples,
        "execution_spec": {
            "case_mode": "single_path",
            "max_parallel_steps": 1,
            "fail_fast": True,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("data_path", nargs="?")
    args = parser.parse_args()
    workspace_root = Path(__file__).resolve().parents[5]
    default_path = (
        Path(args.data_path)
        if args.data_path
        else workspace_root / "locomo_test" / "data" / "locomo_small.json"
    )
    print(json.dumps(build_scenario(default_path), ensure_ascii=False))
