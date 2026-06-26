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
    (run_dir / "artifacts" / "monitor" / "mem_status.csv").write_text(
        "timestamp,mem_free_mb,mem_used_mb\n"
        "1,1000.0,2000.0\n"
        "2,900.0,2100.0\n",
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
    (artifacts / "qa_results.csv").write_text(
        "\n".join(
            [
                "sample_id,qi,question,expected,response,category,result,reasoning",
                "conv-1,1,Q1,A1,R1,1,CORRECT,ok",
                "conv-1,2,Q2,A2,There is no mention of that in the memory.,1,WRONG,bad",
            ]
        ),
        encoding="utf-8",
    )
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
    (artifacts / "meta.json").write_text(
        json.dumps(
            {
                "overall_accuracy": 0.5,
                "total_correct": 1,
                "total_graded": 2,
                "total_questions": 2,
                "ov_closure_summary": {
                    "dominant_state": "memory_recalled_with_consistency_gap",
                },
                "ov_closure_counts": {
                    "memory_recalled_with_consistency_gap": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    (artifacts / "qa_diagnostics.json").write_text(
        json.dumps(
            {
                "issues": {
                    "openviking_memory_written_but_index_unavailable": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    (artifacts / "report.html").write_text("<html>locomo</html>", encoding="utf-8")

    analysis = analyze_run(run_dir)

    assert analysis["overall_accuracy"] == 0.5
    assert analysis["failure_summary"]["retrieval_miss_count"] == 1
    assert analysis["resource_summary"]["cpu_user_peak"] == 20.0
    assert analysis["resource_summary"]["mem_used_peak_mb"] == 2100.0
    assert analysis["ingest_summary"]["session_total"] == 2
    assert analysis["ingest_summary"]["zero_memory_sessions"] == 1
    assert analysis["benchmark_diagnostics"]["source"] == "locomo_test"
    assert (run_dir / "reports" / "analysis.json").is_file()
    assert (run_dir / "reports" / "analysis.md").is_file()
    assert (run_dir / "reports" / "run_report.html").is_file()
    assert (run_dir / "reports" / "timing_report.json").is_file()
    assert (run_dir / "reports" / "timing_report.html").is_file()
    report_html = (run_dir / "reports" / "run_report.html").read_text(encoding="utf-8")
    assert "memory_recalled_with_consistency_gap" in report_html
    assert "openviking_memory_written_but_index_unavailable" in report_html


def test_analyze_run_refreshes_external_result_and_summary_from_external_artifacts(tmp_path: Path):
    run_dir = tmp_path / "run-refresh"
    _build_minimal_run(run_dir)
    (run_dir / "records").mkdir(parents=True, exist_ok=True)
    (run_dir / "records" / "external_entrypoint.json").write_text(
        json.dumps({"entrypoint_id": "official_small", "benchmark_id": "locomo", "agent_id": "openclaw"}),
        encoding="utf-8",
    )
    artifacts = run_dir / "external_artifacts" / "official_small"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "qa_results.csv").write_text(
        "\n".join(
            [
                "sample_id,qi,question,expected,response,category,result,reasoning",
                "conv-1,1,Q1,A1,R1,1,CORRECT,ok",
                "conv-1,2,Q2,A2,R2,1,WRONG,bad",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "reports" / "external_result_summary.json").write_text(
        json.dumps({"source": "csv_result", "summary": {"total_questions": 2, "total_graded": 0, "total_correct": 0}, "case_results": []}),
        encoding="utf-8",
    )

    analysis = analyze_run(run_dir)
    summary = json.loads((run_dir / "reports" / "summary.json").read_text(encoding="utf-8"))
    external = json.loads((run_dir / "reports" / "external_result_summary.json").read_text(encoding="utf-8"))
    case_results = json.loads((run_dir / "reports" / "case_results.json").read_text(encoding="utf-8"))

    assert analysis["overall_accuracy"] == 0.5
    assert summary["status"] == "failed"
    assert summary["case_total"] == 2
    assert summary["case_passed"] == 1
    assert summary["case_failed"] == 1
    assert external["summary"]["total_graded"] == 2
    assert len(case_results) == 2


def test_analyze_run_writes_timing_report_for_official_sample0(tmp_path: Path):
    run_dir = tmp_path / "run-sample0"
    _build_minimal_run(run_dir)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "source_id": "locomo:official_sample0",
                "source_kind": "external_benchmark_runner",
                "agent_id": "openclaw",
                "status": "passed",
            }
        ),
        encoding="utf-8",
    )
    remote_logs = run_dir / "external_artifacts" / "official_sample0" / "remote_logs"
    remote_logs.mkdir(parents=True, exist_ok=True)
    (remote_logs / "sample0.master.log").write_text(
        "[phaseA][session 1/19][direct-ov] session_1 task=t1 session_id=s1 memories=3\n",
        encoding="utf-8",
    )
    artifacts = run_dir / "external_artifacts" / "official_sample0"
    (artifacts / "phaseA_on_19sessions_run-sample0_meta.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "plugin_namespace_config": {"final": {}},
                "ingest_sessions": [
                    {
                        "index": 1,
                        "compact_elapsed_seconds": 3.0,
                        "ov_observation": {
                            "detail": {
                                "created_at": "2026-06-15T18:00:00.000Z",
                                "updated_at": "2026-06-15T18:00:04.000Z",
                                "llm_token_usage": {"total_tokens": 80},
                                "_ov_task": {
                                    "created_at": "2026-06-15T18:00:01.000Z",
                                    "updated_at": "2026-06-15T18:00:03.000Z",
                                    "result": {
                                        "telemetry_summary": {
                                            "operation": "session_commit_phase2",
                                            "duration_ms": 2000,
                                        }
                                    },
                                },
                            }
                        },
                    }
                ],
                "qa_rows": [
                    {
                        "qi": 109,
                        "question": "Q109",
                        "response": "A109",
                        "elapsed_seconds": 6.0,
                        "usage": {"total_tokens": 120},
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

    assert analysis["timing_report"]["duration_label_count"] >= 1
    assert (run_dir / "reports" / "timing_report.json").is_file()
    assert (run_dir / "reports" / "timing_report.html").is_file()


def test_analyze_run_tolerates_missing_cpu_monitor_file(tmp_path: Path):
    run_dir = tmp_path / "run-2"
    _build_minimal_run(run_dir)
    (run_dir / "artifacts" / "monitor" / "cpu_status.csv").unlink()
    (run_dir / "artifacts" / "monitor" / "mem_status.csv").unlink()
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


def test_analyze_run_falls_back_to_external_monitor_files(tmp_path: Path):
    run_dir = tmp_path / "run-fallback-monitor"
    _build_minimal_run(run_dir)
    (run_dir / "artifacts" / "monitor" / "cpu_status.csv").unlink()
    (run_dir / "artifacts" / "monitor" / "mem_status.csv").unlink()
    artifacts = run_dir / "external_artifacts" / "locomo_test_remote" / "monitor"
    artifacts.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "source_id": "locomo:locomo_test_remote",
                "source_kind": "external_benchmark_runner",
                "agent_id": "openclaw",
                "status": "partial",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "records").mkdir(parents=True, exist_ok=True)
    (run_dir / "records" / "external_entrypoint.json").write_text(
        json.dumps({"entrypoint_id": "locomo_test_remote"}),
        encoding="utf-8",
    )
    (artifacts / "cpu_status.csv").write_text(
        "timestamp,summary_util_user,summary_util_sys,summary_util_idle\n"
        "1,11.0,4.0,85.0\n"
        "2,21.0,8.0,71.0\n",
        encoding="utf-8",
    )
    (artifacts / "mem_status.csv").write_text(
        "timestamp,mem_free_mb,mem_used_mb\n"
        "1,800.0,2200.0\n"
        "2,750.0,2250.0\n",
        encoding="utf-8",
    )

    analysis = analyze_run(run_dir)

    assert analysis["resource_summary"]["cpu_user_peak"] == 21.0
    assert analysis["resource_summary"]["mem_used_peak_mb"] == 2250.0


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
