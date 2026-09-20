import json
from pathlib import Path

import pytest

from memory_bench_platform.external_report_import import import_external_result


def test_import_external_result_reads_meta_json_when_present(tmp_path: Path):
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "overall_accuracy": 0.8571,
                "total_correct": 30,
                "total_graded": 35,
                "total_questions": 35,
                "accuracy_by_category": {"1": {"correct": 5, "total": 5, "accuracy": 1.0}},
                "token_totals": {"total_tokens": 400514},
                "memory_token_totals": {"provider": "openviking"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "qa_results.csv").write_text(
        "sample_id,qi,question,expected,response,category,result,reasoning\n"
        "conv-1,1,Q1,A1,R1,1,CORRECT,ok\n",
        encoding="utf-8",
    )
    result = import_external_result(tmp_path)
    assert result["source"] == "locomo_test"
    assert result["summary"]["overall_accuracy"] == 0.8571
    assert result["summary"]["total_correct"] == 30
    assert result["case_results"][0]["passed"] is True


def test_import_external_result_reads_locomo_diagnostics_when_present(tmp_path: Path):
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "overall_accuracy": 0.7429,
                "total_correct": 26,
                "total_graded": 35,
                "total_questions": 35,
                "qa_reindex": {"ok": True, "target_uri": "viking://user/eval-1/memories"},
                "ov_closure_summary": {
                    "dominant_state": "memory_recalled_with_consistency_gap",
                    "has_memory_written": True,
                },
                "ov_closure_counts": {
                    "memory_recalled_with_consistency_gap": 27,
                    "no_memory_signal": 5,
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "qa_diagnostics.json").write_text(
        json.dumps(
            {
                "issues": {
                    "openviking_memory_written_but_index_unavailable": 30,
                },
                "ov_closure_summary": {
                    "dominant_state": "memory_recalled_with_consistency_gap",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "report.html").write_text("<html>demo</html>", encoding="utf-8")
    (tmp_path / "qa_results.csv").write_text(
        "sample_id,qi,question,expected,response,category,result,reasoning\n"
        "conv-1,1,Q1,A1,R1,1,CORRECT,ok\n",
        encoding="utf-8",
    )

    result = import_external_result(tmp_path)

    assert result["benchmark_diagnostics"]["source"] == "locomo_test"
    assert result["benchmark_diagnostics"]["ov_closure_counts"]["no_memory_signal"] == 5
    assert result["benchmark_diagnostics"]["issues"]["openviking_memory_written_but_index_unavailable"] == 30
    assert result["benchmark_diagnostics"]["qa_reindex"]["ok"] is True
    assert result["benchmark_diagnostics"]["artifacts"]["report_html"].endswith("report.html")


def test_import_external_result_marks_invalid_locomo_run_when_memory_extraction_missing(tmp_path: Path):
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "overall_accuracy": 0.0,
                "total_correct": 0,
                "total_graded": 35,
                "total_questions": 35,
                "memory_token_totals": {
                    "provider": "openviking",
                    "llm_total": 0,
                    "embedding": 0,
                    "memories": 0,
                },
                "ov_closure_summary": {
                    "dominant_state": "qa_direct_recall_only",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "qa_diagnostics.json").write_text(
        json.dumps(
            {
                "issues": {
                    "openviking_tokens_all_zero": 35,
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "qa_results.csv").write_text(
        "sample_id,qi,question,expected,response,category,result,reasoning\n"
        "conv-1,1,Q1,A1,R1,1,WRONG,bad\n",
        encoding="utf-8",
    )

    result = import_external_result(tmp_path)

    assert result["summary"]["run_validity"]["valid"] is False
    assert result["summary"]["run_validity"]["reasons"] == [
        "openviking_memory_extraction_unavailable"
    ]
    assert result["benchmark_diagnostics"]["run_validity"]["valid"] is False


def test_import_external_result_falls_back_to_csv_only(tmp_path: Path):
    (tmp_path / "phaseA_demo.csv").write_text(
        "sample_id,qi,question,expected,response,category,result,reasoning\n"
        "conv-1,1,Q1,A1,R1,1,CORRECT,ok\n"
        "conv-1,2,Q2,A2,R2,2,WRONG,bad\n",
        encoding="utf-8",
    )
    result = import_external_result(tmp_path)
    assert result["source"] == "csv_result"
    assert result["summary"]["total_correct"] == 1
    assert result["summary"]["total_graded"] == 2
    assert result["summary"]["accuracy_by_category"]["1"]["accuracy"] == 1.0
    assert result["summary"]["accuracy_by_category"]["2"]["accuracy"] == 0.0


def test_import_external_result_keeps_ungraded_rows_visible(tmp_path: Path):
    (tmp_path / "phaseA_demo.csv").write_text(
        "sample_id,qi,question,expected,response,category,result,reasoning\n"
        "conv-1,1,Q1,A1,R1,1,CORRECT,ok\n"
        "conv-1,2,Q2,A2,R2,2,,\n",
        encoding="utf-8",
    )
    result = import_external_result(tmp_path)
    assert result["summary"]["total_questions"] == 2
    assert result["summary"]["total_graded"] == 1
    assert result["summary"]["ungraded_count"] == 1
    assert len(result["case_results"]) == 2
    assert result["case_results"][1]["label"] == "ungraded"
    assert result["case_results"][1]["passed"] is False


def test_import_external_result_fills_missing_csv_row_from_phase_meta(tmp_path: Path):
    phase_meta_rows = [
        {
            "sample_id": "conv-1",
            "qi": "1",
            "question": "Q1",
            "expected": "A1",
            "response": "R1",
            "category": "1",
        },
        {
            "sample_id": "conv-1",
            "qi": "2",
            "question": "Q2",
            "expected": "A2",
            "response": "R2",
            "category": "2",
        },
    ]
    (tmp_path / "phaseA_meta.json").write_text(
        json.dumps({"qa_rows": phase_meta_rows}),
        encoding="utf-8",
    )
    (tmp_path / "qa_results.csv").write_text(
        "sample_id,qi,question,expected,response,category,result,reasoning\n"
        "conv-1,1,Q1,A1,R1,1,CORRECT,ok\n",
        encoding="utf-8",
    )
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "overall_accuracy": 0.5,
                "total_correct": 1,
                "total_graded": 1,
                "total_questions": 2,
            }
        ),
        encoding="utf-8",
    )

    result = import_external_result(tmp_path)

    assert len(result["case_results"]) == 2
    assert result["summary"]["total_questions"] == 2
    assert result["summary"]["ungraded_count"] == 1
    assert result["case_results"][1]["label"] == "ungraded"
    assert result["case_results"][1]["case_id"] == "conv-1-q2"


def _write_replay_summary(path: Path, **overrides):
    payload = {
        "schema": "production-replay-summary/1",
        "run_id": "run-1",
        "dataset_state": "partially_written",
        "model_mode": "real-model",
        "operations": {
            "add": {"count": 2, "success": 1, "success_rate": 0.5},
            "search": {
                "count": 1,
                "success": 1,
                "success_rate": 1.0,
                "non_empty_rate": 1.0,
            },
        },
        "join_coverage": {
            "client_requests": 3,
            "missing_internal_traces": 1,
            "duplicate_request_ids": 0,
        },
        "attribution_status": "exploratory",
    }
    payload.update(overrides)
    (path / "production_replay_summary.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_replay_events(path: Path, rows: list[dict]):
    (path / "request_events.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


def test_import_external_result_reads_production_replay_contract(tmp_path: Path):
    _write_replay_summary(tmp_path)
    _write_replay_events(
        tmp_path,
        [
            {
                "request_id": "add-1",
                "operation": "add",
                "status": "ok",
                "state": "completed",
            },
            {
                "request_id": "add-2",
                "operation": "add",
                "status": "failed",
                "state": "failed",
                "error_type": "TimeoutError",
            },
            {
                "request_id": "search-1",
                "operation": "search",
                "status": "ok",
                "state": "completed",
                "result_count": 2,
            },
        ],
    )
    imported = import_external_result(tmp_path)
    assert imported["source"] == "production_http_replay"
    assert imported["summary"]["total_questions"] == 3
    assert imported["summary"]["total_correct"] == 2
    assert imported["summary"]["run_validity"]["valid"] is False
    assert imported["benchmark_diagnostics"]["dataset_state"] == "partially_written"


def test_production_replay_import_requires_events_and_known_schema(tmp_path: Path):
    _write_replay_summary(tmp_path)
    with pytest.raises(FileNotFoundError, match="request_events.jsonl"):
        import_external_result(tmp_path)

    _write_replay_events(tmp_path, [])
    _write_replay_summary(tmp_path, schema="unknown/1")
    with pytest.raises(ValueError, match="unsupported production replay schema"):
        import_external_result(tmp_path)


def test_production_replay_import_rejects_malformed_and_duplicate_events(tmp_path: Path):
    _write_replay_summary(tmp_path)
    (tmp_path / "request_events.jsonl").write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        import_external_result(tmp_path)

    duplicate = {
        "request_id": "same",
        "operation": "add",
        "status": "ok",
        "state": "completed",
    }
    _write_replay_events(tmp_path, [duplicate, duplicate])
    with pytest.raises(ValueError, match="duplicate request_id"):
        import_external_result(tmp_path)


@pytest.mark.parametrize("forbidden", ["raw_request", "messages", "query", "memories"])
def test_production_replay_import_rejects_content_fields(
    tmp_path: Path, forbidden: str
):
    _write_replay_summary(
        tmp_path,
        dataset_state="complete",
        operations={
            "add": {"count": 1, "success": 1},
            "search": {"count": 0, "success": 0},
        },
    )
    _write_replay_events(
        tmp_path,
        [
            {
                "request_id": "add-1",
                "operation": "add",
                "status": "ok",
                "state": "completed",
                forbidden: "secret",
            }
        ],
    )
    with pytest.raises(ValueError, match="forbidden event field"):
        import_external_result(tmp_path)
