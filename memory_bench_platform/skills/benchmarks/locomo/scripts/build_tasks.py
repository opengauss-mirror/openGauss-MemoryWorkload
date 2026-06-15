from __future__ import annotations

import json
import sys
from pathlib import Path


def _format_conversation(sample: dict) -> str:
    conv = sample.get("conversation", {})
    lines = [
        "You are answering a LoCoMo memory question.",
        f"Speaker A: {conv.get('speaker_a', '')}",
        f"Speaker B: {conv.get('speaker_b', '')}",
    ]
    for idx in range(1, 5):
        date_key = f"session_{idx}_date_time"
        sess_key = f"session_{idx}"
        session = conv.get(sess_key, [])
        if not session:
            continue
        lines.append(f"{sess_key} @ {conv.get(date_key, '')}")
        for turn in session:
            speaker = str(turn.get("speaker", ""))
            text = str(turn.get("text", ""))
            dia_id = str(turn.get("dia_id", ""))
            lines.append(f"[{dia_id}] {speaker}: {text}")
    return "\n".join(lines)


def build_tasks(data_path: Path) -> dict:
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    cases = []
    steps = []
    for sample in raw:
        sample_id = str(sample.get("sample_id", "sample-0"))
        for idx, qa in enumerate(sample.get("qa", []), start=1):
            if str(qa.get("category", "")) == "5":
                continue
            case_id = f"{sample_id}-q{idx}"
            step_id = f"{case_id}-agent-query"
            question = qa.get("question", "")
            answer = str(qa.get("answer", ""))
            category = str(qa.get("category", ""))
            cases.append(
                {
                    "case_id": case_id,
                    "title": f"LoCoMo QA {sample_id} #{idx}",
                    "goal": "Answer the LoCoMo question using available memory context.",
                    "capability": "memory/question-answering",
                    "reference": {
                        "question": question,
                        "expected_answer": answer,
                        "category": category,
                        "sample_id": sample_id,
                    },
                    "labels": [f"category:{category}", "source:locomo"],
                    "source_metadata": {"sample_id": sample_id, "question_index": idx},
                    "judge_mode": "builtin",
                }
            )
            steps.append(
                {
                    "step_id": step_id,
                    "case_id": case_id,
                    "name": "agent_query",
                    "operator_kind": "agent",
                    "depends_on": [],
                    "retry_limit": 0,
                    "timeout_seconds": 90,
                    "gate_policy": "hard",
                    "inputs": {
                        "system_prompt": "Answer the question using only the provided multi-session conversation history. If the history is insufficient, abstain conservatively.",
                        "messages": [
                            {"role": "user", "content": _format_conversation(sample)},
                            {"role": "user", "content": question},
                        ],
                        "metadata": {"sample_id": sample_id, "agent_id": "locomo-eval"},
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


if __name__ == "__main__":
    workspace_root = Path(__file__).resolve().parents[5]
    default_path = Path(sys.argv[1]) if len(sys.argv) > 1 else workspace_root / "locomo_test" / "data" / "locomo_small.json"
    print(json.dumps(build_tasks(default_path), ensure_ascii=False))
