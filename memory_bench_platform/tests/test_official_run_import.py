import json
import importlib.util
from pathlib import Path

from memory_bench_platform.result_analysis import analyze_run


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "benchmarks"
    / "locomo"
    / "tooling"
    / "test_entrypoints"
    / "import_official_locomo_run.py"
)
SPEC = importlib.util.spec_from_file_location("import_official_locomo_run", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)
import_official_run = MODULE.import_official_run


def _write_small_output(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "qa_results.csv").write_text(
        "\n".join(
            [
                "sample_id,qi,question,expected,response,category,elapsed_seconds,input_tokens,output_tokens,cacheRead,cacheWrite,total_tokens,result,reasoning",
                "conv-1,1,Q1,A1,R1,1,1.2,10,5,0,0,15,CORRECT,ok",
                "conv-1,2,Q2,A2,R2,1,2.4,12,6,0,0,18,WRONG,bad",
            ]
        ),
        encoding="utf-8",
    )
    meta = {
        "run_id": "demo-import",
        "plugin_namespace_config": {"final": {"accountId": "acct-demo", "userId": "user-demo"}},
        "ingest_sessions": [
            {
                "index": 1,
                "compact_elapsed_seconds": 3.0,
                "ov_observation": {
                    "detail": {
                        "created_at": "2026-06-15T18:00:00.000Z",
                        "updated_at": "2026-06-15T18:00:04.000Z",
                        "_ov_task": {
                            "task_id": "task-1",
                            "status": "completed",
                            "created_at_iso": "2026-06-15T18:00:01+00:00",
                            "updated_at_iso": "2026-06-15T18:00:03+00:00",
                        },
                    }
                },
                "telemetry_summary": {
                    "operation": "session_commit_phase2",
                    "duration_ms": 2000,
                    "memory": {"extract": {"stages": {"llm_extract_ms": 450.0}}},
                    "resource": {"request": {"duration_ms": 12.0}},
                },
            }
        ],
        "qa_rows": [
            {
                "qi": 1,
                "question": "Q1",
                "response": "R1",
                "elapsed_seconds": 1.2,
                "usage": {"total_tokens": 15},
            }
        ],
        "ov_log_tail": [],
    }
    (output_dir / "phaseA_demo_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    remote_logs = output_dir / "remote_logs"
    remote_logs.mkdir(parents=True, exist_ok=True)
    (remote_logs / "demo.master.log").write_text(
        "[phaseA][session 1/4][direct-ov] session_1 task=t1 session_id=s1 memories=2\n",
        encoding="utf-8",
    )


def test_import_official_run_populates_platform_run_and_reports(tmp_path: Path):
    output_dir = tmp_path / "official_small_output"
    _write_small_output(output_dir)
    platform_runs_root = tmp_path / "runs"

    run_dir = import_official_run(
        run_id="demo-import",
        entrypoint_id="official_small",
        benchmark_id="locomo",
        agent_id="openclaw",
        output_dir=output_dir,
        platform_runs_root=platform_runs_root,
    )

    analysis = analyze_run(run_dir)

    assert run_dir == platform_runs_root / "demo-import"
    assert (run_dir / "external_artifacts" / "official_small" / "qa_results.csv").is_file()
    assert (run_dir / "reports" / "timing_report.json").is_file()
    assert (run_dir / "reports" / "timing_report.html").is_file()
    assert analysis["timing_report"]["duration_label_count"] >= 1
