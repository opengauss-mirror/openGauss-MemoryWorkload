import json
from pathlib import Path

from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding
from memory_bench_platform.composer import compose_run_plan
from memory_bench_platform.protocol import CaseRecord, ExecutionSpec, StepRecord
from memory_bench_platform.workflow_inputs import validate_workflow
from skills.benchmarks.locomo.scripts.build_scenario import build_scenario


def _write_dataset(path: Path, samples: list[dict]) -> Path:
    path.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8")
    return path


def _sample(sample_id: str, *, include_filtered_qa: bool = False) -> dict:
    qas = [{"question": "What language does A prefer?", "answer": "Go", "category": "2"}]
    if include_filtered_qa:
        qas.append({"question": "Image question", "answer": "yes", "category": "5"})
    return {
        "sample_id": sample_id,
        "conversation": {
            "speaker_a": "A",
            "speaker_b": "B",
            "session_2_date_time": "2:00 pm on 2 May, 2023",
            "session_2": [{"speaker": "B", "text": "second-session-private-text", "dia_id": "d2"}],
            "session_1_date_time": "1:00 pm on 1 May, 2023",
            "session_1": [{"speaker": "A", "text": "first-session-private-text", "dia_id": "d1"}],
        },
        "qa": qas,
    }


def _compose(data_path: Path, integration: str) -> tuple[BenchmarkScenario, dict]:
    scenario = BenchmarkScenario.model_validate(build_scenario(data_path))
    binding = RunBinding(
        benchmark_id="locomo",
        agent_id="openclaw",
        agent_runtime_id="main",
        agent_local=True,
        memory_id="openviking",
        memory_integration=integration,
        memory_plugin_id="openclaw-openviking" if integration == "agent_plugin" else None,
        run_id="run-isolated",
    )
    return scenario, compose_run_plan(scenario, binding)


def test_locomo_adapter_emits_runtime_independent_scenario(tmp_path: Path):
    data_path = _write_dataset(tmp_path / "locomo.json", [_sample("conv-1", include_filtered_qa=True)])

    scenario = build_scenario(data_path)

    assert scenario["source_kind"] == "benchmark_scenario"
    sample = scenario["samples"][0]
    assert [event["event_id"] for event in sample["timeline"]] == ["session_1", "session_2", "final-qa"]
    assert sample["timeline"][-1]["evaluation"]["questions"] == [
        {
            "question_id": "q1",
            "question": "What language does A prefer?",
            "reference": "Go",
            "category": "2",
            "metadata": {"question_index": 1},
        }
    ]
    serialized = json.dumps(scenario, ensure_ascii=False)
    assert "memory_integration" not in serialized
    assert "flush" not in serialized
    assert "wait_settle" not in serialized
    assert "OpenViking" not in serialized


def test_composer_builds_backend_direct_workflow(tmp_path: Path):
    data_path = _write_dataset(tmp_path / "locomo.json", [_sample("conv-1")])
    _scenario, payload = _compose(data_path, "backend_direct")

    assert [case["case_id"] for case in payload["cases"]] == [
        "conv-1-stage-1-setup",
        "conv-1-q1",
    ]
    assert [step["operator_kind"] for step in payload["steps"]] == [
        "memory", "memory", "poll", "memory", "memory", "poll", "memory", "agent"
    ]
    assert [
        step["inputs"].get("action")
        for step in payload["steps"]
        if step["operator_kind"] == "memory"
    ] == ["ingest", "flush", "ingest", "flush", "recall"]
    assert payload["steps"][-1]["inputs"]["messages"][0]["content"]["$template"].startswith(
        "Recalled memory evidence:\n"
    )
    cases = [CaseRecord(run_id="run-1", **item) for item in payload["cases"]]
    steps = [StepRecord(**item) for item in payload["steps"]]
    validate_workflow(
        cases=cases,
        steps=steps,
        execution_spec=ExecutionSpec(**payload["execution_spec"]),
        memory_id="openviking",
    )


def test_composer_builds_agent_plugin_workflow_with_generic_actions(tmp_path: Path):
    data_path = _write_dataset(tmp_path / "locomo.json", [_sample("conv-1")])
    _scenario, payload = _compose(data_path, "agent_plugin")

    steps = payload["steps"]
    actions = [step["inputs"].get("action") for step in steps if step["operator_kind"] == "memory_plugin"]
    assert actions == [
        "validate",
        "prepare",
        "set_phase",
        "commit",
        "wait_ready",
        "commit",
        "wait_ready",
        "set_phase",
    ]
    commit_steps = [
        step for step in steps
        if step["operator_kind"] == "memory_plugin" and step["inputs"].get("action") == "commit"
    ]
    assert commit_steps[0]["inputs"]["session_handle"] == {
        "$ref": "steps.conv-1-stage-1-plugin-setup-session-1-agent-ingest.output.session_handle"
    }
    assert not any(step["operator_kind"] == "memory" for step in steps)
    assert steps[1]["inputs"]["namespace"] == "run-isolated-conv-1"
    assert steps[-1]["operator_kind"] == "agent"
    cases = [CaseRecord(run_id="run-1", **item) for item in payload["cases"]]
    workflow_steps = [StepRecord(**item) for item in steps]
    validate_workflow(
        cases=cases,
        steps=workflow_steps,
        execution_spec=ExecutionSpec(**payload["execution_spec"]),
        memory_id="openviking",
        memory_plugin_id="openclaw-openviking",
    )
