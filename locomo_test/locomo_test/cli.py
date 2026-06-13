"""CLI entry point — locomo-test <command>."""

from __future__ import annotations

import argparse
import sys

from .config import load_config
from .pipeline import run_pipeline


def cmd_run(args):
    cfg = load_config(args.config)

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    skip = [s.strip() for s in args.skip.split(",")] if args.skip else None

    run_pipeline(cfg, only=only, skip=skip, resume=args.resume)


def cmd_judge(args):
    from .config import Config, JudgeEnv
    from .judge import run_judge
    import os

    cfg = Config()
    if args.config:
        cfg = load_config(args.config)
    else:
        cfg.judge_env = JudgeEnv(
            api_key=args.token or os.environ.get("ARK_API_KEY", ""),
            base_url=args.base_url,
            model=args.model,
            parallel=args.parallel,
        )

    output_dir = str(args.input.parent) if hasattr(args.input, 'parent') else os.path.dirname(args.input)
    # Override csv_path by putting the file in expected location
    import shutil
    expected = os.path.join(output_dir, "qa_results.csv")
    if os.path.abspath(args.input) != os.path.abspath(expected):
        # judge.py expects qa_results.csv in output_dir
        shutil.copy2(args.input, expected)

    run_judge(cfg, output_dir)


def cmd_check(args):
    from .config import load_config
    from .checks import check_health
    cfg = load_config(args.config)
    ok = check_health(cfg)
    if not ok:
        sys.exit(1)
    print("All services healthy.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        prog="locomo-test",
        description="LoCoMo benchmark test toolkit for OpenClaw",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    p_run = sub.add_parser("run", help="Run the full pipeline")
    p_run.add_argument("config", help="Path to test config TOML file")
    p_run.add_argument("--only", default=None, help="Comma-separated steps to run (e.g. ingest,qa)")
    p_run.add_argument("--skip", default=None, help="Comma-separated steps to skip (e.g. judge)")
    p_run.add_argument("--resume", action="store_true", help="Skip health_check and ingest, resume from qa")
    p_run.set_defaults(func=cmd_run)

    # --- judge ---
    p_judge = sub.add_parser("judge", help="Run judge on an existing CSV")
    p_judge.add_argument("--input", required=True, help="Path to QA results CSV")
    p_judge.add_argument("--config", default=None, help="Optional: test config TOML for judge settings")
    p_judge.add_argument("--token", default=None, help="API token")
    p_judge.add_argument("--base-url", default="https://ark.cn-beijing.volces.com/api/v3")
    p_judge.add_argument("--model", default="", help="Judge LLM model name (required)")
    p_judge.add_argument("--parallel", type=int, default=5)
    p_judge.set_defaults(func=cmd_judge)

    # --- check ---
    p_check = sub.add_parser("check", help="Check service health")
    p_check.add_argument("config", help="Path to test config TOML file")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
