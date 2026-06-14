from __future__ import annotations

import json
import os


def build_cases() -> dict:
    base_url = os.environ.get("OVTEST_HEALTH_URL", "http://127.0.0.1:1933/health")
    return {
        "source_kind": "native_workflow",
        "cases": [
            {
                "case_id": "ovtest-health-1",
                "title": "ov health endpoint smoke",
                "goal": "Verify the OpenViking health endpoint is reachable and healthy.",
                "capability": "service/health-check",
                "reference": {"expected_answer": "\"healthy\":true"},
                "labels": ["source:ovtest", "native-workflow", "openviking-health"],
                "source_metadata": {"url": base_url},
                "judge_mode": "builtin",
            }
        ],
        "steps": [
            {
                "step_id": "http-health",
                "case_id": "ovtest-health-1",
                "name": "http_health",
                "operator_kind": "http",
                "depends_on": [],
                "retry_limit": 1,
                "timeout_seconds": 10,
                "gate_policy": "hard",
                "inputs": {"method": "GET", "url": base_url},
            }
        ],
        "execution_spec": {
            "case_mode": "single_path",
            "max_parallel_steps": 1,
            "fail_fast": True,
            "default_retry_limit": 0,
            "default_timeout_seconds": 10,
        },
    }


def main() -> None:
    print(json.dumps(build_cases(), ensure_ascii=False))


if __name__ == "__main__":
    main()
