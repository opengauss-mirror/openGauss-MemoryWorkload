import json
from pathlib import Path

import pytest

from memory_bench_platform.integration import run_memory_task
from memory_bench_platform.protocol import MemoryTaskInput, WorkflowRuntimeContext


def _write_memory_skill(tmp_path: Path, runner_source: str, *, include_runner: bool = True) -> Path:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "memories" / "demo-memory"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    entry_lines = ["entry:"] if include_runner else ["entry: {}"]
    if include_runner:
        entry_lines.append("  runner: scripts/run.py")
        (scripts_dir / "run.py").write_text(runner_source, encoding="utf-8")
    manifest = "\n".join(
        [
            "kind: memory",
            "id: demo-memory",
            "version: 0.1.0",
            "version_policy:",
            "  default_selection: latest_official_release_tag",
            "  resolution_order:",
            "    - latest_official_release_tag",
            "  allowed_overrides:",
            "    - user_specified_official_version",
            "  disallowed_defaults:",
            "    - dirty_worktree",
            "  targets:",
            "    - name: demo-memory",
            "      scope: memory_backend",
            "      version_source: upstream_release_tag",
            "      upstream: https://example.test/demo-memory",
            *entry_lines,
        ]
    )
    (skill_dir / "manifest.yaml").write_text(manifest + "\n", encoding="utf-8")
    return skills_root


def _request(tmp_path: Path) -> MemoryTaskInput:
    return MemoryTaskInput(
        task_id="recall-step",
        action="recall",
        inputs={"query": "preferred language"},
        runtime_context=WorkflowRuntimeContext(
            run_id="run-1",
            run_dir=str(tmp_path),
            benchmark_id="benchmark-1",
            agent_id="agent-1",
            memory_id="demo-memory",
        ),
        idempotency_key="run-1:case-1:recall-step",
    )


def test_run_memory_task_uses_manifest_runner_json_contract(tmp_path: Path, monkeypatch):
    runner = """
import json
import os
import sys

request = json.load(sys.stdin)
print(json.dumps({
    "status": "ok",
    "state": "completed",
    "operation": {"task_id": request["task_id"]},
    "output": {
        "query": request["inputs"]["query"],
        "environment_marker": os.environ.get("MEMORY_RUNNER_TEST_MARKER"),
    },
    "metrics": [],
    "artifacts": [],
    "error": {},
}))
"""
    skills_root = _write_memory_skill(tmp_path, runner)
    monkeypatch.setattr("memory_bench_platform.integration.SKILLS_ROOT", skills_root)
    monkeypatch.setenv("MEMORY_RUNNER_TEST_MARKER", "inherited")

    result = run_memory_task("demo-memory", _request(tmp_path))

    assert result.status == "ok"
    assert result.state == "completed"
    assert result.operation["task_id"] == "recall-step"
    assert result.output["query"] == "preferred language"
    assert result.output["environment_marker"] == "inherited"


def test_run_memory_task_rejects_manifest_without_runner(tmp_path: Path, monkeypatch):
    skills_root = _write_memory_skill(tmp_path, "", include_runner=False)
    monkeypatch.setattr("memory_bench_platform.integration.SKILLS_ROOT", skills_root)

    with pytest.raises(ValueError, match="has no runner"):
        run_memory_task("demo-memory", _request(tmp_path))


def test_run_memory_task_rejects_invalid_json(tmp_path: Path, monkeypatch):
    skills_root = _write_memory_skill(tmp_path, "print('not-json')\n")
    monkeypatch.setattr("memory_bench_platform.integration.SKILLS_ROOT", skills_root)

    with pytest.raises(ValueError, match="invalid JSON"):
        run_memory_task("demo-memory", _request(tmp_path))


def test_run_memory_task_rejects_invalid_response_model(tmp_path: Path, monkeypatch):
    runner = f"print({json.dumps(json.dumps({'status': 'ok', 'state': 'unknown'}))})\n"
    skills_root = _write_memory_skill(tmp_path, runner)
    monkeypatch.setattr("memory_bench_platform.integration.SKILLS_ROOT", skills_root)

    with pytest.raises(ValueError, match="invalid response"):
        run_memory_task("demo-memory", _request(tmp_path))
