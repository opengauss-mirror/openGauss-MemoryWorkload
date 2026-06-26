import json
from pathlib import Path

from memory_bench_platform.locomo_test_timing import (
    build_locomo_test_timing_report,
    render_locomo_test_timing_html,
)


def test_build_locomo_test_timing_report_extracts_step_and_recall_stats(tmp_path: Path):
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "name": "locomo-demo",
                "memory_token_totals": {
                    "embedding": 90,
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".ingest_record.json").write_text(
        json.dumps(
            {
                "s1": {"timestamp": 100},
                "s2": {"timestamp": 160},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pipeline.log").write_text(
        "\n".join(
            [
                "  [health_check] done in 10.0s",
                "  [ingest] done in 120.0s",
                "  [qa] done in 80.0s",
                "  [judge] done in 20.0s",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "qa_results.csv").write_text(
        "\n".join(
            [
                "sample_id,qi,total_tokens,ov_llm_total_tokens,ov_recall_total,ov_missing_records",
                "conv-1,1,0,100,5,8",
                "conv-1,2,0,120,4,7",
            ]
        ),
        encoding="utf-8",
    )

    report = build_locomo_test_timing_report(tmp_path)

    assert report["run_id"] == "locomo-demo"
    assert report["source"] == "locomo_test"
    assert report["duration_distributions"]["ingest_seconds"]["max_ms"] == 120000.0
    assert report["duration_distributions"]["ingest_session_span_ms"]["max_ms"] == 60000.0
    assert report["token_summary"]["ov_llm_total_tokens_sum"] == 220
    assert report["recall_summary"]["recall_total_max"] == 5
    html = render_locomo_test_timing_html(report)
    assert "Timing Report" in html
    assert "ingest_seconds" in html
