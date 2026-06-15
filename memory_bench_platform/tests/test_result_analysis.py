import json
from pathlib import Path

from memory_bench_platform.cli import main
from memory_bench_platform.result_analysis import analyze_run, classify_failure


def _build_minimal_run(run_dir: Path) -> None:
    (run_dir / "reports").mkdir(parents=True)
    (run_dir / "artifacts" / "monitor").mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "source_id": "locomo:official_small",
                "source_kind": "external_benchmark_runner",
                "agent_id": "openclaw",
                "status": "failed",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "reports" / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "status": "failed",
                "case_total": 2,
                "case_passed": 1,
                "case_failed": 1,
                "category_summary": {"1": {"correct": 0, "total": 1, "accuracy": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "reports" / "case_results.json").write_text(
        json.dumps(
            [
                {
                    "case_id": "conv-1-q1",
                    "question": "When did Alice join the support group?",
                    "expected_answer": "March 12",
                    "response": "There is no mention of that in the memory.",
                    "category": "1",
                    "passed": False,
                },
                {
                    "case_id": "conv-1-q2",
                    "question": "Where did Alice go?",
                    "expected_answer": "Paris",
                    "response": "Paris",
                    "category": "1",
                    "passed": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "artifacts" / "monitor" / "cpu_status.csv").write_text(
        "timestamp,summary_util_user,summary_util_sys,summary_util_idle\n"
        "1,10.0,5.0,85.0\n"
        "2,20.0,7.0,73.0\n",
        encoding="utf-8",
    )


def _write_master_log(run_dir: Path, text: str) -> None:
    remote_logs = run_dir / "external_artifacts" / "official_small" / "remote_logs"
    remote_logs.mkdir(parents=True, exist_ok=True)
    (remote_logs / f"{run_dir.name}.master.log").write_text(text, encoding="utf-8")


def test_analyze_run_writes_analysis_json_and_md(tmp_path: Path):
    run_dir = tmp_path / "run-1"
    _build_minimal_run(run_dir)
    _write_master_log(
        run_dir,
        "[phaseA][session 1/4][direct-ov] session_1 task=t1 session_id=s1 memories=0\n"
        "[phaseA][session 2/4][direct-ov] session_2 task=t2 session_id=s2 memories=7\n",
    )
    artifacts = run_dir / "external_artifacts" / "official_small"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "phaseA_demo_meta.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "plugin_namespace_config": {"final": {}},
                "ingest_sessions": [
                    {
                        "index": 1,
                        "compact_elapsed_seconds": 10.0,
                        "ov_observation": {
                            "detail": {
                                "created_at": "2026-06-15T09:15:10.000Z",
                                "updated_at": "2026-06-15T09:15:22.000Z",
                                "llm_token_usage": {"total_tokens": 100},
                                "embedding_token_usage": {"total_tokens": 20},
                            }
                        },
                    }
                ],
                "qa_rows": [
                    {
                        "qi": 1,
                        "question": "Q1",
                        "response": "A1",
                        "elapsed_seconds": 5.0,
                        "usage": {"total_tokens": 200},
                        "openclaw_session_ledger": {"found": True},
                    }
                ],
                "ov_log_tail": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    analysis = analyze_run(run_dir)

    assert analysis["overall_accuracy"] == 0.5
    assert analysis["failure_summary"]["retrieval_miss_count"] == 1
    assert analysis["resource_summary"]["cpu_user_peak"] == 20.0
    assert analysis["ingest_summary"]["session_total"] == 2
    assert analysis["ingest_summary"]["zero_memory_sessions"] == 1
    assert (run_dir / "reports" / "analysis.json").is_file()
    assert (run_dir / "reports" / "analysis.md").is_file()
    assert (run_dir / "reports" / "timing_report.json").is_file()
    assert (run_dir / "reports" / "timing_report.html").is_file()


def test_analyze_run_tolerates_missing_cpu_monitor_file(tmp_path: Path):
    run_dir = tmp_path / "run-2"
    _build_minimal_run(run_dir)
    (run_dir / "artifacts" / "monitor" / "cpu_status.csv").unlink()
    (run_dir / "reports" / "case_results.json").write_text(
        json.dumps(
            [
                {
                    "case_id": "c1",
                    "question": "Q",
                    "expected_answer": "A",
                    "response": "",
                    "passed": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "reports" / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "status": "partial",
                "case_total": 1,
                "case_passed": 0,
                "case_failed": 1,
                "category_summary": {},
            }
        ),
        encoding="utf-8",
    )

    analysis = analyze_run(run_dir)

    assert analysis["failure_summary"]["format_or_empty_count"] == 1
    assert analysis["resource_summary"]["cpu_sample_count"] == 0


def test_cli_analyze_run_generates_analysis_files(tmp_path: Path):
    run_dir = tmp_path / "run-3"
    _build_minimal_run(run_dir)

    main(["analyze-run", "--run-dir", str(run_dir)])

    assert (run_dir / "reports" / "analysis.json").is_file()
    assert (run_dir / "reports" / "analysis.md").is_file()


def test_classify_failure_treats_explicit_no_info_variants_as_retrieval_miss():
    samples = [
        "The recalled memories don't mention a charity race.",
        "Based on the recalled memories, I don't have information about Melanie's camping plans.",
        "Based on the recalled memories, there is no explicit information about how Melanie prioritizes self-care.",
        "Based on the recalled memories, there isn't explicit information about a specific kind of place she wants to create.",
        "Based on the recalled memories, there's no explicit mention of Caroline pursuing counseling or what motivated her to do so.",
    ]

    for response in samples:
        bucket, reason = classify_failure(
            {
                "question": "Q",
                "response": response,
                "passed": False,
            }
        )
        assert bucket == "retrieval_miss", (response, bucket, reason)


def test_classify_failure_treats_auth_errors_as_operator_failure():
    bucket, reason = classify_failure(
        {
            "question": "Q",
            "response": "",
            "passed": False,
            "error_detail": "Error: Error code: 401 - {'error': {'message': 'Missing Authentication header', 'code': 401}}",
        }
    )
    assert bucket == "operator_error"
    assert "auth" in reason.lower() or "401" in reason
