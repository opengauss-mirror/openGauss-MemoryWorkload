from __future__ import annotations

import argparse
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


def _build_backend_direct_tasks(data_path: Path) -> dict:
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
                    "judge_mode": "external",
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
        "memory_integration": "backend_direct",
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


def _build_agent_plugin_tasks(data_path: Path, session_namespace: str = "") -> dict:
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    cases = []
    steps = []
    for sample in raw:
        sample_id = str(sample.get("sample_id", "sample-0"))
        session_prefix = f"{session_namespace}:" if session_namespace else ""
        plugin_namespace = f"{session_namespace}-{sample_id}" if session_namespace else sample_id
        setup_case_id = f"{sample_id}-plugin-setup"
        session_keys = _session_keys(sample)
        cases.append(
            {
                "case_id": setup_case_id,
                "title": f"LoCoMo plugin memory setup {sample_id}",
                "goal": "Ingest every conversation session through the configured Agent memory integration.",
                "capability": "agent-memory/plugin-ingest",
                "depends_on_cases": [],
                "reference": {},
                "labels": ["source:locomo", "phase:setup", "memory-integration:agent-plugin"],
                "source_metadata": {"sample_id": sample_id, "session_count": len(session_keys)},
                "judge_mode": "none",
            }
        )

        validate_step_id = f"{setup_case_id}-validate"
        prepare_step_id = f"{setup_case_id}-prepare"
        ingest_phase_step_id = f"{setup_case_id}-set-ingest-phase"
        steps.extend(
            [
                {
                    "step_id": validate_step_id,
                    "case_id": setup_case_id,
                    "name": "validate_memory_plugin",
                    "operator_kind": "memory_plugin",
                    "depends_on": [],
                    "retry_limit": 0,
                    "timeout_seconds": 60,
                    "gate_policy": "hard",
                    "inputs": {"action": "validate"},
                },
                {
                    "step_id": prepare_step_id,
                    "case_id": setup_case_id,
                    "name": "prepare_memory_plugin",
                    "operator_kind": "memory_plugin",
                    "depends_on": [validate_step_id],
                    "retry_limit": 0,
                    "timeout_seconds": 60,
                    "gate_policy": "hard",
                    "inputs": {"action": "prepare", "namespace": plugin_namespace},
                },
                {
                    "step_id": ingest_phase_step_id,
                    "case_id": setup_case_id,
                    "name": "set_plugin_ingest_phase",
                    "operator_kind": "memory_plugin",
                    "depends_on": [prepare_step_id],
                    "retry_limit": 0,
                    "timeout_seconds": 60,
                    "gate_policy": "hard",
                    "inputs": {"action": "set_phase", "phase": "ingest"},
                },
            ]
        )

        previous_step_id = ingest_phase_step_id
        for session_key in session_keys:
            session_slug = session_key.replace("_", "-")
            agent_step_id = f"{setup_case_id}-{session_slug}-agent-ingest"
            flush_step_id = f"{setup_case_id}-{session_slug}-flush"
            wait_step_id = f"{setup_case_id}-{session_slug}-wait-settle"
            semantic_session_key = f"{session_prefix}ingest-{sample_id}-{session_key}"
            steps.extend(
                [
                    {
                        "step_id": agent_step_id,
                        "case_id": setup_case_id,
                        "name": f"agent_ingest_{session_key}",
                        "operator_kind": "agent",
                        "depends_on": [previous_step_id],
                        "retry_limit": 0,
                        "timeout_seconds": 900,
                        "gate_policy": "hard",
                        "inputs": {
                            "system_prompt": (
                                "The user message is a historical conversation supplied for long-term "
                                "memory ingestion. Do not summarize, transform, infer, or add facts. "
                                "Reply exactly INGEST_OK."
                            ),
                            "messages": [
                                {"role": "user", "content": _format_session(sample, session_key)}
                            ],
                            "metadata": {
                                "sample_id": sample_id,
                                "session_key": semantic_session_key,
                                "agent_id": "locomo-eval",
                                "timeout_seconds": 900,
                                "purpose": "memory_ingest",
                            },
                        },
                    },
                    {
                        "step_id": flush_step_id,
                        "case_id": setup_case_id,
                        "name": f"flush_{session_key}",
                        "operator_kind": "memory_plugin",
                        "depends_on": [agent_step_id],
                        "retry_limit": 0,
                        "timeout_seconds": 60,
                        "gate_policy": "hard",
                        "inputs": {
                            "action": "flush",
                            "session_key": semantic_session_key,
                            "agent_id": "locomo-eval",
                        },
                    },
                    {
                        "step_id": wait_step_id,
                        "case_id": setup_case_id,
                        "name": f"wait_{session_key}",
                        "operator_kind": "memory_plugin",
                        "depends_on": [flush_step_id],
                        "retry_limit": 0,
                        "timeout_seconds": 660,
                        "gate_policy": "hard",
                        "inputs": {
                            "action": "wait_settle",
                            "operation": {"$ref": f"steps.{flush_step_id}.output.operation"},
                            "timeout_seconds": 600,
                            "grace_seconds": 0,
                            "interval_seconds": 2,
                        },
                    },
                ]
            )
            previous_step_id = wait_step_id

        qa_phase_step_id = f"{setup_case_id}-set-qa-phase"
        steps.append(
            {
                "step_id": qa_phase_step_id,
                "case_id": setup_case_id,
                "name": "set_plugin_qa_phase",
                "operator_kind": "memory_plugin",
                "depends_on": [previous_step_id],
                "retry_limit": 0,
                "timeout_seconds": 60,
                "gate_policy": "hard",
                "inputs": {"action": "set_phase", "phase": "qa"},
            }
        )

        for idx, qa in enumerate(sample.get("qa", []), start=1):
            if str(qa.get("category", "")) == "5":
                continue
            case_id = f"{sample_id}-q{idx}"
            agent_step_id = f"{case_id}-agent-answer"
            question = str(qa.get("question", ""))
            answer = str(qa.get("answer", ""))
            category = str(qa.get("category", ""))
            cases.append(
                {
                    "case_id": case_id,
                    "title": f"LoCoMo plugin QA {sample_id} #{idx}",
                    "goal": "Answer using the Agent-managed memory plugin context.",
                    "capability": "agent-memory/plugin-question-answering",
                    "depends_on_cases": [setup_case_id],
                    "reference": {
                        "question": question,
                        "expected_answer": answer,
                        "expected_step_id": agent_step_id,
                        "category": category,
                        "sample_id": sample_id,
                    },
                    "labels": [
                        f"category:{category}",
                        "source:locomo",
                        "memory-integration:agent-plugin",
                    ],
                    "source_metadata": {"sample_id": sample_id, "question_index": idx},
                    "judge_mode": "external",
                }
            )
            steps.append(
                {
                    "step_id": agent_step_id,
                    "case_id": case_id,
                    "name": "agent_plugin_answer",
                    "operator_kind": "agent",
                    "depends_on": [],
                    "retry_limit": 0,
                    "timeout_seconds": 900,
                    "gate_policy": "hard",
                    "inputs": {
                        "system_prompt": (
                            "Answer the question using memory supplied by your context engine. "
                            "If memory is insufficient, abstain briefly."
                        ),
                        "messages": [{"role": "user", "content": f"Question: {question}"}],
                        "metadata": {
                            "sample_id": sample_id,
                            "question_index": idx,
                            "session_key": f"{session_prefix}qa-{sample_id}-q{idx}",
                            "agent_id": "locomo-eval",
                            "timeout_seconds": 900,
                            "purpose": "memory_qa",
                        },
                    },
                }
            )

    return {
        "source_kind": "native_workflow",
        "memory_integration": "agent_plugin",
        "cases": cases,
        "steps": steps,
        "execution_spec": {
            "case_mode": "single_path",
            "max_parallel_steps": 1,
            "fail_fast": True,
            "default_retry_limit": 0,
            "default_timeout_seconds": 900,
        },
    }


def build_tasks(
    data_path: Path,
    memory_integration: str = "backend_direct",
    session_namespace: str = "",
) -> dict:
    if memory_integration == "backend_direct":
        return _build_backend_direct_tasks(data_path)
    if memory_integration == "agent_plugin":
        return _build_agent_plugin_tasks(data_path, session_namespace)
    raise ValueError(f"unsupported memory integration: {memory_integration}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("data_path", nargs="?")
    parser.add_argument(
        "--memory-integration",
        choices=["backend_direct", "agent_plugin"],
        default="backend_direct",
    )
    parser.add_argument("--session-namespace", default="")
    args = parser.parse_args()
    workspace_root = Path(__file__).resolve().parents[5]
    default_path = Path(args.data_path) if args.data_path else workspace_root / "locomo_test" / "data" / "locomo_small.json"
    print(
        json.dumps(
            build_tasks(default_path, args.memory_integration, args.session_namespace),
            ensure_ascii=False,
        )
    )
