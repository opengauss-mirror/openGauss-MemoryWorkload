from __future__ import annotations

import json
import os
import time


def build_cases() -> dict:
    stamp = str(int(time.time()))
    server_url = os.environ.get("OVTEST_SERVER_URL", "http://127.0.0.1:1933")
    root_key = os.environ.get("OVTEST_ROOT_KEY", "")
    account = os.environ.get("OVTEST_ACCOUNT", f"codex-smoke-{stamp}")
    admin_user = os.environ.get("OVTEST_ADMIN_USER", "codex-admin")
    tmpdir = os.environ.get("OVTEST_TMPDIR", f"/tmp/ovtest-admin-memory-{stamp}")

    root_conf = f"{tmpdir}/root.conf"
    user_conf = f"{tmpdir}/user.conf"
    create_json = f"{tmpdir}/create.json"
    user_key_file = f"{tmpdir}/user.key"
    find_json = f"{tmpdir}/find.json"
    memory_text = "For systems programming I prefer Go over Python."

    cleanup_cmd = (
        f"rm -rf '{tmpdir}' && mkdir -p '{tmpdir}'"
    )
    create_cmd = (
        "set -e; "
        f"printf '%s' '{{\"url\":\"{server_url}\",\"api_key\":\"{root_key}\"}}' > '{root_conf}'; "
        f"OPENVIKING_CLI_CONFIG_FILE='{root_conf}' ov -o json admin create-account '{account}' --admin '{admin_user}' > '{create_json}'; "
        f"python3 -c \"import json; d=json.load(open('{create_json}', encoding='utf-8')); print(d['result']['user_key'])\" > '{user_key_file}'; "
        f"cat '{create_json}'"
    )
    add_memory_cmd = (
        "set -e; "
        f"USER_KEY=$(cat '{user_key_file}'); "
        f"printf '%s' '{{\"url\":\"{server_url}\",\"api_key\":\"'\"$USER_KEY\"'\",\"account\":\"{account}\",\"user\":\"{admin_user}\"}}' > '{user_conf}'; "
        f"OPENVIKING_CLI_CONFIG_FILE='{user_conf}' ov -o json add-memory \"{memory_text}\""
    )
    find_cmd = (
        "set -e; "
        f"USER_KEY=$(cat '{user_key_file}'); "
        f"printf '%s' '{{\"url\":\"{server_url}\",\"api_key\":\"'\"$USER_KEY\"'\",\"account\":\"{account}\",\"user\":\"{admin_user}\"}}' > '{user_conf}'; "
        f"OPENVIKING_CLI_CONFIG_FILE='{user_conf}' ov -o json find 'systems programming' > '{find_json}'; "
        f"cat '{find_json}'"
    )

    return {
        "source_kind": "native_workflow",
        "cases": [
            {
                "case_id": "ovtest-admin-memory-1",
                "title": "ov admin create-account/add-memory/find",
                "goal": "Verify OpenViking account bootstrap and memory retrieval through ov CLI.",
                "capability": "memory/store-retrieve",
                "reference": {
                    "expected_answer": "Go over Python",
                    "expected_step_id": "find-memory",
                    "account": account,
                    "admin_user": admin_user,
                },
                "labels": ["source:ovtest", "native-workflow", "openviking-admin-memory"],
                "source_metadata": {"server_url": server_url, "tmpdir": tmpdir},
                "judge_mode": "builtin",
            }
        ],
        "steps": [
            {
                "step_id": "cleanup",
                "case_id": "ovtest-admin-memory-1",
                "name": "cleanup",
                "operator_kind": "bash",
                "depends_on": [],
                "retry_limit": 0,
                "timeout_seconds": 10,
                "gate_policy": "soft",
                "inputs": {"cmd": ["sh", "-lc", cleanup_cmd]},
            },
            {
                "step_id": "create-account",
                "case_id": "ovtest-admin-memory-1",
                "name": "create_account",
                "operator_kind": "bash",
                "depends_on": ["cleanup"],
                "retry_limit": 0,
                "timeout_seconds": 30,
                "gate_policy": "hard",
                "inputs": {"cmd": ["sh", "-lc", create_cmd]},
            },
            {
                "step_id": "add-memory",
                "case_id": "ovtest-admin-memory-1",
                "name": "add_memory",
                "operator_kind": "bash",
                "depends_on": ["create-account"],
                "retry_limit": 0,
                "timeout_seconds": 30,
                "gate_policy": "hard",
                "inputs": {"cmd": ["sh", "-lc", add_memory_cmd]},
            },
            {
                "step_id": "settle",
                "case_id": "ovtest-admin-memory-1",
                "name": "settle",
                "operator_kind": "wait",
                "depends_on": ["add-memory"],
                "retry_limit": 0,
                "timeout_seconds": 5,
                "gate_policy": "soft",
                "inputs": {"seconds": 1},
            },
            {
                "step_id": "find-memory",
                "case_id": "ovtest-admin-memory-1",
                "name": "find_memory",
                "operator_kind": "bash",
                "depends_on": ["settle"],
                "retry_limit": 2,
                "timeout_seconds": 30,
                "gate_policy": "hard",
                "inputs": {"cmd": ["sh", "-lc", find_cmd]},
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
