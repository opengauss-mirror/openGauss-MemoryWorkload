from __future__ import annotations

import json
import sys
from pathlib import Path


def _format_history_message(item: dict) -> str:
    question_date = item.get("question_date", "")
    haystack_dates = item.get("haystack_dates", [])
    haystack_sessions = item.get("haystack_sessions", [])
    lines = [
        "You are answering a LongMemEval question.",
        f"Question date: {question_date}",
        "Use the timestamped multi-session history below.",
    ]
    for idx, session in enumerate(haystack_sessions):
        date = haystack_dates[idx] if idx < len(haystack_dates) else f"session-{idx + 1}"
        lines.append(f"[{date}]")
        for turn in session:
            role = str(turn.get("role", "user"))
            content = str(turn.get("content", ""))
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def build_cases(data_path: Path | None = None) -> dict:
    if data_path is None:
        return {
            "source_kind": "benchmark_case_source",
            "cases": [],
            "steps": [],
            "execution_spec": {
                "case_mode": "single_path",
                "max_parallel_steps": 1,
                "fail_fast": True,
                "default_retry_limit": 0,
                "default_timeout_seconds": 90,
            },
        }

    raw = json.loads(data_path.read_text(encoding="utf-8"))
    cases = []
    steps = []
    for item in raw:
        question_id = str(item.get("question_id", "unknown-question"))
        question_type = str(item.get("question_type", "unknown-type"))
        question = str(item.get("question", ""))
        answer = str(item.get("answer", ""))
        labels = [f"question_type:{question_type}", "source:longmemeval"]
        if question_id.endswith("_abs"):
            labels.append("abstention")
        cases.append(
            {
                "case_id": question_id,
                "title": f"LongMemEval {question_type}",
                "goal": "Answer the LongMemEval question using the available long-term history.",
                "capability": "memory/question-answering",
                "reference": {
                    "question_id": question_id,
                    "question": question,
                    "expected_answer": answer,
                    "question_type": question_type,
                    "question_date": item.get("question_date"),
                    "answer_session_ids": item.get("answer_session_ids", []),
                    "haystack_session_ids": item.get("haystack_session_ids", []),
                },
                "labels": labels,
                "source_metadata": {
                    "haystack_dates": item.get("haystack_dates", []),
                    "haystack_session_count": len(item.get("haystack_sessions", [])),
                },
                "judge_mode": "builtin",
            }
        )
        steps.append(
            {
                "step_id": f"{question_id}-agent-query",
                "case_id": question_id,
                "name": "agent_query",
                "operator_kind": "agent",
                "depends_on": [],
                "retry_limit": 0,
                "timeout_seconds": 90,
                "gate_policy": "hard",
                "inputs": {
                    "system_prompt": "Answer the question using only the provided timestamped history. If the history is insufficient, abstain conservatively.",
                    "messages": [
                        {"role": "user", "content": _format_history_message(item)},
                        {"role": "user", "content": question},
                    ],
                    "metadata": {
                        "question_id": question_id,
                        "question_type": question_type,
                        "question_date": item.get("question_date"),
                        "haystack_session_ids": item.get("haystack_session_ids", []),
                        "answer_session_ids": item.get("answer_session_ids", []),
                    },
                },
            }
        )
    return {
        "source_kind": "benchmark_case_source",
        "cases": cases,
        "steps": steps,
        "execution_spec": {
            "case_mode": "single_path",
            "max_parallel_steps": 1,
            "fail_fast": True,
            "default_retry_limit": 0,
            "default_timeout_seconds": 90,
        },
    }


def main() -> None:
    data_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    print(json.dumps(build_cases(data_path), ensure_ascii=False))


if __name__ == "__main__":
    main()
