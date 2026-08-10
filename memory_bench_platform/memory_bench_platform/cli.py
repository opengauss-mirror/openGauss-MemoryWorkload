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
    build_benchmark_scenario,
    build_run_contract,
    build_cases_from_source,
    execute_external_runner,
    execute_smoke_skill,
    run_memory_plugin_task,
    resolve_run_skill_bundle,
    resolve_benchmark_entrypoint,
    score_benchmark_run,
    validate_agent,
    validate_benchmark,
    validate_smoke,
)
from .benchmark_scenario import RunBinding
from .compatibility import resolve_compatibility
from .composer import compose_run_plan
from .loader import load_agent_skill, load_all_skills, load_benchmark_skill
from .paths import SKILLS_ROOT
from .planner import RunPlanRequest, build_run_plan
from .protocol import (
    CaseRecord,
    ExecutionSpec,
    MemoryPluginTaskInput,
    ReportSummary,
    RunRecord,
    StepRecord,
    WorkflowRuntimeContext,
)
from .result_analysis import analyze_run
from .reporter import write_case_results, write_external_result_summary, write_summary
from .resource_monitor import ResourceMonitor
from .storage import RunStorage
from .versioning import build_external_runner_env, build_version_selection
from .workflow import execute_cases


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-skills")

    p_plan = sub.add_parser("plan-run")
    p_plan.add_argument("--benchmark", required=True)
    p_plan.add_argument("--agent", required=True)
    p_plan.add_argument("--memory-backend")
    p_plan.add_argument(
        "--memory-integration",
        choices=["backend_direct", "agent_plugin"],
        default="backend_direct",
    )
    p_plan.add_argument("--hardware-profile")
    p_plan.add_argument("--data-path")
    p_plan.add_argument("--run-id")
    p_plan.add_argument("--version-override", action="append", default=[])

    p_run = sub.add_parser("run")
    p_run.add_argument("--benchmark", required=True)
    p_run.add_argument("--agent", required=True)
    p_run.add_argument("--memory-backend")
    p_run.add_argument(
        "--memory-integration",
        choices=["backend_direct", "agent_plugin"],
        default="backend_direct",
    )
    p_run.add_argument("--hardware-profile")
    p_run.add_argument("--data-path")
    p_run.add_argument("--entrypoint")
    p_run.add_argument("--run-id")
    p_run.add_argument("--smoke-gate")
    p_run.add_argument("--version-override", action="append", default=[])

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--benchmark")
    p_validate.add_argument("--agent")
    p_validate.add_argument("--smoke")
    p_validate.add_argument("--memory-backend")
    p_validate.add_argument("--source-path")
    p_validate.add_argument("--data-path")
    p_validate.add_argument("--api-base", default="https://ark.cn-beijing.volces.com/api/coding/v3")
    p_validate.add_argument("--api-key", default="")
    p_validate.add_argument("--vlm-model", default="doubao-seed-2.0-pro")
    p_validate.add_argument("--embedding-model", default="doubao-embedding-vision")

    p_analyze = sub.add_parser("analyze-run")
    p_analyze.add_argument("--run-dir", required=True)

    p_score = sub.add_parser("score-run")
    p_score.add_argument("--benchmark", required=True)
    p_score.add_argument("--run-dir", required=True)
    p_score.add_argument("--data-path")

    p_run_smoke = sub.add_parser("run-smoke")
    p_run_smoke.add_argument("--smoke", required=True)
    p_run_smoke.add_argument("--run-id")

    return parser


def _plan_from_args(args: argparse.Namespace):
    request = RunPlanRequest(
        benchmark_id=args.benchmark,
        agent_id=args.agent,
        run_id=getattr(args, "run_id", None),
        memory_backend=args.memory_backend,
        memory_integration=args.memory_integration,
        hardware_profile=args.hardware_profile,
        data_path=args.data_path,
    )
    return build_run_plan(request)


def _parse_version_overrides(raw_items: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in raw_items or []:
        if "=" not in item:
            raise SystemExit(f"invalid --version-override value: {item!r}; expected target=version")
        target, version = item.split("=", 1)
        target = target.strip()
        version = version.strip()
        if not target or not version:
            raise SystemExit(f"invalid --version-override value: {item!r}; expected target=version")
        overrides[target] = version
    return overrides


def _build_version_selection(benchmark_manifest, agent_manifest, *, overrides: dict[str, str]) -> dict[str, dict]:
    return {
        "benchmark": build_version_selection(benchmark_manifest, overrides=overrides),
        "agent": build_version_selection(agent_manifest, overrides=overrides),
    }


def _extract_case_result_rows(cases: list[CaseRecord], judge_results: list, step_results: list) -> list[dict]:
    case_map = {case.case_id: case for case in cases}

    rows: list[dict] = []
    for judge in judge_results:
        case = case_map.get(judge.case_id)
        expected_step_id = str(case.reference.get("expected_step_id", "") or "") if case else ""
        if expected_step_id:
            matched_results = [item for item in step_results if item.step_id == expected_step_id]
        else:
            matched_results = [item for item in step_results if item.step_id.startswith(f"{judge.case_id}-")]
        response = ""
        if matched_results:
            structured = matched_results[-1].structured_output
            extractor = str(case.reference.get("evaluation_extractor") or "qa_answer") if case else "qa_answer"
            if extractor == "evidence_text":
                output = structured.get("output", {}) if isinstance(structured, dict) else {}
                response = str(
                    (output.get("evidence_text") if isinstance(output, dict) else "")
                    or structured.get("evidence_text")
                    or ""
                )
            else:
                response = str(
                    structured.get("agent_answer")
                    or structured.get("text_output")
                    or structured.get("stdout_text")
                    or ""
                )
            raw = structured.get("raw", {}) if isinstance(structured, dict) else {}
            raw_stderr = str(raw.get("stderr", "") or "") if isinstance(raw, dict) else ""
        else:
            raw_stderr = ""
        reference = case.reference if case else {}
        rows.append(
            {
                "case_id": judge.case_id,
                "question": str(reference.get("question", "")),
                "expected_answer": str(reference.get("expected_answer", "")),
                "response": response,
                "category": str(reference.get("category", "") or reference.get("question_type", "")),
                "error_detail": raw_stderr or (matched_results[-1].gate_detail if matched_results else ""),
                "label": judge.label,
                "passed": judge.passed,
                "score": judge.score,
                "rationale": judge.rationale,
            }
        )
    return rows


def _summarize_native_evaluation(
    cases: list[CaseRecord],
    steps: list[StepRecord],
    judge_results: list,
    step_results: list,
) -> dict:
    valid_results = [item for item in judge_results if item.passed is not None]
    passed_cases = sum(1 for item in valid_results if item.passed is True)
    failed_cases = sum(1 for item in valid_results if item.passed is False)
    ungraded_cases = len(judge_results) - len(valid_results)

    result_by_id = {item.step_id: item for item in step_results}
    setup_case_ids = [
        case.case_id
        for case in cases
        if "phase:setup" in case.labels
    ]
    ready_checkpoints = 0
    checkpoint_rows: list[dict] = []
    ready_latencies: list[int] = []
    for case_id in setup_case_ids:
        expected = [step for step in steps if step.case_id == case_id]
        ready = all(
            step.step_id in result_by_id and result_by_id[step.step_id].status == "passed"
            for step in expected
        )
        barrier_steps = [
            step
            for step in expected
            if step.operator_kind == "poll"
            or str(step.inputs.get("action") or "")
            in {"flush", "commit", "wait_ready", "status"}
        ]
        latency_ms = sum(
            result_by_id[step.step_id].duration_ms or 0
            for step in barrier_steps
            if step.step_id in result_by_id
        )
        case = next(item for item in cases if item.case_id == case_id)
        checkpoint_rows.append(
            {
                "setup_case_id": case_id,
                "checkpoint_id": case.source_metadata.get("checkpoint_id"),
                "ready": ready,
                "readiness_latency_ms": latency_ms,
                "failed_steps": [
                    step.step_id
                    for step in expected
                    if step.step_id not in result_by_id
                    or result_by_id[step.step_id].status != "passed"
                ],
            }
        )
        if ready:
            ready_checkpoints += 1
            ready_latencies.append(latency_ms)
    executed_runtime = [item for item in step_results if item.status != "skipped"]
    runtime_failures = [item for item in executed_runtime if item.status == "failed"]

    benchmark_score = (
        round(passed_cases / len(valid_results), 4)
        if valid_results
        else None
    )
    checkpoint_ready_rate = (
        round(ready_checkpoints / len(setup_case_ids), 4)
        if setup_case_ids
        else None
    )
    runtime_failure_rate = (
        round(len(runtime_failures) / len(executed_runtime), 4)
        if executed_runtime
        else 0.0
    )
    readiness_latency_ms = (
        round(sum(ready_latencies) / len(ready_latencies), 3)
        if ready_latencies
        else 0.0
    )
    return {
        "case_total": len(judge_results),
        "case_passed": passed_cases,
        "case_failed": failed_cases,
        "case_ungraded": ungraded_cases,
        "benchmark_score": benchmark_score,
        "checkpoint_ready_rate": checkpoint_ready_rate,
        "runtime_failure_rate": runtime_failure_rate,
        "readiness_latency_ms": readiness_latency_ms,
        "checkpoints": checkpoint_rows,
    }


def _write_smoke_result(run_dir: Path, result: dict) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "smoke_trace.json").write_text(
        json.dumps(result["probe"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (reports_dir / "smoke_summary.json").write_text(
        json.dumps(result["validation"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (reports_dir / "smoke_report.html").write_text(
        str(result["report"].get("html", "")),
        encoding="utf-8",
    )


def _build_smoke_summary(run_id: str, smoke_id: str, result: dict) -> dict:
    validation = result["validation"]
    return {
        "run_id": run_id,
        "status": "passed" if str(validation.get("status", "")).lower() == "passed" else "failed",
        "case_total": len(validation.get("stage_results", [])),
        "case_passed": sum(1 for item in validation.get("stage_results", []) if item.get("passed")),
        "case_failed": sum(1 for item in validation.get("stage_results", []) if not item.get("passed")),
        "category_summary": {},
        "resource_summary": {
            "smoke_id": smoke_id,
            "issues": validation.get("issues", []),
        },
    }


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "list-skills":
        loaded = load_all_skills(SKILLS_ROOT)
        payload = {
            "benchmarks": [skill.id for skill in loaded["benchmarks"]],
            "agents": [skill.id for skill in loaded["agents"]],
            "memories": [skill.id for skill in loaded["memories"]],
            "memory_plugins": [skill.id for skill in loaded["memory_plugins"]],
            "smokes": [skill.id for skill in loaded["smokes"]],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.command == "validate":
        payload: dict[str, dict] = {}
        if args.benchmark:
            payload["benchmark"] = validate_benchmark(args.benchmark, args.data_path)
        if args.agent:
            payload["agent"] = validate_agent(args.agent)
        if args.smoke:
            payload["smoke"] = validate_smoke(args.smoke)
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
        if args.benchmark and args.agent:
            payload["run_contract"] = build_run_contract(
                args.benchmark,
                args.agent,
                args.memory_backend,
                getattr(args, "memory_integration", "backend_direct"),
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.command == "analyze-run":
        run_dir = Path(args.run_dir)
        analyze_run(run_dir)
        print(str(run_dir))
        return

    if args.command == "score-run":
        payload = score_benchmark_run(args.benchmark, Path(args.run_dir), args.data_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.command == "run-smoke":
        run_id = args.run_id or f"{args.smoke}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        storage = RunStorage(Path.cwd() / "runs")
        run_record = RunRecord(
            run_id=run_id,
            source_id=f"smoke:{args.smoke}",
            source_kind="native_workflow",
            operator_targets=[],
            status="running",
            started_at=datetime.now(),
        )
        run_dir = storage.init_run(run_record)
        result = execute_smoke_skill(args.smoke, run_dir)
        _write_smoke_result(run_dir, result)
        summary = _build_smoke_summary(run_id, args.smoke, result)
        status = summary["status"]
        write_summary(run_dir, summary)
        write_case_results(run_dir, result["validation"].get("stage_results", []))
        run_record.status = status
        run_record.ended_at = datetime.now()
        storage.write_run_record(run_dir, run_record)
        print(str(run_dir))
        return

    plan = _plan_from_args(args)
    version_overrides = _parse_version_overrides(getattr(args, "version_override", []))

    if args.command == "plan-run":
        payload = asdict(plan)
        payload["run_contract"] = build_run_contract(
            args.benchmark,
            args.agent,
            args.memory_backend,
            args.memory_integration,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    storage = RunStorage(Path.cwd() / "runs")
    benchmark_manifest = load_benchmark_skill(SKILLS_ROOT, args.benchmark)
    agent_manifest = load_agent_skill(SKILLS_ROOT, args.agent)
    run_contract = build_run_contract(
        args.benchmark,
        args.agent,
        args.memory_backend,
        args.memory_integration,
    )
    entrypoint = resolve_benchmark_entrypoint(args.benchmark, getattr(args, "entrypoint", None))
    source_kind = (
        "external_benchmark_runner"
        if entrypoint.entrypoint_kind == "external_runner"
        else "benchmark_scenario"
        if entrypoint.entrypoint_kind == "scenario_builder"
        else "benchmark_case_source"
    )
    run_record = RunRecord(
        run_id=plan.run_id,
        source_id=f"{plan.benchmark_id}:{entrypoint.entrypoint_id}" if args.entrypoint else plan.benchmark_id,
        source_kind=source_kind,
        operator_targets=[args.agent],
        benchmark_skill_version=benchmark_manifest.version,
        benchmark_version=plan.benchmark_version,
        agent_id=plan.agent_id,
        agent_skill_version=agent_manifest.version,
        agent_version=plan.agent_version,
        memory_backend=str(run_contract["selection"].get("memory_id") or plan.memory_backend or ""),
        hardware_profile=plan.hardware_profile,
        benchmark_version_policy=benchmark_manifest.version_policy.model_dump(mode="json"),
        agent_version_policy=agent_manifest.version_policy.model_dump(mode="json"),
        version_selection=_build_version_selection(benchmark_manifest, agent_manifest, overrides=version_overrides),
        config=(
            {
                key: value
                for key, value in {
                    "data_path": args.data_path,
                    "memory_integration": args.memory_integration,
                    "memory_plugin_id": run_contract["selection"].get("memory_plugin_id"),
                    "version_overrides": version_overrides or None,
                }.items()
                if value
            }
        ),
        status="pending",
    )
    run_record.status = "running"
    run_record.started_at = datetime.now()
    run_dir = storage.init_run(run_record)
    storage.write_json_record(
        run_dir,
        "records/version_selection.json",
        run_record.version_selection,
    )
    storage.write_json_record(run_dir, "records/run_contract.json", run_contract)

    if getattr(args, "smoke_gate", None):
        smoke_result = execute_smoke_skill(args.smoke_gate, run_dir)
        _write_smoke_result(run_dir, smoke_result)
        storage.write_json_record(run_dir, "records/smoke_gate.json", smoke_result)
        smoke_status = str(smoke_result["validation"].get("status", "")).lower()
        if smoke_status != "passed":
            summary_record = ReportSummary(
                run_id=run_record.run_id,
                status="failed",
                case_total=0,
                case_passed=0,
                case_failed=0,
                resource_summary={
                    "smoke_gate": {
                        "smoke_id": args.smoke_gate,
                        "issues": smoke_result["validation"].get("issues", []),
                    }
                },
                category_summary={},
            )
            run_record.status = "failed"
            run_record.ended_at = datetime.now()
            storage.write_run_record(run_dir, run_record)
            write_summary(run_dir, summary_record.model_dump(mode="json"))
            write_case_results(run_dir, smoke_result["validation"].get("stage_results", []))
            analyze_run(run_dir)
            print(str(run_dir))
            return

    if entrypoint.entrypoint_kind == "external_runner":
        output_dir = run_dir / "external_artifacts" / entrypoint.entrypoint_id
        monitor = ResourceMonitor(run_dir / "artifacts" / "monitor", Path.cwd(), "/", "lo")
        monitor.setup_writers()
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
        env.update(build_external_runner_env(run_record.version_selection))
        monitor.start_background_sampling()
        try:
            runner_result = execute_external_runner(entrypoint, env=env, cwd=Path.cwd().parent)
        finally:
            monitor.stop_background_sampling()
        cpu_snapshot = monitor.capture_once()
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
                    resource_summary={"external_error": str(exc), "cpu": cpu_snapshot},
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
                imported_validity = imported["summary"].get("run_validity", {}) if isinstance(imported.get("summary"), dict) else {}
                is_valid_run = bool(imported_validity.get("valid", True))
                summary_record = ReportSummary(
                    run_id=run_record.run_id,
                    status=(
                        "partial"
                        if not is_valid_run
                        else "passed" if runner_result["status"] == "passed" else "failed"
                    ),
                    case_total=imported["summary"]["total_questions"],
                    case_passed=imported["summary"]["total_correct"],
                    case_failed=imported["summary"]["total_questions"] - imported["summary"]["total_correct"],
                    resource_summary={
                        "cpu": cpu_snapshot,
                        "token_totals": imported["summary"].get("token_totals", {}),
                        "memory_token_totals": imported["summary"].get("memory_token_totals", {}),
                        "ungraded_count": imported["summary"].get("ungraded_count", 0),
                        "run_validity": imported_validity,
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
                resource_summary={"external_error": f"missing external output dir: {output_dir}", "cpu": cpu_snapshot},
                category_summary={},
            )
        final_status = summary_record.status
        run_record.status = final_status
        run_record.ended_at = datetime.now()
        storage.write_run_record(run_dir, run_record)
        write_summary(run_dir, summary_record.model_dump(mode="json"))
        write_case_results(run_dir, case_results)
        analyze_run(run_dir)
        print(str(run_dir))
        return

    if entrypoint.entrypoint_kind == "scenario_builder":
        scenario = build_benchmark_scenario(args.benchmark, args.data_path)
        bundle = resolve_run_skill_bundle(
            args.benchmark,
            args.agent,
            args.memory_backend,
            args.memory_integration,
        )
        binding = RunBinding(
            benchmark_id=args.benchmark,
            agent_id=args.agent,
            agent_runtime_id=str(bundle.agent.runtime.get("agent_id") or "").strip() or None,
            agent_local=(
                bool(bundle.agent.runtime.get("local", False))
                or os.environ.get("MEMORY_BENCH_AGENT_LOCAL", "").strip().lower()
                in {"1", "true", "yes", "on"}
            ),
            memory_id=bundle.memory_id,
            memory_integration=args.memory_integration,
            memory_plugin_id=bundle.memory_plugin_id,
            run_id=run_record.run_id,
        )
        compatibility = resolve_compatibility(
            scenario,
            binding,
            agent=bundle.agent,
            memory=bundle.memory,
            memory_plugin=bundle.memory_plugin,
        )
        storage.write_json_record(
            run_dir,
            "records/benchmark_scenario.json",
            scenario.model_dump(mode="json"),
        )
        storage.write_json_record(
            run_dir,
            "records/run_binding.json",
            binding.model_dump(mode="json"),
        )
        storage.write_json_record(
            run_dir,
            "records/compatibility_result.json",
            compatibility.model_dump(mode="json"),
        )
        storage.write_json_record(
            run_dir,
            "records/runtime_capabilities.json",
            compatibility.resolved_capabilities,
        )
        storage.write_json_record(
            run_dir,
            "records/evaluation_profile.json",
            {
                "default": scenario.evaluation.model_dump(mode="json"),
                "checkpoints": [
                    {
                        "sample_id": sample.sample_id,
                        "checkpoint_id": event.event_id,
                        "evaluation": event.evaluation.model_dump(mode="json"),
                    }
                    for sample in scenario.samples
                    for event in sample.timeline
                    if event.evaluation is not None
                ],
            },
        )
        if not compatibility.compatible:
            missing = ", ".join(compatibility.missing_capabilities)
            raise ValueError(f"runtime is incompatible with benchmark scenario: {missing}")
        cases_payload = compose_run_plan(
            scenario,
            binding,
            compatibility.resolved_capabilities,
        )
        storage.write_json_record(
            run_dir,
            "records/composed_run_plan.json",
            cases_payload,
        )
    else:
        cases_payload = build_cases_from_source(
            args.benchmark,
            args.data_path,
            args.memory_integration,
            run_record.run_id,
        )
    declared_source_kind = str(cases_payload.get("source_kind", "") or "")
    if declared_source_kind:
        run_record.source_kind = declared_source_kind
        storage.write_run_record(run_dir, run_record)
    cases = [CaseRecord(run_id=run_record.run_id, **item) for item in cases_payload.get("cases", [])]
    steps = [StepRecord(**item) for item in cases_payload.get("steps", [])]
    execution_spec = ExecutionSpec(**cases_payload.get("execution_spec", {}))
    resolved_memory_id = str(run_contract["selection"].get("memory_id") or "").strip() or None
    runtime_context = WorkflowRuntimeContext(
        run_id=run_record.run_id,
        run_dir=str(run_dir),
        benchmark_id=str(run_contract["selection"]["benchmark_id"]),
        agent_id=str(run_contract["selection"]["agent_id"]),
        memory_id=resolved_memory_id,
        memory_integration=args.memory_integration,
        memory_plugin_id=str(run_contract["selection"].get("memory_plugin_id") or "") or None,
        run_contract=run_contract,
        version_selection=run_record.version_selection,
    )

    monitor = ResourceMonitor(run_dir / "artifacts" / "monitor", Path.cwd(), "/", "lo")
    monitor.setup_writers()
    monitor.start_background_sampling()
    plugin_finalize_payload = None
    try:
        workflow_output = execute_cases(
            run_id=run_record.run_id,
            agent_id=args.agent,
            memory_id=resolved_memory_id,
            runtime_context=runtime_context,
            cases=cases,
            steps=steps,
            execution_spec=execution_spec,
            run_dir=run_dir,
        )
    finally:
        memory_plugin_id = runtime_context.memory_plugin_id
        if memory_plugin_id:
            try:
                plugin_finalize_payload = run_memory_plugin_task(
                    memory_plugin_id,
                    MemoryPluginTaskInput(
                        task_id="run-memory-plugin-finalize",
                        action="finalize",
                        inputs={},
                        runtime_context=runtime_context,
                        idempotency_key=f"{run_record.run_id}:memory-plugin-finalize",
                    ),
                ).model_dump(mode="json")
            except Exception as exc:
                plugin_finalize_payload = {
                    "status": "failed",
                    "state": "failed",
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            storage.write_json_record(
                run_dir,
                "records/memory_plugin_finalize.json",
                plugin_finalize_payload,
            )
        monitor.stop_background_sampling()
    cpu_snapshot = monitor.capture_once()

    judge_results = workflow_output["judge_results"]
    evaluation_summary = _summarize_native_evaluation(
        cases,
        steps,
        judge_results,
        workflow_output["step_results"],
    )
    if not judge_results or evaluation_summary["benchmark_score"] is None:
        final_status = "failed"
    elif evaluation_summary["case_ungraded"] > 0:
        final_status = "partial"
    else:
        final_status = (
            "passed" if evaluation_summary["case_failed"] == 0 else "partial"
        )
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
        "records/artifacts.json",
        [item.model_dump(mode="json") for item in workflow_output["artifacts"]],
    )
    storage.write_json_record(
        run_dir,
        "records/checkpoint_readiness.json",
        {
            "checkpoint_ready_rate": evaluation_summary["checkpoint_ready_rate"],
            "readiness_latency_ms": evaluation_summary["readiness_latency_ms"],
            "runtime_failure_rate": evaluation_summary["runtime_failure_rate"],
            "checkpoints": evaluation_summary["checkpoints"],
        },
    )
    evaluation_metrics = [
        {
            "metric_id": f"{run_record.run_id}-{name.replace('_', '-')}",
            "run_id": run_record.run_id,
            "case_id": None,
            "step_id": None,
            "scope": "run",
            "name": name,
            "value": value,
            "unit": "ratio" if name.endswith("rate") or name == "benchmark_score" else "ms",
            "dimension": {},
        }
        for name, value in (
            ("benchmark_score", evaluation_summary["benchmark_score"]),
            ("checkpoint_ready_rate", evaluation_summary["checkpoint_ready_rate"]),
            ("runtime_failure_rate", evaluation_summary["runtime_failure_rate"]),
            ("readiness_latency_ms", evaluation_summary["readiness_latency_ms"]),
        )
        if value is not None
    ]
    storage.write_json_record(
        run_dir,
        "records/metrics.json",
        [item.model_dump(mode="json") for item in workflow_output["metrics"]]
        + evaluation_metrics
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
        case_total=evaluation_summary["case_total"],
        case_passed=evaluation_summary["case_passed"],
        case_failed=evaluation_summary["case_failed"],
        case_ungraded=evaluation_summary["case_ungraded"],
        benchmark_score=evaluation_summary["benchmark_score"],
        checkpoint_ready_rate=evaluation_summary["checkpoint_ready_rate"],
        runtime_failure_rate=evaluation_summary["runtime_failure_rate"],
        readiness_latency_ms=evaluation_summary["readiness_latency_ms"],
        resource_summary={
            "cpu": cpu_snapshot,
            "evaluation": evaluation_summary,
        },
        category_summary={},
    )
    write_summary(run_dir, summary_record.model_dump(mode="json"))
    write_case_results(
        run_dir,
        _extract_case_result_rows(cases, judge_results, workflow_output["step_results"]),
    )
    analyze_run(run_dir)
    print(str(run_dir))


if __name__ == "__main__":
    main()
