from memory_bench_platform.protocol import (
    ExecutionSpec,
    JudgeInput,
    RenderedTaskInput,
    RunRecord,
    TaskRecord,
    TurnRecord,
)


def test_run_record_requires_core_identifiers():
    record = RunRecord(
        run_id="run-001",
        benchmark_id="locomo",
        agent_id="openclaw",
        status="pending",
    )
    assert record.run_id == "run-001"
    assert record.benchmark_id == "locomo"
    assert record.agent_id == "openclaw"


def test_turn_record_binds_to_task():
    turn = TurnRecord(
        turn_id="turn-1",
        task_id="task-1",
        index=0,
        role="user",
        content="hello",
    )
    assert turn.task_id == "task-1"


def test_protocol_supports_explicit_execution_and_judge_inputs():
    spec = ExecutionSpec(mode="multi_turn", requires_stateful_agent=True)
    rendered = RenderedTaskInput(task_id="task-1", messages=[{"role": "user", "content": "hello"}])
    judge = JudgeInput(task_id="task-1", expected_answer="world", agent_answer="hello")
    task = TaskRecord(task_id="task-1", run_id="run-1", sample_id="sample-1")
    assert spec.mode == "multi_turn"
    assert rendered.task_id == task.task_id
    assert judge.expected_answer == "world"
