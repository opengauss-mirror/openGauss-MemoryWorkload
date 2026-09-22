"""Dedicated route: multi-instance runs do not have one memory-backend contract."""
import json
import sys
import uuid
from pathlib import Path

from .multi_user_config import load_config, preflight
from .multi_user_replay import run_multi_user


def execute(args, argv=None):
    if args.benchmark != "production-http-replay":
        raise SystemExit("--workload-config requires production-http-replay")
    # Reject options that would otherwise be silently ignored by the dedicated route.
    for name in ("memory_backend", "entrypoint", "smoke_gate", "version_override", "hardware_profile", "smoke", "trace"):
        if getattr(args, name, None):
            raise SystemExit("multi-user mode uses instance configuration; incompatible CLI option")
    if getattr(args, "trace_mode", "off") != "off":
        raise SystemExit("multi-user model/trace endpoints must be configured per instance")
    try:
        config = load_config(Path(args.workload_config))
        if (config.mode == "openclaw" and args.agent != "openclaw") or (config.mode == "backend_direct" and args.agent != "none"):
            raise ValueError("mode/agent mismatch: use openclaw or none respectively")
        arguments = sys.argv[1:] if argv is None else argv
        if any(a == "--memory-integration" or a.startswith("--memory-integration=") for a in arguments):
            expected = "agent_plugin" if config.mode == "openclaw" else "backend_direct"
            if args.memory_integration != expected:
                raise ValueError("mode/memory-integration mismatch")
        preflight(config)
    except Exception:
        raise SystemExit("multi-user preflight failed: check config, files, instance environment and skill capabilities") from None
    if args.command in {"plan-run", "validate"}:
        print(json.dumps({"status": "validated", "mode": config.mode, "users": len(config.users),
                          "instances": len({u.instance for u in config.users}),
                          "runtime_health_and_isolation": "checked_at_run_start"}))
        return
    run_id = args.run_id or "multi-" + uuid.uuid4().hex[:16]
    output = Path.cwd() / "runs" / run_id
    try:
        report = run_multi_user(config, output, run_id)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        raise SystemExit("multi-user run failed; inspect partial reports if present") from None
    print(str(output))
    if report["status"] != "completed":
        raise SystemExit(1)
