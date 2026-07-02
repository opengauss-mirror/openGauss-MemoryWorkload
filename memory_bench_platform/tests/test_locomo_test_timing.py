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
                "sample_id,qi,total_tokens,qa_elapsed_seconds,qa_direct_recall_elapsed_seconds,qa_llm_elapsed_seconds,ov_llm_total_tokens,ov_recall_total,ov_missing_records,ov_direct_recall_count,jsonl_filename",
                "conv-1,1,0,3.5,0.4,3.0,100,5,8,4,q1.jsonl",
                "conv-1,2,0,4.5,0.5,3.8,120,4,7,3,q2.jsonl",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "session_ingest_diagnostics.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "accepted",
                        "sample_id": "conv-1",
                        "session_key": "session_1",
                        "query_mode": "direct_ov_stable",
                        "send": {"elapsed_seconds": 2.5, "attempts": 1},
                        "ov_commit": {"session_id": "ov-session-1", "task_id": "task-1"},
                        "accepted_elapsed_seconds": 2.5,
                    }
                ),
                json.dumps(
                    {
                        "event": "completed",
                        "sample_id": "conv-1",
                        "session_key": "session_1",
                        "query_mode": "direct_ov_stable",
                        "ov_commit": {"session_id": "ov-session-1", "task_id": "task-1"},
                        "ov_task_wait": {
                            "elapsed_seconds": 12.0,
                            "poll_count": 3,
                            "final_status": "completed",
                            "timed_out": False,
                        },
                        "ov_consistency": {"ok": True, "missing_record_count": 0},
                        "accepted_elapsed_seconds": 2.5,
                        "session_total_elapsed_seconds": 14.5,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_locomo_test_timing_report(tmp_path)

    assert report["run_id"] == "locomo-demo"
    assert report["source"] == "locomo_test"
    assert report["duration_distributions"]["ingest_seconds"]["max_ms"] == 120000.0
    assert report["duration_distributions"]["ingest_session_span_ms"]["max_ms"] == 60000.0
    assert report["duration_distributions"]["agent.ingest.accepted_ms"]["count"] == 1
    assert report["duration_distributions"]["agent.ingest.accepted_ms"]["max_ms"] == 2500.0
    assert report["duration_distributions"]["ov.ingest.drain_wait_ms"]["max_ms"] == 12000.0
    assert report["duration_distributions"]["ov.ingest.session_total_ms"]["max_ms"] == 14500.0
    assert report["duration_distributions"]["qa.session_total_ms"]["max_ms"] == 4500.0
    assert report["duration_distributions"]["qa.direct_recall_ms"]["max_ms"] == 500.0
    assert report["duration_distributions"]["qa.llm_answer_ms"]["max_ms"] == 3800.0
    assert "agent.ingest.accepted_ms" in report["stage_taxonomy"]["wrapper_labels"]
    assert "agent.ingest.send_ms" in report["stage_taxonomy"]["leaf_labels"]
    assert "qa.direct_recall_ms" in report["stage_taxonomy"]["leaf_labels"]
    assert "pipeline.qa_ms" in report["stage_taxonomy"]["wrapper_labels"]
    assert "ov.ingest.drain_wait_ms" in report["stage_taxonomy"]["wrapper_labels"]
    assert any(event.get("scope") == "qa_session" and event.get("parent_id") == "stage:qa" for event in report["duration_events"])
    assert report["token_summary"]["ov_llm_total_tokens_sum"] == 220
    assert report["recall_summary"]["recall_total_max"] == 5
    html = render_locomo_test_timing_html(report)
    assert "Timing Report" in html
    assert "ingest_seconds" in html
    assert "qa_session" in html
    assert "Parent" in html
    assert "Stage Taxonomy" in html
