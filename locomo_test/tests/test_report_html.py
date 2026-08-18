import csv
import json
from pathlib import Path

from locomo_test.report import write_html_report


def test_write_html_report_generates_summary_and_table(tmp_path: Path):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "meta.json").write_text(
        json.dumps(
            {
                "name": "demo-run",
                "overall_accuracy": 0.7429,
                "total_correct": 26,
                "total_graded": 35,
                "total_questions": 35,
                "memory_mode": "openviking",
                "ov_closure_counts": {
                    "memory_recalled_with_consistency_gap": 27,
                    "no_memory_signal": 5,
                    "token_emitted_only": 3,
                },
                "ov_closure_summary": {
                    "dominant_state": "memory_recalled_with_consistency_gap",
                    "has_memory_written": True,
                    "has_token_emitted": True,
                    "has_index_unavailable": True,
                },
                "memory_token_totals": {
                    "provider": "openviking",
                    "llm_total": 23420,
                    "embedding": 9012,
                    "memories": 20,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "qa_diagnostics.json").write_text(
        json.dumps(
            {
                "issues": {
                    "openviking_index_missing_records_max": 8,
                    "openviking_memory_written_but_index_unavailable": 30,
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with (output_dir / "qa_results.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "qi",
                "question",
                "expected",
                "response",
                "category",
                "result",
                "reasoning",
                "ov_closure_state",
                "ov_recall_total",
                "ov_missing_records",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "qi": "1",
                "question": "When did Caroline go to the LGBTQ support group?",
                "expected": "7 May 2023",
                "response": "Caroline attended the LGBTQ support group on 7 May 2023.",
                "category": "2",
                "result": "CORRECT",
                "reasoning": "",
                "ov_closure_state": "memory_recalled_with_consistency_gap",
                "ov_recall_total": "5",
                "ov_missing_records": "8",
            }
        )
        writer.writerow(
            {
                "qi": "34",
                "question": "What motivated Caroline to pursue counseling?",
                "expected": "her own journey and support she received",
                "response": "No relevant memory is available to answer that.",
                "category": "4",
                "result": "WRONG",
                "reasoning": "missed memory",
                "ov_closure_state": "no_memory_signal",
                "ov_recall_total": "5",
                "ov_missing_records": "8",
            }
        )

    report_path = write_html_report(output_dir)
    html = report_path.read_text(encoding="utf-8")

    assert report_path.name == "report.html"
    assert "demo-run" in html
    assert "74.29%" in html
    assert "memory_recalled_with_consistency_gap" in html
    assert "When did Caroline go to the LGBTQ support group?" in html
    assert "What motivated Caroline to pursue counseling?" in html
