from __future__ import annotations

import json


def build_cases() -> dict:
    return {
        "source_kind": "native_workflow",
        "cases": [
            {
                "case_id": "ovtest-memory-1",
                "title": "ovtest memory smoke",
                "goal": "Verify workflow engine can carry evidence across bash and wait operators.",
                "capability": "memory/store-retrieve",
                "reference": {
                    "expected_answer": "For systems programming I prefer Go over Python."
                },
                "labels": ["source:ovtest", "native-workflow"],
                "source_metadata": {},
                "judge_mode": "builtin",
            }
        ],
        "steps": [
            {
                "step_id": "emit-fact",
                "case_id": "ovtest-memory-1",
                "name": "emit_fact",
                "operator_kind": "bash",
                "depends_on": [],
                "retry_limit": 0,
                "timeout_seconds": 30,
                "gate_policy": "hard",
                "inputs": {
                    "cmd": [
                        "python3",
                        "-c",
                        "print('For systems programming I prefer Go over Python.')",
                    ]
                },
            },
            {
                "step_id": "settle",
                "case_id": "ovtest-memory-1",
                "name": "settle",
                "operator_kind": "wait",
                "depends_on": ["emit-fact"],
                "retry_limit": 0,
                "timeout_seconds": 5,
                "gate_policy": "soft",
                "inputs": {"seconds": 0},
            },
        ],
        "execution_spec": {
            "case_mode": "dag",
            "max_parallel_steps": 1,
            "fail_fast": True,
            "default_retry_limit": 0,
            "default_timeout_seconds": 30,
        },
    }


def main() -> None:
    print(json.dumps(build_cases(), ensure_ascii=False))


if __name__ == "__main__":
    main()
