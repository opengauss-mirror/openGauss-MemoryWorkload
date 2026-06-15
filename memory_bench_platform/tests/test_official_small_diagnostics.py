import json
from pathlib import Path

from memory_bench_platform.official_small_diagnostics import diagnose_official_small_run


def test_diagnose_official_small_extracts_node_summary_and_timing(tmp_path: Path):
    run_dir = tmp_path / "run"
    artifacts = run_dir / "external_artifacts" / "official_small"
    logs = artifacts / "remote_logs"
    logs.mkdir(parents=True)
    meta = {
        "run_id": "demo-run",
        "plugin_namespace_config": {
            "final": {
                "isolateUserScopeByAgent": False,
                "isolateAgentScopeByUser": False,
                "userId": "user-demo",
                "accountId": "acct-demo",
                "agent_prefix": "acct-demo",
            }
        },
        "ingest_sessions": [
            {
                "index": 1,
                "compact_elapsed_seconds": 10.0,
                "ov_session_id": "s1",
                "compact_status": {"commit_status": "completed"},
                "ov_observation": {
                    "poll_ok": True,
                    "detail": {
                        "commit_count": 1,
                        "memories_extracted": {"total": 0},
                        "llm_token_usage": {"total_tokens": 100},
                        "embedding_token_usage": {"total_tokens": 20},
                    },
                },
            },
            {
                "index": 2,
                "compact_elapsed_seconds": 12.0,
                "ov_session_id": "s2",
                "compact_status": {"commit_status": "completed"},
                "ov_observation": {
                    "poll_ok": True,
                    "detail": {
                        "commit_count": 1,
                        "memories_extracted": {"total": 3},
                        "llm_token_usage": {"total_tokens": 120},
                        "embedding_token_usage": {"total_tokens": 30},
                    },
                },
            },
        ],
        "qa_rows": [
            {
                "qi": 2,
                "question": "Q1",
                "expected": "A1",
                "response": "No information.",
                "category": 2,
                "elapsed_seconds": 5.0,
                "usage": {"total_tokens": 200},
                "openclaw_session_ledger": {"found": False, "message_count": 0, "error": "session_file_not_found"},
            },
            {
                "qi": 3,
                "question": "Q2",
                "expected": "A2",
                "response": "A2",
                "category": 1,
                "elapsed_seconds": 7.0,
                "usage": {"total_tokens": 240},
                "openclaw_session_ledger": {"found": True, "message_count": 1},
            },
        ],
        "ov_log_tail": [
            '2026-06-14 08:48:16,313 - uvicorn.access - INFO - 127.0.0.1:40290 - "POST /api/v1/search/find HTTP/1.1" 200',
            '2026-06-14 08:48:16,532 - uvicorn.access - INFO - 127.0.0.1:40290 - "GET /api/v1/content/read?uri=viking%3A%2F%2Fuser%2Fdemo HTTP/1.1" 200',
        ],
    }
    (artifacts / "phaseA_demo_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (logs / "demo-run.master.log").write_text(
        "[phaseA][session 1/4][direct-ov] session_1 task=t1 session_id=s1 memories=0\n"
        "[phaseA][session 2/4][direct-ov] session_2 task=t2 session_id=s2 memories=3\n",
        encoding="utf-8",
    )

    result = diagnose_official_small_run(run_dir)

    assert result["nodes"]["session_construction"]["session_total"] == 2
    assert result["nodes"]["namespace_isolation"]["isolateUserScopeByAgent"] is False
    assert result["nodes"]["memory_capture"]["zero_memory_sessions"] == 1
    assert result["nodes"]["recall_query"]["search_find_calls"] == 1
    assert result["nodes"]["answer_generation"]["qa_total"] == 2
    assert result["timing"]["ingest"]["p50_seconds"] == 11.0
    assert result["timing"]["qa"]["max_seconds"] == 7.0
    assert result["findings"]
