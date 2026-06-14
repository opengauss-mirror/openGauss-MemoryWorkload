from pathlib import Path
import os
import sys

from memory_bench_platform.integration import classify_entrypoint, execute_external_runner, resolve_benchmark_entrypoint


def test_classify_entrypoint_marks_official_locomo_script_as_external():
    entry = {"external_runner": "benchmark/locomo/openclaw/run_clean_small_in_container.sh"}
    assert classify_entrypoint(entry) == "external_runner"


def test_resolve_benchmark_entrypoint_reads_manifest_external_runner():
    entrypoint = resolve_benchmark_entrypoint("locomo", "official_small")
    assert entrypoint.entrypoint_kind == "external_runner"
    assert entrypoint.command[0] == "bash"
    assert entrypoint.command[-1].endswith("tools/test_entrypoints/run_official_locomo_small.sh")


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
