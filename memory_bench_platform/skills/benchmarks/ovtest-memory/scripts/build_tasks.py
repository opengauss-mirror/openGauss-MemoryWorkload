from __future__ import annotations

import json


def build_cases() -> dict:
    return {
        "source_kind": "native_workflow",
        "cases": [
            {
                "case_id": "ovtest-memory-1",
                "title": "ovtest memory smoke",
                "goal": "Verify native ingest, completion polling, recall, and agent answer flow.",
                "capability": "memory/store-retrieve",
                "reference": {
                    "expected_answer": "For systems programming I prefer Go over Python.",
                    "expected_step_id": "agent-answer",
                },
                "labels": ["source:ovtest", "native-workflow"],
                "source_metadata": {},
                "judge_mode": "builtin",
            }
        ],
        "steps": [
            {
                "step_id": "memory-ingest",
                "case_id": "ovtest-memory-1",
                "name": "memory_ingest",
                "operator_kind": "memory",
                "depends_on": [],
                "retry_limit": 0,
                "timeout_seconds": 30,
                "gate_policy": "hard",
                "inputs": {
                    "action": "ingest",
                    "content": "For systems programming I prefer Go over Python.",
                },
            },
            {
                "step_id": "poll-ingest",
                "case_id": "ovtest-memory-1",
                "name": "poll_ingest",
                "operator_kind": "poll",
                "depends_on": ["memory-ingest"],
                "retry_limit": 0,
                "timeout_seconds": 120,
                "gate_policy": "hard",
                "inputs": {
                    "interval_seconds": 1,
                    "probe": {
                        "operator_kind": "memory",
                        "action": "status",
                        "inputs": {
                            "operation": {"$ref": "steps.memory-ingest.output.operation"},
                        },
                    },
                    "success_when": {"path": "state", "equals": "completed"},
                    "failure_when": {"path": "state", "equals": "failed"},
                },
            },
            {
                "step_id": "memory-recall",
                "case_id": "ovtest-memory-1",
                "name": "memory_recall",
                "operator_kind": "memory",
                "depends_on": ["poll-ingest"],
                "retry_limit": 0,
                "timeout_seconds": 30,
                "gate_policy": "hard",
                "inputs": {
                    "action": "recall",
                    "query": "Which language do I prefer for systems programming?",
                    "node_limit": 5,
                },
            },
            {
                "step_id": "agent-answer",
                "case_id": "ovtest-memory-1",
                "name": "agent_answer",
                "operator_kind": "agent",
                "depends_on": ["memory-recall"],
                "retry_limit": 0,
                "timeout_seconds": 30,
                "gate_policy": "hard",
                "inputs": {
                    "question": {
                        "$template": "Use this recalled evidence to answer the preference question: {{ steps.memory-recall.output.evidence_text }}"
                    },
                    "metadata": {"source": "ovtest-memory"},
                },
            },
        ],
        "execution_spec": {
            "case_mode": "single_path",
            "max_parallel_steps": 1,
            "fail_fast": True,
            "default_retry_limit": 0,
            "default_timeout_seconds": 120,
        },
    }


def main() -> None:
    print(json.dumps(build_cases(), ensure_ascii=False))


if __name__ == "__main__":
    main()
