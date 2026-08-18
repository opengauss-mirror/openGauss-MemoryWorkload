from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _session_content(session: list[dict[str, Any]], date: str, session_id: str) -> str:
    lines = [
        "LongMemEval timestamped conversation session for memory ingestion.",
        f"Session ID: {session_id}",
        f"Session date: {date}",
    ]
    for turn in session:
        role = str(turn.get("role") or "user")
        content = str(turn.get("content") or "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def build_scenario(data_path: Path | None = None) -> dict[str, Any]:
    if data_path is None:
        raise ValueError("LongMemEval data path is required")
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    samples: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        question_id = str(item.get("question_id") or f"question-{index}")
        question_type = str(item.get("question_type") or "unknown-type")
        sessions = item.get("haystack_sessions", [])
        dates = item.get("haystack_dates", [])
        session_ids = item.get("haystack_session_ids", [])
        timeline: list[dict[str, Any]] = []
        for session_index, session in enumerate(sessions, start=1):
            date = str(dates[session_index - 1]) if session_index <= len(dates) else ""
            session_id = (
                str(session_ids[session_index - 1])
                if session_index <= len(session_ids)
                else f"session-{session_index}"
            )
            timeline.append(
                {
                    "event_id": session_id,
                    "type": "conversation",
                    "timestamp": date or None,
                    "payload": {
                        "content": _session_content(session, date, session_id),
                        "messages": session,
                    },
                    "metadata": {"session_index": session_index},
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
                    "questions": [
                        {
                            "question_id": question_id,
                            "question": str(item.get("question") or ""),
                            "reference": item.get("answer", ""),
                            "category": question_type,
                            "metadata": {
                                "question_date": item.get("question_date"),
                                "question_type": question_type,
                                "answer_session_ids": item.get("answer_session_ids", []),
                                "abstention": question_id.endswith("_abs"),
                            },
                        }
                    ],
                },
            }
        )
        samples.append(
            {
                "sample_id": question_id,
                "namespace_hint": question_id,
                "timeline": timeline,
                "metadata": {
                    "question_date": item.get("question_date"),
                    "question_type": question_type,
                    "session_count": len(sessions),
                },
            }
        )
    return {
        "source_kind": "benchmark_scenario",
        "benchmark_id": "longmemeval",
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


def main() -> None:
    data_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    print(json.dumps(build_scenario(data_path), ensure_ascii=False))


if __name__ == "__main__":
    main()
