from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
import os
from pathlib import Path

from .backends import validate_openviking_source
from .external_report_import import import_external_result
from .integration import (
    build_cases_from_source,
    execute_external_runner,
    resolve_benchmark_entrypoint,
    validate_agent,
    validate_benchmark,
)
from .loader import load_all_skills
from .paths import SKILLS_ROOT
from .planner import RunPlanRequest, build_run_plan
from .protocol import CaseRecord, ExecutionSpec, ReportSummary, RunRecord, StepRecord
from .reporter import write_case_results, write_external_result_summary, write_summary
from .resource_monitor import ResourceMonitor
from .storage import RunStorage
from .workflow import execute_cases


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-skills")

    p_plan = sub.add_parser("plan-run")
    p_plan.add_argument("--benchmark", required=True)
    p_plan.add_argument("--agent", required=True)
    p_plan.add_argument("--memory-backend")
    p_plan.add_argument("--hardware-profile")
    p_plan.add_argument("--data-path")
    p_plan.add_argument("--run-id")

    p_run = sub.add_parser("run")
    p_run.add_argument("--benchmark", required=True)
    p_run.add_argument("--agent", required=True)
    p_run.add_argument("--memory-backend")
    p_run.add_argument("--hardware-profile")
    p_run.add_argument("--data-path")
    p_run.add_argument("--entrypoint")
    p_run.add_argument("--run-id")

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--benchmark")
    p_validate.add_argument("--agent")
    p_validate.add_argument("--memory-backend")
    p_validate.add_argument("--source-path")
    p_validate.add_argument("--data-path")
    p_validate.add_argument("--api-base", default="https://ark.cn-beijing.volces.com/api/coding/v3")
    p_validate.add_argument("--api-key", default="")
    p_validate.add_argument("--vlm-model", default="doubao-seed-2.0-pro")
    p_validate.add_argument("--embedding-model", default="doubao-embedding-vision")

    return parser


def _plan_from_args(args: argparse.Namespace):
    request = RunPlanRequest(
        benchmark_id=args.benchmark,
        agent_id=args.agent,
        run_id=getattr(args, "run_id", None),
        memory_backend=args.memory_backend,
        hardware_profile=args.hardware_profile,
        data_path=args.data_path,
    )
    return build_run_plan(request)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "list-skills":
        loaded = load_all_skills(SKILLS_ROOT)
        payload = {
            "benchmarks": [skill.id for skill in loaded["benchmarks"]],
            "agents": [skill.id for skill in loaded["agents"]],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.command == "validate":
        payload: dict[str, dict] = {}
        if args.benchmark:
            payload["benchmark"] = validate_benchmark(args.benchmark, args.data_path)
        if args.agent:
            payload["agent"] = validate_agent(args.agent)
        if args.memory_backend == "openviking":
            if not args.source_path:
                raise SystemExit("--source-path is required for --memory-backend openviking")
            payload["memory_backend"] = validate_openviking_source(
                args.source_path,
                api_base=args.api_base,
                api_key=args.api_key,
                vlm_model=args.vlm_model,
                embedding_model=args.embedding_model,
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    plan = _plan_from_args(args)

    if args.command == "plan-run":
        print(json.dumps(asdict(plan), ensure_ascii=False, indent=2))
        return

    storage = RunStorage(Path.cwd() / "runs")
    entrypoint = resolve_benchmark_entrypoint(args.benchmark, getattr(args, "entrypoint", None))
    source_kind = "external_benchmark_runner" if entrypoint.entrypoint_kind == "external_runner" else "benchmark_case_source"
    run_record = RunRecord(
        run_id=plan.run_id,
        source_id=f"{plan.benchmark_id}:{entrypoint.entrypoint_id}" if args.entrypoint else plan.benchmark_id,
        source_kind=source_kind,
        operator_targets=[args.agent],
        benchmark_version=plan.benchmark_version,
        agent_id=plan.agent_id,
        agent_version=plan.agent_version,
        memory_backend=plan.memory_backend,
        hardware_profile=plan.hardware_profile,
        config={"data_path": args.data_path} if args.data_path else {},
        status="pending",
    )
    run_record.status = "running"
    run_record.started_at = datetime.now()
    run_dir = storage.init_run(run_record)

    if entrypoint.entrypoint_kind == "external_runner":
        output_dir = run_dir / "external_artifacts" / entrypoint.entrypoint_id
        env = os.environ.copy()
        env.update(
            {
                "RUN_ID": plan.run_id,
                "OUTPUT_DIR": str(output_dir),
                "DATA_PATH": args.data_path or "",
                "BENCHMARK_ID": args.benchmark,
                "AGENT_ID": args.agent,
            }
        )
        runner_result = execute_external_runner(entrypoint, env=env, cwd=Path.cwd().parent)
        (run_dir / "logs" / "external_runner.stdout.log").write_text(runner_result["stdout"], encoding="utf-8")
        (run_dir / "logs" / "external_runner.stderr.log").write_text(runner_result["stderr"], encoding="utf-8")
        if output_dir.exists():
            try:
                imported = import_external_result(output_dir)
            except FileNotFoundError as exc:
                storage.write_json_record(
                    run_dir,
                    "records/external_entrypoint.json",
                    {
                        "entrypoint_id": entrypoint.entrypoint_id,
                        "command": entrypoint.command,
                        "output_dir": str(output_dir),
                        "status": "failed",
                        "exit_code": runner_result["exit_code"],
                        "error": str(exc),
                    },
                )
                case_results = []
                summary_record = ReportSummary(
                    run_id=run_record.run_id,
                    status="failed",
                    case_total=0,
                    case_passed=0,
                    case_failed=0,
                    resource_summary={"external_error": str(exc)},
                    category_summary={},
                )
            else:
                storage.write_json_record(
                    run_dir,
                    "records/external_entrypoint.json",
                    {
                        "entrypoint_id": entrypoint.entrypoint_id,
                        "command": entrypoint.command,
                        "output_dir": str(output_dir),
                        "status": runner_result["status"],
                        "exit_code": runner_result["exit_code"],
                    },
                )
                write_external_result_summary(run_dir, imported)
                case_results = imported["case_results"]
                summary_record = ReportSummary(
                    run_id=run_record.run_id,
                    status="passed" if runner_result["status"] == "passed" else "failed",
                    case_total=imported["summary"]["total_questions"],
                    case_passed=imported["summary"]["total_correct"],
                    case_failed=imported["summary"]["total_graded"] - imported["summary"]["total_correct"],
                    resource_summary={
                        "token_totals": imported["summary"].get("token_totals", {}),
                        "memory_token_totals": imported["summary"].get("memory_token_totals", {}),
                    },
                    category_summary=imported["summary"].get("accuracy_by_category", {}),
                )
        else:
            storage.write_json_record(
                run_dir,
                "records/external_entrypoint.json",
                {
                    "entrypoint_id": entrypoint.entrypoint_id,
                    "command": entrypoint.command,
                    "output_dir": str(output_dir),
                    "status": "failed",
                    "exit_code": runner_result["exit_code"],
                    "error": f"missing external output dir: {output_dir}",
                },
            )
            case_results = []
            summary_record = ReportSummary(
                run_id=run_record.run_id,
                status="failed",
                case_total=0,
                case_passed=0,
                case_failed=0,
                resource_summary={"external_error": f"missing external output dir: {output_dir}"},
                category_summary={},
            )
        final_status = summary_record.status
        run_record.status = final_status
        run_record.ended_at = datetime.now()
        storage.write_run_record(run_dir, run_record)
        write_summary(run_dir, summary_record.model_dump(mode="json"))
        write_case_results(run_dir, case_results)
        print(str(run_dir))
        return

    cases_payload = build_cases_from_source(args.benchmark, args.data_path)
    cases = [CaseRecord(run_id=run_record.run_id, **item) for item in cases_payload.get("cases", [])]
    steps = [StepRecord(**item) for item in cases_payload.get("steps", [])]
    execution_spec = ExecutionSpec(**cases_payload.get("execution_spec", {}))

    monitor = ResourceMonitor(run_dir / "artifacts" / "monitor", Path.cwd(), "/", "lo")
    monitor.setup_writers()
    cpu_snapshot = monitor.capture_once()

    workflow_output = execute_cases(
        run_id=run_record.run_id,
        agent_id=args.agent,
        cases=cases,
        steps=steps,
        execution_spec=execution_spec,
        run_dir=run_dir,
    )

    judge_results = workflow_output["judge_results"]
    passed_cases = sum(1 for item in judge_results if item.passed)
    failed_cases = len(judge_results) - passed_cases
    final_status = "passed" if judge_results and failed_cases == 0 else "partial"
    run_record.status = final_status
    run_record.ended_at = datetime.now()
    storage.write_run_record(run_dir, run_record)

    storage.write_json_record(run_dir, "records/cases.json", [item.model_dump(mode="json") for item in cases])
    storage.write_json_record(run_dir, "records/steps.json", [item.model_dump(mode="json") for item in steps])
    storage.write_json_record(
        run_dir,
        "records/step_results.json",
        [item.model_dump(mode="json") for item in workflow_output["step_results"]],
    )
    storage.write_json_record(
        run_dir,
        "records/traces.json",
        [item.model_dump(mode="json") for item in workflow_output["traces"]],
    )
    storage.write_json_record(
        run_dir,
        "records/judge_results.json",
        [item.model_dump(mode="json") for item in judge_results],
    )
    storage.write_json_record(
        run_dir,
        "records/metrics.json",
        [item.model_dump(mode="json") for item in workflow_output["metrics"]]
        + [
            {
                "metric_id": f"{run_record.run_id}-cpu-idle",
                "run_id": run_record.run_id,
                "case_id": None,
                "step_id": None,
                "scope": "run",
                "name": "cpu_idle_percent",
                "value": cpu_snapshot["summary_util_idle"],
                "unit": "percent",
                "dimension": {},
            }
        ],
    )

    summary_record = ReportSummary(
        run_id=run_record.run_id,
        status=final_status,
        case_total=len(cases),
        case_passed=passed_cases,
        case_failed=failed_cases,
        resource_summary={"cpu": cpu_snapshot},
        category_summary={},
    )
    write_summary(run_dir, summary_record.model_dump(mode="json"))
    write_case_results(
        run_dir,
        [item.model_dump(mode="json") for item in judge_results],
    )
    print(str(run_dir))


if __name__ == "__main__":
    main()
