from memory_bench_platform.cli import _summarize_native_evaluation
from memory_bench_platform.protocol import CaseRecord, JudgeResult, StepRecord, StepResultRecord


def test_summary_excludes_ungraded_judge_failures_from_benchmark_score():
    cases = [
        CaseRecord(
            case_id="setup",
            run_id="run-1",
            title="setup",
            goal="setup",
            capability="memory/ingest",
            labels=["phase:setup"],
            judge_mode="none",
        ),
        CaseRecord(
            case_id="q1",
            run_id="run-1",
            title="q1",
            goal="answer",
            capability="memory/question-answering",
        ),
        CaseRecord(
            case_id="q2",
            run_id="run-1",
            title="q2",
            goal="answer",
            capability="memory/question-answering",
        ),
    ]
    steps = [
        StepRecord(
            step_id="setup-wait-ready",
            case_id="setup",
            name="wait_ready",
            operator_kind="poll",
        )
    ]
    step_results = [
        StepResultRecord(
            step_result_id="setup-wait-ready-1",
            step_id="setup-wait-ready",
            attempt=1,
            status="passed",
            duration_ms=125,
            gate_passed=True,
        )
    ]
    judges = [
        JudgeResult(judge_id="q1", run_id="run-1", case_id="q1", passed=True, score=1.0),
        JudgeResult(
            judge_id="q2",
            run_id="run-1",
            case_id="q2",
            passed=None,
            score=None,
            label="judge-error",
        ),
    ]

    summary = _summarize_native_evaluation(cases, steps, judges, step_results)

    assert summary["case_total"] == 2
    assert summary["case_passed"] == 1
    assert summary["case_failed"] == 0
    assert summary["case_ungraded"] == 1
    assert summary["benchmark_score"] == 1.0
    assert summary["checkpoint_ready_rate"] == 1.0
    assert summary["readiness_latency_ms"] == 125.0
