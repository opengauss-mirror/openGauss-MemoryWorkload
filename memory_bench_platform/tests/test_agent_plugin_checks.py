"""Optional plugin checks bracket Agent calls without coupling the Agent runner."""
import pytest

from memory_bench_platform.protocol import (
    MemoryPluginTaskOutput, StepRecord, WorkflowRuntimeContext,
)
from memory_bench_platform.workflow_operators import _execute_agent


def context(mode="agent_plugin", actions=None):
    return WorkflowRuntimeContext(
        run_id="run", run_dir="/tmp/run", benchmark_id="locomo", agent_id="openclaw",
        memory_integration=mode, memory_plugin_id="test-plugin",
        run_contract={"memory_plugin_runtime": {"actions": actions or []}},
    )


def step():
    return StepRecord(step_id="qa", case_id="case", name="answer", operator_kind="agent",
                      inputs={"messages": [{"role": "user", "content": "question"}]})


@pytest.mark.parametrize("failure", [None, "before_agent", "after_agent"])
def test_checks_bracket_call_and_reject_failures(failure):
    seen = []
    def plugin(plugin_id, request):
        assert plugin_id == "test-plugin"
        seen.append(request.action)
        assert request.inputs["agent_request"]["metadata"]["step_id"] == "qa"
        if request.action == "after_agent":
            assert request.inputs["check_context"] == {"marker": "first-call"}
            assert request.inputs["agent_result"]["turns"][0]["text"] == "answer"
        return MemoryPluginTaskOutput(
            status="failed" if failure == request.action else "ok",
            state="failed" if failure == request.action else "completed",
            output={"marker": "first-call"}, error={"message": "guard rejected"},
        )
    def agent(*args):
        seen.append("agent")
        return {"status": "ok", "turns": [{"text": "answer"}]}
    ctx = context(actions=["before_agent", "after_agent"])
    result = _execute_agent(step(), "openclaw", agent, ctx, plugin)
    if failure:
        assert result["status"] == "failed"
        assert "guard rejected" in result["error_message"]
        assert result["plugin_checks"][failure]["state"] == "failed"
    else:
        assert result["status"] == "ok"
    if failure != "before_agent":
        assert result["agent_answer"] == "answer"
    assert seen == (["before_agent"] if failure == "before_agent" else
                    ["before_agent", "agent", "after_agent"])


@pytest.mark.parametrize("mode,actions", [
    ("backend_direct", ["before_agent", "after_agent"]), ("agent_plugin", []),
])
def test_unrelated_modes_do_not_call_checks(mode, actions):
    def plugin(*args):
        pytest.fail("undeclared/unbound plugin check invoked")
    result = _execute_agent(step(), "openclaw",
        lambda *a: {"status": "ok", "turns": [{"text": "answer"}]},
        context(mode, actions), plugin)
    assert result["agent_answer"] == "answer"


def test_noncompleted_before_check_does_not_start_agent():
    def agent(*args): pytest.fail("agent started before check completed")
    result = _execute_agent(step(), "openclaw", agent, context(actions=["before_agent"]),
                            lambda *a: MemoryPluginTaskOutput(status="ok", state="accepted"))
    assert result["status"] == "failed" and "accepted" in result["error_message"]
