from __future__ import annotations

from pathlib import Path
import os

from memory_bench_platform.integration import run_json_script


def test_run_json_script_merges_trace_environment_without_global_mutation(tmp_path: Path):
    script = tmp_path / "runner.py"
    script.write_text(
        "import json, os; print(json.dumps({'trace': os.environ.get('TRACE_AGENT_CHAT_BASE_URL')}))",
        encoding="utf-8",
    )
    result = run_json_script(
        script,
        environment={"TRACE_AGENT_CHAT_BASE_URL": "http://trace.local"},
    )
    assert result == {"trace": "http://trace.local"}
    assert "TRACE_AGENT_CHAT_BASE_URL" not in os.environ
