from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory_bench_platform.benchmark_scenario import BenchmarkScenario
from memory_bench_platform.cli import main
from memory_bench_platform.protocol import (
    EntryPointRecord,
    JudgeResult,
    MemoryPluginTaskOutput,
    StepResultRecord,
)


class _Monitor:
    def __init__(self, *args, **kwargs):
        pass

    def setup_writers(self):
        pass

    def start_background_sampling(self):
        pass

    def stop_background_sampling(self):
        pass

    def capture_once(self):
        return {
            "summary_util_idle": 100.0,
            "summary_util_user": 0.0,
            "summary_util_sys": 0.0,
        }


def _scenario(*, target: str = "qa_answer", allow_override: bool = False):
    metadata = {}
    if allow_override:
        metadata["evaluation_override"] = {
            "enabled": True,
            "reason": "contract test",
        }
    return BenchmarkScenario.model_validate(
        {
            "benchmark_id": "locomo",
            "evaluation": {"target": target, "profile": "llm_judge@1"},
            "metadata": metadata,
            "requirements": {
                "agent": {"multi_turn": True, "stateful_session": True},
                "memory": {"actions": ["ingest", "recall"]},
            },
            "samples": [
                {
                    "sample_id": "sample-1",
                    "timeline": [
                        {
                            "event_id": "session-1",
                            "type": "conversation",
                            "payload": {"content": "remember tea"},
                        },
                        {
                            "event_id": "checkpoint-1",
                            "type": "checkpoint",
                            "evaluation": {
                                "target": target,
                                "profile": "llm_judge@1",
                                "questions": [
                                    {
                                        "question_id": "q1",
                                        "question": "What should be remembered?",
                                        "reference": "tea",
                                    }
                                ],
                            },
                        },
                    ],
                }
            ],
        }
    )


def _patch_common(monkeypatch, scenario):
    monkeypatch.setattr("memory_bench_platform.cli.ResourceMonitor", _Monitor)
    monkeypatch.setattr(
        "memory_bench_platform.cli.build_version_selection",
        lambda manifest, overrides=None: {
            "selection_mode": manifest.version_policy.default_selection,
            "overridden": False,
            "targets": [],
        },
    )
    monkeypatch.setattr(
        "memory_bench_platform.cli.build_benchmark_scenario",
        lambda benchmark, data_path: scenario,
    )


def _run_args(run_id: str, *, integration: str = "backend_direct") -> list[str]:
    return [
        "run",
        "--benchmark",
        "locomo",
        "--agent",
        "openclaw",
        "--memory-backend",
        "openviking",
        "--memory-integration",
        integration,
        "--run-id",
        run_id,
    ]


def _run_dir(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "runs" / run_id


def test_smoke_execution_failure_is_archived_and_run_is_terminated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "memory_bench_platform.cli.execute_smoke_skill",
        lambda smoke_id, run_dir: (_ for _ in ()).throw(RuntimeError("smoke crashed")),
    )

    with pytest.raises(RuntimeError, match="smoke crashed"):
        main(
            [
                "run-smoke",
                "--smoke",
                "locomo-openclaw-openviking-minimal",
                "--run-id",
                "smoke-failure",
            ]
        )

    run_dir = _run_dir(tmp_path, "smoke-failure")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    error = json.loads((run_dir / "records/run_error.json").read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["ended_at"] is not None
    assert error["phase"] == "smoke_execution"


def test_smoke_result_write_failure_is_archived(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "memory_bench_platform.cli.execute_smoke_skill",
        lambda smoke_id, run_dir: {
            "probe": {},
            "validation": {"status": "passed", "stage_results": []},
            "report": {"html": ""},
        },
    )
    monkeypatch.setattr(
        "memory_bench_platform.cli._write_smoke_result",
        lambda run_dir, result: (_ for _ in ()).throw(OSError("write failed")),
    )

    with pytest.raises(OSError, match="write failed"):
        main(
            [
                "run-smoke",
                "--smoke",
                "locomo-openclaw-openviking-minimal",
                "--run-id",
                "smoke-write-failure",
            ]
        )

    run_dir = _run_dir(tmp_path, "smoke-write-failure")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    error = json.loads((run_dir / "records/run_error.json").read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["ended_at"] is not None
    assert error["phase"] == "smoke_execution"


def test_external_runner_start_failure_is_archived(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_common(monkeypatch, _scenario())
    monkeypatch.setattr(
        "memory_bench_platform.cli.resolve_benchmark_entrypoint",
        lambda benchmark, entrypoint: EntryPointRecord(
            entrypoint_id="external",
            entrypoint_kind="external_runner",
            command=["missing-runner"],
        ),
    )
    monkeypatch.setattr(
        "memory_bench_platform.cli.execute_external_runner",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing-runner")),
    )

    with pytest.raises(FileNotFoundError, match="missing-runner"):
        main(_run_args("external-runner-failure"))

    run_dir = _run_dir(tmp_path, "external-runner-failure")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    error = json.loads((run_dir / "records/run_error.json").read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["ended_at"] is not None
    assert error["phase"] == "external_runner_execution"


def test_external_monitor_setup_failure_is_archived(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_common(monkeypatch, _scenario())
    monkeypatch.setattr(
        "memory_bench_platform.cli.resolve_benchmark_entrypoint",
        lambda benchmark, entrypoint: EntryPointRecord(
            entrypoint_id="external",
            entrypoint_kind="external_runner",
            command=["runner"],
        ),
    )

    class BrokenMonitor(_Monitor):
        def setup_writers(self):
            raise OSError("monitor setup failed")

    monkeypatch.setattr("memory_bench_platform.cli.ResourceMonitor", BrokenMonitor)

    with pytest.raises(OSError, match="monitor setup failed"):
        main(_run_args("external-monitor-failure"))

    run_dir = _run_dir(tmp_path, "external-monitor-failure")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    error = json.loads((run_dir / "records/run_error.json").read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["ended_at"] is not None
    assert error["phase"] == "external_runner_execution"


def test_smoke_gate_exception_is_archived_before_benchmark_execution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_common(monkeypatch, _scenario())
    monkeypatch.setattr(
        "memory_bench_platform.cli.execute_smoke_skill",
        lambda smoke_id, run_dir: (_ for _ in ()).throw(RuntimeError("gate crashed")),
    )

    with pytest.raises(RuntimeError, match="gate crashed"):
        main(
            _run_args("smoke-gate-failure")
            + ["--smoke-gate", "locomo-openclaw-openviking-minimal"]
        )

    run_dir = _run_dir(tmp_path, "smoke-gate-failure")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    error = json.loads((run_dir / "records/run_error.json").read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["ended_at"] is not None
    assert error["phase"] == "smoke_gate"
    assert error["details"]["smoke_id"] == "locomo-openclaw-openviking-minimal"


def test_scenario_builder_failure_is_archived_and_run_is_terminated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_common(monkeypatch, _scenario())
    monkeypatch.setattr(
        "memory_bench_platform.cli.build_benchmark_scenario",
        lambda benchmark, data_path: (_ for _ in ()).throw(ValueError("bad dataset")),
    )

    with pytest.raises(ValueError, match="bad dataset"):
        main(_run_args("preflight-builder-failure"))

    run_dir = _run_dir(tmp_path, "preflight-builder-failure")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    error = json.loads((run_dir / "records/run_error.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "reports/summary.json").read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["ended_at"] is not None
    assert error["phase"] == "scenario_builder"
    assert summary["status"] == "failed"


def test_unsupported_target_is_archived_as_invalid_preflight(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scenario = _scenario(target="memory_extraction", allow_override=True)
    _patch_common(monkeypatch, scenario)

    with pytest.raises(ValueError, match="incompatible"):
        main(_run_args("preflight-incompatible"))

    run_dir = _run_dir(tmp_path, "preflight-incompatible")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    error = json.loads((run_dir / "records/run_error.json").read_text(encoding="utf-8"))
    assert run["status"] == "invalid"
    assert run["ended_at"] is not None
    assert error["phase"] == "compatibility"
    assert (
        "runtime.evaluation_targets.memory_extraction"
        in error["details"]["missing_capabilities"]
    )


def test_composer_failure_is_archived_and_run_is_terminated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_common(monkeypatch, _scenario())
    monkeypatch.setattr(
        "memory_bench_platform.cli.compose_run_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("compose failed")),
    )

    with pytest.raises(RuntimeError, match="compose failed"):
        main(_run_args("preflight-composer-failure"))

    run_dir = _run_dir(tmp_path, "preflight-composer-failure")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    error = json.loads((run_dir / "records/run_error.json").read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["ended_at"] is not None
    assert error["phase"] == "composer"


def test_unexpected_runtime_exception_is_archived(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_common(monkeypatch, _scenario())
    monkeypatch.setattr(
        "memory_bench_platform.cli.execute_cases",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("executor crashed")),
    )

    with pytest.raises(RuntimeError, match="executor crashed"):
        main(_run_args("runtime-execution-failure"))

    run_dir = _run_dir(tmp_path, "runtime-execution-failure")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    error = json.loads((run_dir / "records/run_error.json").read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["ended_at"] is not None
    assert error["phase"] == "runtime_execution"


def test_plugin_finalize_failure_invalidates_successful_questions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scenario = _scenario()
    _patch_common(monkeypatch, scenario)

    def execute_cases(**kwargs):
        step_results = [
            StepResultRecord(
                step_result_id=f"{step.step_id}-attempt-1",
                step_id=step.step_id,
                attempt=1,
                status="passed",
                duration_ms=1,
                gate_passed=True,
                structured_output={"agent_answer": "tea"},
            )
            for step in kwargs["steps"]
        ]
        question_case = next(
            case for case in kwargs["cases"] if case.judge_mode != "none"
        )
        return {
            "step_results": step_results,
            "traces": [],
            "judge_results": [
                JudgeResult(
                    judge_id="q1",
                    run_id=kwargs["run_id"],
                    case_id=question_case.case_id,
                    score=1.0,
                    passed=True,
                )
            ],
            "metrics": [],
            "artifacts": [],
        }

    monkeypatch.setattr("memory_bench_platform.cli.execute_cases", execute_cases)
    monkeypatch.setattr(
        "memory_bench_platform.cli.run_memory_plugin_task",
        lambda skill_id, request: MemoryPluginTaskOutput(
            status="failed",
            state="failed",
            error={
                "type": "RestoreError",
                "code": "restore_error",
                "category": "runtime",
                "retryable": False,
                "message": "restore failed",
            },
        ),
    )

    main(_run_args("plugin-finalize-failure", integration="agent_plugin"))

    run_dir = _run_dir(tmp_path, "plugin-finalize-failure")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "reports/summary.json").read_text(encoding="utf-8"))
    step_results = json.loads(
        (run_dir / "records/step_results.json").read_text(encoding="utf-8")
    )
    finalize = next(
        item for item in step_results if item["step_id"] == "run-memory-plugin-finalize"
    )
    assert run["status"] == "invalid"
    assert summary["raw_benchmark_score"] == 1.0
    assert summary["benchmark_score"] is None
    assert summary["runtime_failure_rate"] > 0
    assert "memory_plugin_finalize_failed" in summary["run_validity"]["reasons"]
    assert finalize["status"] == "failed"
