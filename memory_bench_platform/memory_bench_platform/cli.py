from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .backends import validate_openviking_source
from .integration import build_benchmark_tasks, run_agent_task, validate_agent, validate_benchmark
from .loader import load_all_skills
from .paths import SKILLS_ROOT
from .planner import RunPlanRequest, build_run_plan
from .protocol import RenderedTaskInput
from .reporter import write_summary
from .storage import RunStorage


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

    p_run = sub.add_parser("run")
    p_run.add_argument("--benchmark", required=True)
    p_run.add_argument("--agent", required=True)
    p_run.add_argument("--memory-backend")
    p_run.add_argument("--hardware-profile")
    p_run.add_argument("--data-path")

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
    run_record = plan.to_run_record()
    run_record.status = "running"
    run_record.started_at = datetime.now()
    run_dir = storage.init_run(run_record)
    tasks_payload = build_benchmark_tasks(args.benchmark, args.data_path)
    tasks = tasks_payload.get("tasks", [])
    agent_output = None
    if tasks:
        first_task = tasks[0]
        rendered = RenderedTaskInput(
            task_id=first_task["task_id"],
            messages=[{"role": "user", "content": first_task.get("question", "")}],
            metadata={"sample_id": first_task.get("sample_id", "")},
        )
        agent_output = run_agent_task(args.agent, rendered)
    final_status = "partial" if agent_output is not None else "stubbed"
    run_record.status = final_status
    run_record.ended_at = datetime.now()
    storage.write_run_record(run_dir, run_record)
    summary = {
        "run_id": plan.run_id,
        "benchmark_id": plan.benchmark_id,
        "agent_id": plan.agent_id,
        "status": final_status,
        "task_count": len(tasks),
        "first_task_id": tasks[0]["task_id"] if tasks else None,
        "agent_output_status": None if agent_output is None else agent_output.get("status"),
    }
    (run_dir / "records" / "tasks.json").write_text(
        json.dumps(tasks_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if agent_output is not None:
        (run_dir / "artifacts" / "agent-output.json").write_text(
            json.dumps(agent_output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    write_summary(run_dir, summary)
    print(str(run_dir))


if __name__ == "__main__":
    main()
