import json
from pathlib import Path

import pytest

from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding
from memory_bench_platform.composer import compose_run_plan


def _scenario() -> BenchmarkScenario:
    payload = json.loads(
        Path("tests/golden/multi_checkpoint_scenario.json").read_text(encoding="utf-8")
    )
    return BenchmarkScenario.model_validate(payload)


def _binding(integration: str) -> RunBinding:
    return RunBinding(
        benchmark_id="golden-progressive-memory",
        agent_id="openclaw",
        memory_id="openviking",
        memory_integration=integration,
        memory_plugin_id="openclaw-openviking" if integration == "agent_plugin" else None,
        run_id="run-golden",
    )


def test_multi_checkpoint_direct_plan_preserves_stage_boundaries():
    plan = compose_run_plan(
        _scenario(),
        _binding("backend_direct"),
        {
            "memory": {
                "async_ingest": True,
                "commit": {"required_after_ingest": True},
                "readiness": {"supported": True},
            }
        },
    )

    setup_cases = [case for case in plan["cases"] if "phase:setup" in case["labels"]]
    assert [case["source_metadata"]["checkpoint_id"] for case in setup_cases] == [
        "checkpoint-1",
        "checkpoint-2",
    ]
    actions = [
        step["inputs"].get("action")
        for step in plan["steps"]
        if step["operator_kind"] in {"memory", "poll"}
    ]
    assert actions.count("ingest") == 2
    assert actions.count("flush") == 2
    assert len([step for step in plan["steps"] if "wait-ready" in step["step_id"]]) == 2
    second_setup = setup_cases[1]
    assert second_setup["depends_on_cases"] == ["sample-1-q1"]
    scoped_steps = [
        step for step in plan["steps"]
        if step["operator_kind"] == "memory" and step["inputs"].get("action") in {"ingest", "recall"}
    ]
    assert {step["inputs"]["scope_id"] for step in scoped_steps} == {"run-golden:sample-1"}


def test_checkpoint_barrier_finishes_all_commits_before_any_wait():
    payload = _scenario().model_dump(mode="json")
    payload["samples"][0]["timeline"].insert(
        1,
        {
            "event_id": "session-1b",
            "type": "conversation",
            "payload": {"content": "The user also likes hiking."},
        },
    )
    plan = compose_run_plan(
        BenchmarkScenario.model_validate(payload),
        _binding("backend_direct"),
        {
            "memory": {
                "async_ingest": True,
                "commit": {"required_after_ingest": True},
                "readiness": {"supported": True},
            }
        },
    )
    first_stage = [
        step for step in plan["steps"] if step["case_id"] == "sample-1-stage-1-setup"
    ]
    action_order = [
        step["inputs"].get("action", "wait")
        for step in first_stage
        if step["operator_kind"] in {"memory", "poll"}
    ]
    assert action_order == ["ingest", "ingest", "flush", "flush", "wait", "wait"]
    assert all(
        step["inputs"].get("checkpoint_id") == "checkpoint-1"
        for step in first_stage
        if step["operator_kind"] == "memory"
    )


def test_synchronous_runtime_omits_commit_and_wait_ready():
    plan = compose_run_plan(
        _scenario(),
        _binding("backend_direct"),
        {
            "memory": {
                "async_ingest": False,
                "commit": {"required_after_ingest": False},
                "readiness": {"supported": False},
            }
        },
    )

    memory_actions = [
        step["inputs"].get("action")
        for step in plan["steps"]
        if step["operator_kind"] == "memory"
    ]
    assert "flush" not in memory_actions
    assert not any("wait-ready" in step["step_id"] for step in plan["steps"])


def test_async_direct_runtime_waits_for_ingest_without_commit():
    plan = compose_run_plan(
        _scenario(),
        _binding("backend_direct"),
        {
            "memory": {
                "async_ingest": True,
                "commit": {"required_after_ingest": False},
                "readiness": {"supported": True},
            }
        },
    )

    assert not any(
        step["inputs"].get("action") == "flush" for step in plan["steps"]
    )
    waits = [step for step in plan["steps"] if "wait-ready" in step["step_id"]]
    assert len(waits) == 2
    assert all(
        ".output.operation" in step["inputs"]["probe"]["inputs"]["operation"]["$ref"]
        and "-ingest.output.operation" in step["inputs"]["probe"]["inputs"]["operation"]["$ref"]
        for step in waits
    )


def test_multi_checkpoint_plugin_reenters_ingest_after_qa():
    plan = compose_run_plan(
        _scenario(),
        _binding("agent_plugin"),
        {
            "memory_plugin": {
                "commit": {"required_after_ingest": True},
                "readiness": {"supported": True},
            }
        },
    )

    phases = [
        step["inputs"].get("phase")
        for step in plan["steps"]
        if step["operator_kind"] == "memory_plugin"
        and step["inputs"].get("action") == "set_phase"
    ]
    assert phases == ["ingest", "qa", "ingest", "qa"]
    prepare = next(
        step for step in plan["steps"]
        if step["operator_kind"] == "memory_plugin" and step["inputs"].get("action") == "prepare"
    )
    assert prepare["inputs"]["scope_id"] == "run-golden:sample-1"


def test_async_plugin_waits_without_commit():
    plan = compose_run_plan(
        _scenario(),
        _binding("agent_plugin"),
        {
            "memory_plugin": {
                "commit": {"required_after_ingest": False},
                "readiness": {"supported": True},
            }
        },
    )

    plugin_steps = [
        step for step in plan["steps"] if step["operator_kind"] == "memory_plugin"
    ]
    assert not any(step["inputs"].get("action") == "commit" for step in plugin_steps)
    waits = [step for step in plugin_steps if step["inputs"].get("action") == "wait_ready"]
    assert len(waits) == 2
    assert all("operation" not in step["inputs"] for step in waits)
    assert all(step["inputs"].get("session_key") for step in waits)


def test_unknown_evaluation_profile_is_rejected():
    scenario = _scenario()
    scenario.evaluation.profile = "unknown_profile@1"
    with pytest.raises(ValueError, match="unsupported evaluation profile"):
        compose_run_plan(scenario, _binding("backend_direct"), {})


def test_scenario_rejects_duplicate_episode_ids():
    payload = _scenario().model_dump(mode="json")
    payload["samples"].append(payload["samples"][0])
    with pytest.raises(ValueError, match="sample_id values must be unique"):
        BenchmarkScenario.model_validate(payload)
