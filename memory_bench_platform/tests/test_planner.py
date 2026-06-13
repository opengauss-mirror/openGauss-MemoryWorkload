from memory_bench_platform.planner import RunPlanRequest, build_run_plan


def test_build_run_plan_resolves_selected_benchmark_and_agent():
    request = RunPlanRequest(
        benchmark_id="locomo",
        agent_id="openclaw",
        benchmark_version="0.1.0",
        agent_version="0.1.0",
    )
    plan = build_run_plan(request)
    assert plan.run_id.startswith("locomo-openclaw-")
    assert plan.benchmark_id == "locomo"
