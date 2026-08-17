#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _load_optional_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _detect_status(output_dir: Path) -> str:
    qa_csv = output_dir / "qa_results.csv"
    if qa_csv.exists():
        return "passed"
    phase_csvs = sorted(output_dir.glob("phaseA*.csv"))
    if phase_csvs:
        return "running"
    return "failed"


def import_official_run(
    *,
    run_id: str,
    entrypoint_id: str,
    benchmark_id: str,
    agent_id: str,
    output_dir: Path,
    platform_runs_root: Path,
) -> Path:
    run_dir = platform_runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "reports").mkdir(parents=True, exist_ok=True)
    (run_dir / "records").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts" / "monitor").mkdir(parents=True, exist_ok=True)

    external_dir = run_dir / "external_artifacts" / entrypoint_id
    _copy_tree(output_dir, external_dir)

    run_record = {
        "run_id": run_id,
        "source_id": f"{benchmark_id}:{entrypoint_id}",
        "source_kind": "external_benchmark_runner",
        "agent_id": agent_id,
        "status": _detect_status(output_dir),
        "started_at": datetime.now().isoformat(),
        "ended_at": datetime.now().isoformat(),
    }
    (run_dir / "run.json").write_text(
        json.dumps(run_record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary_payload = _load_optional_json(output_dir / "meta.json")
    if summary_payload:
        (run_dir / "reports" / "external_result_summary.json").write_text(
            json.dumps(
                {
                    "source": "locomo_test",
                    "summary": summary_payload,
                    "case_results": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    (run_dir / "reports" / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": run_record["status"],
                "case_total": 0,
                "case_passed": 0,
                "case_failed": 0,
                "category_summary": {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "reports" / "case_results.json").write_text(
        "[]",
        encoding="utf-8",
    )

    (run_dir / "records" / "external_entrypoint.json").write_text(
        json.dumps(
            {
                "entrypoint_id": entrypoint_id,
                "benchmark_id": benchmark_id,
                "agent_id": agent_id,
                "output_dir": str(external_dir),
                "status": run_record["status"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--entrypoint-id", required=True)
    parser.add_argument("--benchmark-id", default="locomo")
    parser.add_argument("--agent-id", default="openclaw")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--platform-runs-root", required=True)
    args = parser.parse_args(argv)

    run_dir = import_official_run(
        run_id=args.run_id,
        entrypoint_id=args.entrypoint_id,
        benchmark_id=args.benchmark_id,
        agent_id=args.agent_id,
        output_dir=Path(args.output_dir),
        platform_runs_root=Path(args.platform_runs_root),
    )
    print(str(run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
