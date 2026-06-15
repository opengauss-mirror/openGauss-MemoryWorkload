import json
from pathlib import Path

from memory_bench_platform.official_small_timing import (
    build_official_small_timing_report,
    render_official_small_timing_html,
)


def test_build_official_small_timing_report_extracts_duration_events(tmp_path: Path):
    run_dir = tmp_path / "run"
    artifacts = run_dir / "external_artifacts" / "official_small"
    remote_logs = artifacts / "remote_logs"
    artifacts.mkdir(parents=True)
    remote_logs.mkdir(parents=True)
    meta = {
        "run_id": "demo-run",
        "ingest_sessions": [
            {
                "index": 1,
                "compact_elapsed_seconds": 10.0,
                "ov_observation": {
                    "detail": {
                        "created_at": "2026-06-15T09:15:10.000Z",
                        "updated_at": "2026-06-15T09:15:22.500Z",
                        "llm_token_usage": {"total_tokens": 100},
                        "embedding_token_usage": {"total_tokens": 20},
                        "_ov_task": {
                            "task_id": "task-1",
                            "status": "completed",
                            "created_at_iso": "2026-06-15T09:15:10+00:00",
                            "updated_at_iso": "2026-06-15T09:15:21+00:00",
                        },
                    }
                },
                "wm_preprocess": {
                    "status": "active",
                    "structured_facts_count": 3,
                    "metrics": {"selected_span_count": 4, "selected_span_tokens_est": 120},
                },
                "telemetry_summary": {
                    "duration_ms": 842.3,
                    "operation": "session_commit_phase2",
                    "status": "ok",
                    "embedding": {
                        "async": {
                            "wait_ms": 6.0,
                            "duration_ms": 18.5,
                            "max_concurrent": 8,
                        }
                    },
                    "memory": {
                        "extract": {
                            "stages": {
                                "llm_extract_ms": 410.2,
                            },
                        }
                    },
                    "storage": {
                        "read_file": {"messages_jsonl": {"duration_ms": 4.0}},
                        "write_file": {"archive_done": {"duration_ms": 1.5}},
                    },
                    "session": {
                        "commit": {
                            "phase2": {"wait_for_request": {"duration_ms": 120.0}}
                        }
                    },
                },
            }
        ],
        "qa_rows": [
            {
                "qi": 2,
                "question": "Q1",
                "elapsed_seconds": 5.0,
                "usage": {"total_tokens": 240, "input_tokens": 180, "output_tokens": 60, "cacheRead": 33},
            }
        ],
    }
    (artifacts / "phaseA_demo_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (remote_logs / "demo.ov.log").write_text(
        '\n'.join(
            [
                '2026-06-16 07:00:00 INFO openviking.telemetry.execution OV_TELEMETRY_SUMMARY '
                + json.dumps(
                    {
                        "telemetry_id": "tele-1",
                        "operation": "search.find",
                        "summary": {
                            "operation": "search.find",
                            "duration_ms": 91.2,
                            "search": {
                                "embed_query": {"duration_ms": 11.3},
                                "vector_retrieval": {"duration_ms": 55.4},
                            },
                            "embedding": {"async": {"wait_ms": 2.5, "duration_ms": 7.5}},
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        encoding="utf-8",
    )

    report = build_official_small_timing_report(run_dir)

    assert report["run_id"] == "demo-run"
    assert report["ingest_session_count"] == 1
    assert report["question_count"] == 1
    assert "ov.session.commit.total_ms" in report["duration_distributions"]
    assert "ov.commit.task.total_ms" in report["duration_distributions"]
    assert "ov.commit.phase2.total_ms" in report["duration_distributions"]
    assert "ov.session.window_ms" in report["duration_distributions"]
    assert "agent.qa.total_ms" in report["duration_distributions"]
    assert "ov.memory.extract.stage.llm_extract_ms" in report["duration_distributions"]
    assert "ov.embedding.async.wait_ms" in report["duration_distributions"]
    assert "ov.embedding.async.duration_ms" in report["duration_distributions"]
    assert "ov.storage.read_file.messages_jsonl_ms" in report["duration_distributions"]
    assert "ov.storage.write_file.archive_done_ms" in report["duration_distributions"]
    assert "ov.session.commit.phase2.wait_for_request_ms" in report["duration_distributions"]
    assert "ov.search.find.total_ms" in report["duration_distributions"]
    assert report["token_summary"]["ingest"]["ov_llm_total_tokens"] == 100
    assert report["wm_preprocess_summary"]["selected_span_count_total"] == 4
    html = render_official_small_timing_html(report)
    assert "Timing Report" in html
    assert "ov.session.commit.total_ms" in html
