import json
from pathlib import Path

from memory_bench_platform.protocol import CaseRecord, ExecutionSpec, StepRecord
from memory_bench_platform.workflow_inputs import validate_workflow
from skills.benchmarks.locomo.scripts.build_tasks import build_tasks


def _write_dataset(path: Path, samples: list[dict]) -> Path:
    path.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8")
    return path


def _sample(sample_id: str, *, include_filtered_qa: bool = False) -> dict:
    qas = [
        {
            "question": "What language does A prefer?",
            "answer": "Go",
            "category": "2",
        }
    ]
    if include_filtered_qa:
        qas.append(
            {
                "question": "Should this image question be skipped?",
                "answer": "yes",
                "category": "5",
            }
        )
    return {
        "sample_id": sample_id,
        "conversation": {
            "speaker_a": "A",
            "speaker_b": "B",
            "session_2_date_time": "2:00 pm on 2 May, 2023",
            "session_2": [
                {"speaker": "B", "text": "second-session-private-text", "dia_id": "d2"}
            ],
            "session_1_date_time": "1:00 pm on 1 May, 2023",
            "session_1": [
                {"speaker": "A", "text": "first-session-private-text", "dia_id": "d1"}
            ],
        },
        "qa": qas,
    }


def test_locomo_task_builder_emits_sample_setup_and_dependent_qa(tmp_path: Path):
    data_path = _write_dataset(
        tmp_path / "locomo.json",
        [_sample("conv-1", include_filtered_qa=True)],
    )

    payload = build_tasks(data_path)

    assert payload["source_kind"] == "native_workflow"
    assert [case["case_id"] for case in payload["cases"]] == [
        "conv-1-setup",
        "conv-1-q1",
    ]
    setup_case, qa_case = payload["cases"]
    assert setup_case["judge_mode"] == "none"
    assert qa_case["judge_mode"] == "external"
    assert qa_case["depends_on_cases"] == ["conv-1-setup"]
    assert qa_case["reference"]["expected_step_id"] == "conv-1-q1-agent-answer"

    steps = payload["steps"]
    assert [step["operator_kind"] for step in steps] == [
        "memory",
        "poll",
        "memory",
        "poll",
        "memory",
        "agent",
    ]
    assert [step["step_id"] for step in steps] == [
        "conv-1-setup-session-1-ingest",
        "conv-1-setup-session-1-poll",
        "conv-1-setup-session-2-ingest",
        "conv-1-setup-session-2-poll",
        "conv-1-q1-memory-recall",
        "conv-1-q1-agent-answer",
    ]
    assert steps[0]["depends_on"] == []
    assert steps[1]["depends_on"] == ["conv-1-setup-session-1-ingest"]
    assert steps[2]["depends_on"] == ["conv-1-setup-session-1-poll"]
    assert steps[3]["depends_on"] == ["conv-1-setup-session-2-ingest"]
    assert steps[4]["inputs"]["query"] == "What language does A prefer?"
    assert steps[5]["depends_on"] == ["conv-1-q1-memory-recall"]

    agent_inputs = json.dumps(steps[5]["inputs"], ensure_ascii=False)
    assert "first-session-private-text" not in agent_inputs
    assert "second-session-private-text" not in agent_inputs
    assert "What language does A prefer?" in agent_inputs
    assert "steps.conv-1-q1-memory-recall.output.evidence_text" in agent_inputs

    cases = [CaseRecord(run_id="run-1", **item) for item in payload["cases"]]
    workflow_steps = [StepRecord(**item) for item in steps]
    validate_workflow(
        cases=cases,
        steps=workflow_steps,
        execution_spec=ExecutionSpec(**payload["execution_spec"]),
        memory_id="openviking",
    )


def test_locomo_task_builder_emits_one_setup_per_sample(tmp_path: Path):
    data_path = _write_dataset(
        tmp_path / "locomo.json",
        [_sample("conv-1"), _sample("conv-2")],
    )

    payload = build_tasks(data_path)

    assert [case["case_id"] for case in payload["cases"]] == [
        "conv-1-setup",
        "conv-1-q1",
        "conv-2-setup",
        "conv-2-q1",
    ]
    assert payload["cases"][1]["depends_on_cases"] == ["conv-1-setup"]
    assert payload["cases"][3]["depends_on_cases"] == ["conv-2-setup"]


def test_locomo_agent_plugin_builder_uses_agent_only_for_ingest_and_qa(tmp_path: Path):
    data_path = _write_dataset(tmp_path / "locomo.json", [_sample("conv-1")])

    payload = build_tasks(data_path, "agent_plugin", "run-isolated")

    assert payload["memory_integration"] == "agent_plugin"
    assert [case["case_id"] for case in payload["cases"]] == [
        "conv-1-plugin-setup",
        "conv-1-q1",
    ]
    steps = payload["steps"]
    assert [step["operator_kind"] for step in steps] == [
        "memory_plugin",
        "memory_plugin",
        "memory_plugin",
        "agent",
        "memory_plugin",
        "memory_plugin",
        "agent",
        "memory_plugin",
        "memory_plugin",
        "memory_plugin",
        "agent",
    ]
    assert not any(
        step["operator_kind"] == "memory" or step["inputs"].get("action") == "recall"
        for step in steps
    )
    ingest_step = steps[3]
    ingest_input = json.dumps(ingest_step["inputs"], ensure_ascii=False)
    assert "LoCoMo conversation session for memory ingestion." in ingest_input
    assert "[d1] A: first-session-private-text" in ingest_input
    assert "Reply exactly INGEST_OK" in ingest_input
    assert "OpenViking" not in json.dumps(steps, ensure_ascii=False)
    assert "Transform the following raw conversation" not in ingest_input
    assert "memory-ingestion notes" not in ingest_input
    assert steps[1]["inputs"] == {
        "action": "prepare",
        "namespace": "run-isolated-conv-1",
    }
    assert steps[2]["inputs"] == {"action": "set_phase", "phase": "ingest"}
    assert steps[4]["inputs"] == {
        "action": "flush",
        "session_key": "run-isolated:ingest-conv-1-session_1",
        "agent_id": "locomo-eval",
    }
    assert steps[5]["inputs"]["operation"] == {
        "$ref": "steps.conv-1-plugin-setup-session-1-flush.output.operation"
    }
    assert steps[9]["inputs"] == {"action": "set_phase", "phase": "qa"}
    qa_step = steps[-1]
    qa_input = json.dumps(qa_step["inputs"], ensure_ascii=False)
    assert "What language does A prefer?" in qa_input
    assert "evidence_text" not in qa_input
    assert "Recalled memory evidence" not in qa_input
    assert steps[3]["inputs"]["metadata"]["session_key"] == (
        "run-isolated:ingest-conv-1-session_1"
    )
    assert qa_step["inputs"]["metadata"]["session_key"] == "run-isolated:qa-conv-1-q1"
    assert payload["cases"][-1]["judge_mode"] == "external"

    cases = [CaseRecord(run_id="run-1", **item) for item in payload["cases"]]
    workflow_steps = [StepRecord(**item) for item in steps]
    validate_workflow(
        cases=cases,
        steps=workflow_steps,
        execution_spec=ExecutionSpec(**payload["execution_spec"]),
        memory_id="openviking",
        memory_plugin_id="openclaw-openviking",
    )


def test_locomo_agent_plugin_builder_isolates_multiple_samples(tmp_path: Path):
    data_path = _write_dataset(
        tmp_path / "locomo.json",
        [_sample("conv-1"), _sample("conv-2")],
    )

    payload = build_tasks(data_path, "agent_plugin", "run-isolated")

    assert [case["case_id"] for case in payload["cases"]] == [
        "conv-1-plugin-setup",
        "conv-1-q1",
        "conv-2-plugin-setup",
        "conv-2-q1",
    ]
    prepare_steps = [step for step in payload["steps"] if step["name"] == "prepare_memory_plugin"]
    assert [step["inputs"]["namespace"] for step in prepare_steps] == [
        "run-isolated-conv-1",
        "run-isolated-conv-2",
    ]
