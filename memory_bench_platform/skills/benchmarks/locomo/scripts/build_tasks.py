from __future__ import annotations

import json
import sys
from pathlib import Path


def _session_keys(sample: dict) -> list[str]:
    conv = sample.get("conversation", {})
    keys = [
        key
        for key, value in conv.items()
        if key.startswith("session_")
        and not key.endswith("_date_time")
        and isinstance(value, list)
    ]
    return sorted(keys, key=lambda key: int(key.split("_")[1]))


def _format_session(sample: dict, session_key: str) -> str:
    conv = sample.get("conversation", {})
    lines = [
        "LoCoMo conversation session for memory ingestion.",
        f"Speaker A: {conv.get('speaker_a', '')}",
        f"Speaker B: {conv.get('speaker_b', '')}",
        f"{session_key} @ {conv.get(f'{session_key}_date_time', '')}",
    ]
    for turn in conv.get(session_key, []):
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
        setup_case_id = f"{sample_id}-setup"
        session_keys = _session_keys(sample)
        cases.append(
            {
                "case_id": setup_case_id,
                "title": f"LoCoMo memory setup {sample_id}",
                "goal": "Ingest every conversation session and wait until memory is searchable.",
                "capability": "memory/ingest",
                "depends_on_cases": [],
                "reference": {},
                "labels": ["source:locomo", "phase:setup"],
                "source_metadata": {
                    "sample_id": sample_id,
                    "session_count": len(session_keys),
                },
                "judge_mode": "none",
            }
        )

        previous_poll_step_id = ""
        for session_key in session_keys:
            ingest_step_id = f"{setup_case_id}-{session_key.replace('_', '-')}-ingest"
            poll_step_id = f"{setup_case_id}-{session_key.replace('_', '-')}-poll"
            steps.append(
                {
                    "step_id": ingest_step_id,
                    "case_id": setup_case_id,
                    "name": f"ingest_{session_key}",
                    "operator_kind": "memory",
                    "depends_on": [previous_poll_step_id] if previous_poll_step_id else [],
                    "retry_limit": 0,
                    "timeout_seconds": 30,
                    "gate_policy": "hard",
                    "inputs": {
                        "action": "ingest",
                        "content": _format_session(sample, session_key),
                    },
                }
            )
            steps.append(
                {
                    "step_id": poll_step_id,
                    "case_id": setup_case_id,
                    "name": f"poll_{session_key}",
                    "operator_kind": "poll",
                    "depends_on": [ingest_step_id],
                    "retry_limit": 0,
                    "timeout_seconds": 600,
                    "gate_policy": "hard",
                    "inputs": {
                        "interval_seconds": 1,
                        "probe": {
                            "operator_kind": "memory",
                            "action": "status",
                            "inputs": {
                                "operation": {
                                    "$ref": f"steps.{ingest_step_id}.output.operation"
                                }
                            },
                        },
                        "success_when": {"path": "state", "equals": "completed"},
                        "failure_when": {"path": "state", "equals": "failed"},
                    },
                }
            )
            previous_poll_step_id = poll_step_id

        for idx, qa in enumerate(sample.get("qa", []), start=1):
            if str(qa.get("category", "")) == "5":
                continue
            case_id = f"{sample_id}-q{idx}"
            recall_step_id = f"{case_id}-memory-recall"
            agent_step_id = f"{case_id}-agent-answer"
            question = qa.get("question", "")
            answer = str(qa.get("answer", ""))
            category = str(qa.get("category", ""))
            cases.append(
                {
                    "case_id": case_id,
                    "title": f"LoCoMo QA {sample_id} #{idx}",
                    "goal": "Answer the LoCoMo question using recalled memory evidence.",
                    "capability": "memory/question-answering",
                    "depends_on_cases": [setup_case_id],
                    "reference": {
                        "question": question,
                        "expected_answer": answer,
                        "expected_step_id": agent_step_id,
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
                    "step_id": recall_step_id,
                    "case_id": case_id,
                    "name": "memory_recall",
                    "operator_kind": "memory",
                    "depends_on": [],
                    "retry_limit": 0,
                    "timeout_seconds": 90,
                    "gate_policy": "hard",
                    "inputs": {
                        "action": "recall",
                        "query": question,
                        "node_limit": 10,
                    },
                }
            )
            steps.append(
                {
                    "step_id": agent_step_id,
                    "case_id": case_id,
                    "name": "agent_answer",
                    "operator_kind": "agent",
                    "depends_on": [recall_step_id],
                    "retry_limit": 0,
                    "timeout_seconds": 90,
                    "gate_policy": "hard",
                    "inputs": {
                        "system_prompt": "Answer the question using only the recalled memory evidence. If the evidence is insufficient, abstain conservatively.",
                        "messages": [
                            {
                                "role": "user",
                                "content": {
                                    "$template": (
                                        "Recalled memory evidence:\n"
                                        f"{{{{ steps.{recall_step_id}.output.evidence_text }}}}\n\n"
                                        f"Question: {question}"
                                    )
                                },
                            }
                        ],
                        "metadata": {
                            "sample_id": sample_id,
                            "question_index": idx,
                            "agent_id": "locomo-eval",
                        },
                    },
                }
            )
    return {
        "source_kind": "native_workflow",
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
