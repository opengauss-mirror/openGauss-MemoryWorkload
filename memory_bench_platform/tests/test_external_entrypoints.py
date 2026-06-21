from pathlib import Path
import os
import sys
import json

from memory_bench_platform.integration import classify_entrypoint, execute_external_runner, resolve_benchmark_entrypoint


def test_classify_entrypoint_marks_official_locomo_script_as_external():
    entry = {"external_runner": "benchmark/locomo/openclaw/run_clean_small_in_container.sh"}
    assert classify_entrypoint(entry) == "external_runner"


def test_resolve_benchmark_entrypoint_reads_manifest_external_runner():
    entrypoint = resolve_benchmark_entrypoint("locomo", "official_small")
    assert entrypoint.entrypoint_kind == "external_runner"
    assert entrypoint.command[0] == "bash"
    assert entrypoint.command[-1].endswith("tools/test_entrypoints/run_official_locomo_small.sh")


def test_resolve_benchmark_entrypoint_reads_sample0_wrapper():
    entrypoint = resolve_benchmark_entrypoint("locomo", "official_sample0")
    assert entrypoint.entrypoint_kind == "external_runner"
    assert entrypoint.command[0] == "bash"
    assert entrypoint.command[-1].endswith("tools/test_entrypoints/run_official_locomo_sample.sh")


def test_execute_external_runner_captures_process_result(tmp_path: Path):
    script = tmp_path / "runner.py"
    script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['OUTPUT_DIR']).mkdir(parents=True, exist_ok=True)\n"
        "print('runner-ok')\n",
        encoding="utf-8",
    )
    from memory_bench_platform.protocol import EntryPointRecord

    result = execute_external_runner(
        EntryPointRecord(
            entrypoint_id="test",
            entrypoint_kind="external_runner",
            command=[sys.executable, str(script)],
        ),
        env={**os.environ, "OUTPUT_DIR": str(tmp_path / "out")},
        cwd=tmp_path,
    )
    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert "runner-ok" in result["stdout"]


def test_external_runner_missing_output_becomes_failed_summary(monkeypatch, tmp_path: Path):
    from memory_bench_platform import cli as cli_module

    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(
        cli_module,
        "_plan_from_args",
        lambda args: type(
            "Plan",
            (),
            {
                "run_id": "run-ext-fail",
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
        cli_module,
        "resolve_benchmark_entrypoint",
        lambda benchmark, entrypoint_id: type(
            "EntryPoint",
            (),
            {
                "entrypoint_id": "official_small",
                "entrypoint_kind": "external_runner",
                "command": ["bash", "dummy.sh"],
            },
        )(),
    )
    monkeypatch.setattr(
        cli_module,
        "execute_external_runner",
        lambda entrypoint, env, cwd=None: {"status": "passed", "exit_code": 0, "stdout": "", "stderr": ""},
    )
    monkeypatch.setattr(cli_module.Path, "cwd", lambda: tmp_path)

    cli_module.main(["run", "--benchmark", "locomo", "--agent", "openclaw", "--entrypoint", "official_small"])

    run_dir = tmp_path / "runs" / "run-ext-fail"
    summary = json.loads((run_dir / "reports" / "summary.json").read_text(encoding="utf-8"))
    entry = json.loads((run_dir / "records" / "external_entrypoint.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert "external_error" in summary["resource_summary"]
    assert entry["status"] == "failed"


def test_external_runner_receives_expected_version_env(monkeypatch, tmp_path: Path):
    from memory_bench_platform import cli as cli_module

    monkeypatch.chdir(tmp_path)

    captured = {}

    monkeypatch.setattr(
        cli_module,
        "_plan_from_args",
        lambda args: type(
            "Plan",
            (),
            {
                "run_id": "run-ext-version-env",
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
        cli_module,
        "resolve_benchmark_entrypoint",
        lambda benchmark, entrypoint_id: type(
            "EntryPoint",
            (),
            {
                "entrypoint_id": "official_small",
                "entrypoint_kind": "external_runner",
                "command": ["bash", "dummy.sh"],
            },
        )(),
    )
    monkeypatch.setattr(
        cli_module,
        "build_version_selection",
        lambda manifest, overrides=None: {
            "selection_mode": "latest_official_release_tag",
            "overridden": False,
            "targets": [
                {
                    "name": "openclaw" if getattr(manifest, "kind", "") == "agent" else "locomo-benchmark",
                    "upstream": "https://github.com/openclaw/openclaw"
                    if getattr(manifest, "kind", "") == "agent"
                    else "https://github.com/snap-research/locomo",
                    "resolved_default": {"status": "resolved", "resolved_version": "v2026.4.8"}
                    if getattr(manifest, "kind", "") == "agent"
                    else {"status": "resolved", "resolved_version": "v1.0.0"},
                },
                *(
                    [
                        {
                            "name": "openviking",
                            "upstream": "https://github.com/volcengine/OpenViking",
                            "resolved_default": {"status": "resolved", "resolved_version": "v0.3.24"},
                        }
                    ]
                    if getattr(manifest, "kind", "") == "agent"
                    else []
                ),
            ],
        },
    )

    def _fake_execute(entrypoint, env, cwd=None):
        captured["env"] = env
        output_dir = Path(env["OUTPUT_DIR"])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "meta.json").write_text(
            json.dumps(
                {
                    "overall_accuracy": 0.5,
                    "total_correct": 1,
                    "total_graded": 2,
                    "total_questions": 2,
                    "accuracy_by_category": {},
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "qa_results.csv").write_text(
            "question,expected_answer,response,category,result,input_tokens,output_tokens,cacheRead,cacheWrite,total_tokens\n"
            "q1,a1,r1,cat,CORRECT,1,1,0,0,2\n"
            "q2,a2,r2,cat,WRONG,1,1,0,0,2\n",
            encoding="utf-8",
        )
        return {"status": "passed", "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cli_module, "execute_external_runner", _fake_execute)
    monkeypatch.setattr(cli_module.Path, "cwd", lambda: tmp_path)

    cli_module.main(["run", "--benchmark", "locomo", "--agent", "openclaw", "--entrypoint", "official_small"])

    env = captured["env"]
    assert env["MEMORY_BENCH_EXPECTED_OPENVIKING_VERSION"] == "v0.3.24"
    assert env["MEMORY_BENCH_EXPECTED_OPENCLAW_UPSTREAM"] == "https://github.com/openclaw/openclaw"


def test_external_runner_receives_version_override_env(monkeypatch, tmp_path: Path):
    from memory_bench_platform import cli as cli_module

    monkeypatch.chdir(tmp_path)

    captured = {}

    monkeypatch.setattr(
        cli_module,
        "_plan_from_args",
        lambda args: type(
            "Plan",
            (),
            {
                "run_id": "run-ext-version-override",
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
        cli_module,
        "resolve_benchmark_entrypoint",
        lambda benchmark, entrypoint_id: type(
            "EntryPoint",
            (),
            {
                "entrypoint_id": "official_small",
                "entrypoint_kind": "external_runner",
                "command": ["bash", "dummy.sh"],
            },
        )(),
    )

    def _fake_execute(entrypoint, env, cwd=None):
        captured["env"] = env
        output_dir = Path(env["OUTPUT_DIR"])
        output_dir.mkdir(parents=True, exist_ok=True)
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
            "question,expected_answer,response,category,result,input_tokens,output_tokens,cacheRead,cacheWrite,total_tokens\n"
            "q1,a1,r1,cat,CORRECT,1,1,0,0,2\n",
            encoding="utf-8",
        )
        return {"status": "passed", "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cli_module, "execute_external_runner", _fake_execute)
    monkeypatch.setattr(cli_module.Path, "cwd", lambda: tmp_path)

    cli_module.main(
        [
            "run",
            "--benchmark",
            "locomo",
            "--agent",
            "openclaw",
            "--entrypoint",
            "official_small",
            "--version-override",
            "openviking=v0.3.24",
            "--version-override",
            "openclaw=v2026.4.8",
        ]
    )

    env = captured["env"]
    assert env["MEMORY_BENCH_EXPECTED_OPENVIKING_VERSION"] == "v0.3.24"
    assert env["MEMORY_BENCH_EXPECTED_OPENCLAW_VERSION"] == "v2026.4.8"
