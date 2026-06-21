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
                "extraction_signals": {
                    "memory_count": 0,
                    "durable_memory_file_count": 3,
                },
                "ov_observation": {
                    "poll_ok": True,
                    "detail": {
                        "commit_count": 1,
                        "memories_extracted": {"total": 0},
                        "llm_token_usage": {"total_tokens": 0},
                        "embedding_token_usage": {"total_tokens": 0},
                        "telemetry_summary": {
                            "memory": {"extract": {"stages": {"prepare_inputs_ms": 1.2}}}
                        },
                    },
                },
            },
            {
                "index": 2,
                "compact_elapsed_seconds": 12.0,
                "ov_session_id": "s2",
                "compact_status": {"commit_status": "completed"},
                "extraction_signals": {
                    "memory_count": 3,
                    "durable_memory_file_count": 5,
                },
                "ov_observation": {
                    "poll_ok": True,
                    "detail": {
                        "commit_count": 1,
                        "memories_extracted": {"total": 3},
                        "llm_token_usage": {"total_tokens": 0},
                        "embedding_token_usage": {"total_tokens": 0},
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
        "qa_direct_search_probe": {
            "question_count": 2,
            "zero_total_count": 2,
            "all_zero": True,
            "probes": [
                {"question": "Q1", "user_total": 0, "agent_total": 0},
                {"question": "Q2", "user_total": 0, "agent_total": 0},
            ],
        },
    }
    (artifacts / "phaseA_demo_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (logs / "demo-run.master.log").write_text(
        "[phaseA][session 1/4][direct-ov] session_1 task=t1 session_id=s1 memories=0\n"
        "[phaseA][session 2/4][direct-ov] session_2 task=t2 session_id=s2 memories=3\n",
        encoding="utf-8",
    )
    (logs / "demo-run.ov.log").write_text(
        "\n".join(
            [
                "2026-06-18 03:33:53,457 - openviking.session.compressor_v2 - ERROR - Failed to extract memories with v2: SessionExtractContextProvider.__init__() got an unexpected keyword argument 'latest_archive_session_time'",
                "2026-06-18 03:34:06,474 - openviking.session.session - ERROR - Agent memory extraction failed: SessionCompressorV2.extract_agent_memories() got an unexpected keyword argument 'latest_archive_overview'",
            ]
        ),
        encoding="utf-8",
    )
    (logs / "demo-run.preflight.json").write_text(
        json.dumps(
            {
                "openviking_git_describe": "v0.3.24",
                "observer_system": {"status_code": 200},
                "extract_compatibility": {
                    "session_extract_context_provider": {
                        "accepts_latest_archive_session_time": False,
                    },
                    "extract_agent_memories": {
                        "accepts_latest_archive_overview": False,
                        "accepts_latest_archive_session_time": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (logs / "demo-run.postrun.json").write_text(
        json.dumps(
            {
                "observer_models": {"status_code": 200},
                "observer_system": {
                    "body": {
                        "result": {
                            "components": {
                                "queue": {
                                    "status": "Embedding Pending 58 Requeued 444"
                                }
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    meta["post_ingest_reindex"] = {"ok": False, "last_error": "timeout"}
    (artifacts / "phaseA_demo_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    result = diagnose_official_small_run(run_dir)

    assert result["nodes"]["session_construction"]["session_total"] == 2
    assert result["nodes"]["namespace_isolation"]["isolateUserScopeByAgent"] is False
    assert result["nodes"]["memory_capture"]["zero_memory_sessions"] == 1
    assert result["nodes"]["memory_capture"]["phase2_zero_token_sessions"] == 2
    assert result["nodes"]["memory_capture"]["phase2_llm_extract_present_sessions"] == 0
    assert result["nodes"]["memory_capture"]["durable_growth_sessions"] == 2
    assert result["nodes"]["memory_capture"]["durable_growth_with_zero_memory"] == 1
    assert result["nodes"]["recall_query"]["search_find_calls"] == 1
    assert result["nodes"]["recall_query"]["direct_probe_all_zero"] is True
    assert result["nodes"]["answer_generation"]["qa_total"] == 2
    assert result["timing"]["ingest"]["p50_seconds"] == 11.0
    assert result["timing"]["qa"]["max_seconds"] == 7.0
    assert result["runtime"]["preflight"]["openviking_git_describe"] == "v0.3.24"
    assert result["runtime"]["extract_compatibility"]["session_extract_context_provider"]["accepts_latest_archive_session_time"] is False
    assert result["runtime"]["postrun"]["observer_models"]["status_code"] == 200
    assert result["runtime"]["post_ingest_reindex"]["ok"] is False
    assert "Embedding Pending 58" in result["runtime"]["queue_status_text"]
    assert "latest_archive_session_time" in result["runtime"]["signature_mismatch_errors"]
    assert "latest_archive_overview" in result["runtime"]["signature_mismatch_errors"]
    assert len(result["runtime"]["extract_runtime_errors"]) == 2
    assert any("平台观测口径与真实落盘结果不一致" in item for item in result["findings"])
    assert any("未产生 llm/embedding token" in item for item in result["findings"])
    assert any("post_ingest_reindex 未成功完成" in item for item in result["findings"])
    assert any("preflight 运行时接口自检失败" in item for item in result["findings"])
    assert any("接口签名错配" in item for item in result["findings"])
    assert any("direct search probe 对多条 QA 问题均为 0 命中" in item for item in result["findings"])
    assert result["findings"]


def test_diagnose_official_small_surfaces_extract_compatibility_import_error(tmp_path: Path):
    run_dir = tmp_path / "run-import-error"
    artifacts = run_dir / "external_artifacts" / "official_small"
    logs = artifacts / "remote_logs"
    logs.mkdir(parents=True)
    meta = {
        "run_id": "demo-import-error",
        "plugin_namespace_config": {"final": {}},
        "ingest_sessions": [],
        "qa_rows": [],
        "ov_log_tail": [],
    }
    (artifacts / "phaseA_demo_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (logs / "demo-import-error.preflight.json").write_text(
        json.dumps(
            {
                "extract_compatibility": {
                    "error": "No module named 'openviking.session.memory.constants'",
                    "error_type": "ModuleNotFoundError",
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    result = diagnose_official_small_run(run_dir)

    assert result["runtime"]["extract_compatibility"]["error_type"] == "ModuleNotFoundError"
    assert any("不能视为可信评测环境" in item for item in result["findings"])
