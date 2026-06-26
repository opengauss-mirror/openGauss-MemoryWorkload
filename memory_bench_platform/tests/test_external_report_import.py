import json
from pathlib import Path

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
    assert result["benchmark_diagnostics"]["artifacts"]["report_html"].endswith("report.html")


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
