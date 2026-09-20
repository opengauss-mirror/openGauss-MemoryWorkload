from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from memory_bench_platform.protocol import (
    EntryPointRecord,
    TraceCounterSnapshot,
    TraceRuntimeSummary,
)
from memory_bench_platform.trace_runtime import TraceBindings
from memory_bench_platform.trace_runtime.bundle import BundleWriter
from memory_bench_platform.trace_runtime.protocol import (
    ProviderRequest,
    ProviderResponse,
    TraceRecord,
    TraceScope,
    TraceTiming,
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
        return {"summary_util_idle": 100.0, "summary_util_user": 0.0, "summary_util_sys": 0.0}


def test_external_trace_run_archives_config_and_summary(tmp_path: Path, monkeypatch):
    from memory_bench_platform import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "ResourceMonitor", _Monitor)

    class _TraceRuntime:
        @classmethod
        def prepare(cls, config):
            instance = cls()
            instance.config = config
            return instance

        def start(self):
            pass

        def bindings(self):
            return TraceBindings(
                endpoints={"embedding": "http://127.0.0.1:18087/v1"},
                environment={"TRACE_EMBEDDING_BASE_URL": "http://127.0.0.1:18087/v1"},
            )

        def stop(self):
            pass

        def verify_and_collect(self):
            return TraceRuntimeSummary(
                mode="replay-zero-delay",
                bundle_id="bundle",
                valid=False,
                channels={"embedding": TraceCounterSnapshot(loaded=1, remaining=1)},
            )

        def collect(self, run_dir, summary):
            artifact_dir = run_dir / "artifacts" / "trace_runtime"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "counter-snapshots.jsonl").write_text("{}\n")

    monkeypatch.setattr(cli, "TraceRuntime", _TraceRuntime)
    monkeypatch.setattr(
        cli,
        "resolve_benchmark_entrypoint",
        lambda benchmark, entrypoint: EntryPointRecord(
            entrypoint_id="external",
            entrypoint_kind="external_runner",
            command=["runner"],
        ),
    )
    monkeypatch.setattr(
        cli,
        "execute_external_runner",
        lambda *args, **kwargs: {"status": "failed", "exit_code": 1, "stdout": "", "stderr": "failed"},
    )
    monkeypatch.setattr(
        cli,
        "_plan_from_args",
        lambda args: type(
            "Plan",
            (),
            {
                "run_id": "trace-external",
                "benchmark_id": args.benchmark,
                "agent_id": args.agent,
                "benchmark_version": None,
                "agent_version": None,
                "memory_backend": None,
                "hardware_profile": None,
            },
        )(),
    )
    monkeypatch.setattr(
        cli,
        "build_version_selection",
        lambda manifest, overrides=None: {"selection_mode": "latest_official_release_tag", "overridden": False, "targets": []},
    )

    cli.main(
        [
            "run",
            "--benchmark",
            "locomo",
            "--agent",
            "openclaw",
            "--entrypoint",
            "external",
            "--trace-mode",
            "replay-zero-delay",
            "--trace-profile",
            "openai-compatible@1",
            "--trace-bundle",
            str(tmp_path / "bundle"),
            "--trace-deployment",
            "external",
            "--trace-endpoint",
            "embedding=http://127.0.0.1:18087/v1",
            "--trace-channel",
            "embedding=openai-embeddings:strict:fingerprint",
        ]
    )

    run_dir = tmp_path / "runs" / "trace-external"
    config = json.loads((run_dir / "config_snapshot" / "trace-runtime.json").read_text())
    summary = json.loads((run_dir / "records" / "trace_runtime_summary.json").read_text())
    assert config["mode"] == "replay-zero-delay"
    assert config["deployment"] == "external"
    assert summary["valid"] is False
    report_summary = json.loads((run_dir / "reports" / "summary.json").read_text())
    assert report_summary["run_validity"]["trace_runtime"]["valid"] is False


def test_managed_trace_replay_wraps_external_runner_and_archives_metrics(tmp_path: Path, monkeypatch):
    from memory_bench_platform import cli

    bundle_path = tmp_path / "bundle"
    writer = BundleWriter(
        bundle_path,
        bundle_id="cli-bundle",
        profile_id="openai-compatible@1",
        channels={
            "embedding": {
                "protocol": "openai-embeddings",
                "match_mode": "strict",
                "order_scope": "fingerprint",
            }
        },
    )
    writer.append(
        TraceRecord(
            trace_id="trace-1",
            ordinal=1,
            channel="embedding",
            protocol="openai-embeddings",
            scope=TraceScope(request_id="capture-1", session_id="session-1"),
            request=ProviderRequest(
                method="POST",
                path="/v1/embeddings",
                headers={"content-type": "application/json"},
                body={"model": "embed", "input": ["hello"]},
            ),
            response=ProviderResponse(
                status=200,
                headers={"content-type": "application/json"},
                body={"data": [{"embedding": [1.0]}]},
            ),
            timing=TraceTiming(
                started_at=datetime.now(timezone.utc).isoformat(),
                upstream_duration_ms=1,
            ),
        )
    )
    writer.finalize()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "ResourceMonitor", _Monitor)
    monkeypatch.setattr(
        cli,
        "resolve_benchmark_entrypoint",
        lambda benchmark, entrypoint: EntryPointRecord(
            entrypoint_id="external",
            entrypoint_kind="external_runner",
            command=["runner"],
        ),
    )
    monkeypatch.setattr(
        cli,
        "_plan_from_args",
        lambda args: type(
            "Plan",
            (),
            {
                "run_id": "trace-managed",
                "benchmark_id": args.benchmark,
                "agent_id": args.agent,
                "benchmark_version": None,
                "agent_version": None,
                "memory_backend": None,
                "hardware_profile": None,
            },
        )(),
    )
    monkeypatch.setattr(
        cli,
        "build_version_selection",
        lambda manifest, overrides=None: {"selection_mode": "latest_official_release_tag", "overridden": False, "targets": []},
    )

    def _execute(entrypoint, env, cwd=None):
        request = Request(
            env["TRACE_EMBEDDING_BASE_URL"] + "/v1/embeddings",
            data=b'{"model":"embed","input":["hello"]}',
            method="POST",
            headers={
                "content-type": "application/json",
                "x-request-id": "replay-1",
            },
        )
        with urlopen(request, timeout=5) as response:
            assert json.loads(response.read()) == {"data": [{"embedding": [1.0]}]}
        output_dir = Path(env["OUTPUT_DIR"])
        output_dir.mkdir(parents=True)
        (output_dir / "meta.json").write_text(
            json.dumps(
                {
                    "overall_accuracy": 1.0,
                    "total_correct": 1,
                    "total_graded": 1,
                    "total_questions": 1,
                    "accuracy_by_category": {},
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "qa_results.csv").write_text(
            "question,expected_answer,response,category,result\nq,a,a,1,CORRECT\n",
            encoding="utf-8",
        )
        return {"status": "passed", "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cli, "execute_external_runner", _execute)
    cli.main(
        [
            "run",
            "--benchmark",
            "locomo",
            "--agent",
            "openclaw",
            "--entrypoint",
            "external",
            "--trace-mode",
            "replay-zero-delay",
            "--trace-bundle",
            str(bundle_path),
            "--run-id",
            "trace-managed",
        ]
    )

    run_dir = tmp_path / "runs" / "trace-managed"
    summary = json.loads((run_dir / "records" / "trace_runtime_summary.json").read_text())
    metrics = json.loads((run_dir / "records" / "metrics.json").read_text())
    report_summary = json.loads((run_dir / "reports" / "summary.json").read_text())
    assert summary["valid"] is True
    assert summary["channels"]["embedding"]["matched"] == 1
    assert any(item["name"] == "trace.requests.matched" for item in metrics)
    assert report_summary["run_validity"]["trace_runtime"]["valid"] is True
