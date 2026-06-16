from memory_bench_platform.protocol import (
    CaseRecord,
    ExecutionSpec,
    JudgeInput,
    ReportSummary,
    RunRecord,
    StepRecord,
    StepResultRecord,
    TraceEventRecord,
)


def test_run_record_requires_core_identifiers():
    record = RunRecord(
        run_id="run-001",
        source_id="locomo",
        source_kind="benchmark_case_source",
        benchmark_version_policy={"default_selection": "latest_official_release_tag"},
        version_selection={"benchmark": {"selection_mode": "latest_official_release_tag", "overridden": False}},
        status="pending",
    )
    assert record.run_id == "run-001"
    assert record.source_id == "locomo"
    assert record.benchmark_version_policy["default_selection"] == "latest_official_release_tag"


def test_case_and_step_records_bind_together():
    case = CaseRecord(
        case_id="case-1",
        run_id="run-1",
        title="LoCoMo QA",
        goal="answer question",
        capability="memory/question-answering",
    )
    step = StepRecord(
        step_id="step-1",
        case_id="case-1",
        name="agent_query",
        operator_kind="agent",
    )
    assert step.case_id == case.case_id


def test_protocol_supports_case_execution_and_judge_inputs():
    spec = ExecutionSpec(case_mode="dag", default_retry_limit=1)
    result = StepResultRecord(
        step_result_id="result-1",
        step_id="step-1",
        attempt=1,
        status="passed",
    )
    trace = TraceEventRecord(case_id="case-1", trace_id="trace-1", event_type="step_started")
    judge = JudgeInput(case_id="case-1", reference={"expected_answer": "world"}, step_results=[{"structured_output": {"agent_answer": "hello"}}])
    summary = ReportSummary(run_id="run-1", status="partial", case_total=1, case_passed=0, case_failed=1)
    assert spec.case_mode == "dag"
    assert result.step_id == "step-1"
    assert trace.case_id == "case-1"
    assert judge.reference["expected_answer"] == "world"
    assert summary.case_total == 1
