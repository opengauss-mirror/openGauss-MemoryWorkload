from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

def _load_probe() -> dict:
    raw = sys.stdin.read().strip()
    return json.loads(raw or "{}")


def _static_validate(probe: dict) -> dict:
    issues: list[str] = []
    for key in ("mini_test_config", "smoke_test_config", "remote_entrypoint", "env_toml"):
        path = Path(str(probe.get(key, "") or ""))
        if not path.exists():
            issues.append(f"missing_required_path:{key}")
    env_path = Path(str(probe.get("env_toml", "") or ""))
    if env_path.exists():
        try:
            tomllib.loads(env_path.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(f"invalid_env_toml:{exc}")
    passed = not issues
    return {
        "status": "passed" if passed else "failed",
        "mode": "static",
        "stage_results": [
            {
                "case_id": "stage-session_bootstrap",
                "question": "session_bootstrap",
                "label": "passed" if passed else "failed",
                "passed": passed,
                "response": "required smoke files and env prerequisites are present" if passed else ";".join(issues),
            }
        ],
        "issues": issues,
    }


def _prepare_runtime_config(probe: dict, run_dir: Path) -> tuple[Path, Path]:
    mini_config = Path(probe["mini_test_config"])
    smoke_root = run_dir / "smoke_artifacts"
    smoke_root.mkdir(parents=True, exist_ok=True)
    text = mini_config.read_text(encoding="utf-8")
    run_name = probe.get("smoke_run_name", "mini-smoke")
    if re.search(r'^name = ".*"$', text, flags=re.MULTILINE):
        text = re.sub(r'^name = ".*"$', f'name = "{run_name}"', text, flags=re.MULTILINE)
    if re.search(r'^output_dir = ".*"$', text, flags=re.MULTILINE):
        text = re.sub(r'^output_dir = ".*"$', f'output_dir = "{smoke_root}"', text, flags=re.MULTILINE)
    else:
        text = text.replace("[general]\n", f'[general]\noutput_dir = "{smoke_root}"\n', 1)
    repo_root = Path(str(probe.get("repo_root", "") or ""))
    locomo_small = repo_root / "locomo_test" / "data" / "locomo_small.json"
    if locomo_small.exists() and not re.search(r"(?m)^data_file\s*=", text):
        text = text.replace("[general]\n", f'[general]\ndata_file = "{locomo_small}"\n', 1)
    runtime_config = smoke_root / "mini-test-runtime.toml"
    runtime_config.write_text(text, encoding="utf-8")
    env_src_raw = str(probe.get("env_toml", "") or "").strip()
    env_src = Path(env_src_raw) if env_src_raw else (mini_config.parent / "env.toml")
    if env_src.exists():
        env_text = env_src.read_text(encoding="utf-8")
        default_state_dir = smoke_root / "openclaw-state"
        if re.search(r'(?m)^state_dir\s*=\s*""\s*$', env_text):
            env_text = re.sub(
                r'(?m)^state_dir\s*=\s*""\s*$',
                f'state_dir = "{default_state_dir}"',
                env_text,
                count=1,
            )
        (smoke_root / "env.toml").write_text(env_text, encoding="utf-8")
    return runtime_config, smoke_root / run_name


def _run_locomo_smoke(probe: dict, run_dir: Path) -> dict:
    runtime_config, output_dir = _prepare_runtime_config(probe, run_dir)
    repo_root = Path(probe["repo_root"])
    env = os.environ.copy()
    remote_entrypoint = Path(str(probe.get("remote_entrypoint", "") or ""))
    if remote_entrypoint.exists():
        env["RUN_ID"] = probe.get("smoke_run_name", "mini-smoke")
        env["OUTPUT_DIR"] = str(output_dir)
        env["LOCOMO_TEST_CONFIG"] = Path(str(probe["mini_test_config"])).name
        cmd = ["bash", str(remote_entrypoint)]
    else:
        env["PYTHONPATH"] = f"{repo_root / 'locomo_test'}:{repo_root / 'memory_bench_platform'}"
        env["LOCOMO_RUN_LOCK_DIR"] = str(run_dir / ".locks")
        cmd = [sys.executable, "-m", "locomo_test.cli", "run", str(runtime_config)]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "output_dir": str(output_dir),
    }


def _read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _stage_result(case_id: str, passed: bool, response: str) -> dict:
    return {
        "case_id": f"stage-{case_id}",
        "question": case_id,
        "label": "passed" if passed else "failed",
        "passed": passed,
        "response": response,
    }


def _runtime_validate(probe: dict, run_dir: Path) -> dict:
    execution = _run_locomo_smoke(probe, run_dir)
    output_dir = Path(execution["output_dir"])
    pipeline_log = (output_dir / "pipeline.log").read_text(encoding="utf-8", errors="ignore") if (output_dir / "pipeline.log").exists() else ""
    qa_rows = _read_csv_rows(output_dir / "qa_results.csv")
    qa_diag = json.loads((output_dir / "qa_diagnostics.json").read_text(encoding="utf-8")) if (output_dir / "qa_diagnostics.json").exists() else {}
    recall_hits = sum(int((row.get("ov_direct_recall_count") or "0").strip() or 0) for row in qa_rows)
    blocked = execution["returncode"] != 0 and not pipeline_log

    def _blocked_response(text: str) -> str:
        if not blocked:
            return text
        return f"blocked_by_execution_error: {execution['stderr'].strip() or execution['returncode']}"

    stage_results = [
        _stage_result("session_bootstrap", "[health_check] done" in pipeline_log, _blocked_response("health_check completed")),
        _stage_result("message_ingest", (output_dir / ".ingest_record.json").exists(), _blocked_response("ingest record present")),
        _stage_result("session_commit", "[ov-commit]" in pipeline_log, _blocked_response("ov commit observed")),
        _stage_result("memory_extraction", recall_hits > 0, _blocked_response(f"direct recall hits={recall_hits}")),
        _stage_result("reindex_or_consistency", (output_dir / "qa_reindex.json").exists(), _blocked_response("qa_reindex present")),
        _stage_result("recall_probe", recall_hits > 0, _blocked_response(f"direct recall hits={recall_hits}")),
        _stage_result("answer_probe", any(str(row.get("response") or "").strip() for row in qa_rows), _blocked_response("qa responses present")),
        _stage_result("result_parse", (output_dir / "qa_diagnostics.json").exists(), _blocked_response("qa diagnostics present")),
    ]
    issues = [item["case_id"] for item in stage_results if not item["passed"]]
    if execution["returncode"] != 0:
        issues.append(f"locomo_test_exit_code:{execution['returncode']}")
    return {
        "status": "passed" if execution["returncode"] == 0 and not issues else "failed",
        "mode": "runtime",
        "stage_results": stage_results,
        "issues": issues,
        "artifacts": {
            "output_dir": str(output_dir),
            "pipeline_log": str(output_dir / "pipeline.log"),
            "qa_results_csv": str(output_dir / "qa_results.csv"),
            "qa_diagnostics_json": str(output_dir / "qa_diagnostics.json"),
            "qa_reindex_json": str(output_dir / "qa_reindex.json"),
        },
        "qa_diagnostics": qa_diag,
        "execution": execution,
    }


def main() -> None:
    probe = _load_probe()
    run_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    payload = _runtime_validate(probe, run_dir) if run_dir else _static_validate(probe)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
