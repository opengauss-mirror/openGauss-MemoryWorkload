from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding
from memory_bench_platform.compatibility import resolve_compatibility
from memory_bench_platform.integration import resolve_run_skill_bundle


def _scenario() -> BenchmarkScenario:
    return BenchmarkScenario.model_validate(
        {
            "benchmark_id": "demo",
            "requirements": {
                "agent": {"multi_turn": True, "stateful_session": True},
                "memory": {"actions": ["ingest", "recall"]},
            },
            "samples": [
                {
                    "sample_id": "sample-1",
                    "timeline": [
                        {
                            "event_id": "session-1",
                            "type": "conversation",
                            "payload": {"content": "remember this"},
                        },
                        {
                            "event_id": "qa",
                            "type": "checkpoint",
                            "evaluation": {
                                "target": "qa_answer",
                                "questions": [
                                    {"question_id": "q1", "question": "what?", "reference": "this"}
                                ],
                            },
                        },
                    ],
                }
            ],
        }
    )


def test_openclaw_openviking_plugin_satisfies_lifecycle_contract():
    bundle = resolve_run_skill_bundle("locomo", "openclaw", "openviking", "agent_plugin")
    result = resolve_compatibility(
        _scenario(),
        RunBinding(
            benchmark_id="demo",
            agent_id="openclaw",
            memory_id="openviking",
            memory_integration="agent_plugin",
            memory_plugin_id="openclaw-openviking",
            run_id="run-1",
        ),
        agent=bundle.agent,
        memory=bundle.memory,
        memory_plugin=bundle.memory_plugin,
    )

    assert result.compatible is True
    assert result.resolved_capabilities["memory_plugin"]["qa_read_only"] is True


def test_compatibility_rejects_plugin_without_scoped_readiness():
    bundle = resolve_run_skill_bundle("locomo", "openclaw", "openviking", "agent_plugin")
    plugin = bundle.memory_plugin.model_copy(deep=True)
    plugin.capabilities["readiness"]["scoped_by_operation"] = False

    result = resolve_compatibility(
        _scenario(),
        RunBinding(
            benchmark_id="demo",
            agent_id="openclaw",
            memory_id="openviking",
            memory_integration="agent_plugin",
            memory_plugin_id="openclaw-openviking",
            run_id="run-1",
        ),
        agent=bundle.agent,
        memory=bundle.memory,
        memory_plugin=plugin,
    )

    assert result.compatible is False
    assert "memory_plugin.readiness.scoped_by_operation" in result.missing_capabilities


def test_synchronous_plugin_does_not_require_commit_or_wait_actions():
    bundle = resolve_run_skill_bundle("locomo", "openclaw", "openviking", "agent_plugin")
    plugin = bundle.memory_plugin.model_copy(deep=True)
    plugin.capabilities["commit"] = {
        "supported": False,
        "required_after_ingest": False,
    }
    plugin.capabilities["readiness"] = {
        "supported": False,
        "scoped_by_operation": False,
    }
    plugin.lifecycle["actions"] = ["validate", "prepare", "set_phase", "finalize"]
    plugin.capabilities["outputs"] = {}

    result = resolve_compatibility(
        _scenario(),
        RunBinding(
            benchmark_id="demo",
            agent_id="openclaw",
            memory_id="openviking",
            memory_integration="agent_plugin",
            memory_plugin_id="openclaw-openviking",
            run_id="run-1",
        ),
        agent=bundle.agent,
        memory=bundle.memory,
        memory_plugin=plugin,
    )

    assert result.compatible is True


def test_backend_direct_requires_episode_scope_support():
    bundle = resolve_run_skill_bundle("locomo", "openclaw", "openviking", "backend_direct")
    memory = bundle.memory.model_copy(deep=True)
    memory.capabilities["scope"]["scoped_recall"] = False
    result = resolve_compatibility(
        _scenario(),
        RunBinding(
            benchmark_id="demo",
            agent_id="openclaw",
            memory_id="openviking",
            memory_integration="backend_direct",
            run_id="run-1",
        ),
        agent=bundle.agent,
        memory=memory,
        memory_plugin=None,
    )
    assert result.compatible is False
    assert "memory.scope.scoped_recall" in result.missing_capabilities


def test_backend_direct_qa_requires_recall_even_when_scenario_omits_action():
    bundle = resolve_run_skill_bundle("locomo", "openclaw", "openviking", "backend_direct")
    scenario = _scenario()
    scenario.requirements["memory"]["actions"] = ["ingest"]
    memory = bundle.memory.model_copy(deep=True)
    memory.capabilities["actions"] = ["ingest", "flush", "status"]
    memory.runtime["actions"] = ["ingest", "flush", "status"]

    result = resolve_compatibility(
        scenario,
        RunBinding(
            benchmark_id="demo",
            agent_id="openclaw",
            memory_id="openviking",
            memory_integration="backend_direct",
            run_id="run-1",
        ),
        agent=bundle.agent,
        memory=memory,
        memory_plugin=None,
    )

    assert result.compatible is False
    assert "memory.actions.recall" in result.missing_capabilities
