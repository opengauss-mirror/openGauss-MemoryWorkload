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
