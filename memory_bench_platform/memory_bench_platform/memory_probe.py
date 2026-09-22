"""Agent-free smoke check for memory/1 backends; writes a fresh synthetic episode."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .composer import _direct_barrier_policy
from .integration import get_memory_manifest
from .paths import SKILLS_ROOT
from .protocol import MemoryTaskInput, MemoryTaskOutput, WorkflowRuntimeContext


def probe(backend, run_dir, *, ready_timeout=120, invoke=None):
    """Exercise the public contract without deploying, patching or invoking an agent."""
    if not math.isfinite(ready_timeout) or ready_timeout <= 0:
        raise ValueError("ready_timeout must be finite and positive")
    manifest = get_memory_manifest(backend)
    commit, readiness = _direct_barrier_policy({"memory": manifest.capabilities})
    required = {"ingest", "recall"} | ({"flush"} if commit else set()) | ({"status"} if readiness else set())
    if not required.issubset(set(manifest.capabilities.get("actions", []))):
        raise ValueError("Backend does not declare the required memory/1 actions")
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    run_id = "memory-probe-" + uuid.uuid4().hex
    scope = run_id + ":sample"
    report = {"run_id": run_id, "backend": backend, "status": "running", "steps": []}
    context = WorkflowRuntimeContext(run_id=run_id, run_dir=str(run_dir),
                                    benchmark_id="memory-probe", agent_id="none", memory_id=backend)

    def call(action, inputs, timeout=None):
        task_id = f"{run_id}:{len(report['steps'])}:{action}"
        request = MemoryTaskInput(task_id=task_id, action=action,
                                  inputs={"scope_id": scope, **inputs},
                                  runtime_context=context, idempotency_key=task_id)
        limit = float(manifest.capabilities.get("action_timeouts_seconds", {}).get(action, 90))
        if timeout is not None:
            limit = min(limit, timeout)
        if invoke is None:
            proc = subprocess.run([sys.executable, str(SKILLS_ROOT / "memories" / backend / manifest.entry.runner)],
                                  input=request.model_dump_json(), capture_output=True, text=True,
                                  timeout=limit, check=True)
            result = MemoryTaskOutput.model_validate_json(proc.stdout)
        else:
            result = MemoryTaskOutput.model_validate(invoke(request))
        report["steps"].append({"action": action, "scope_id": request.inputs["scope_id"],
                                "result": result.model_dump(mode="json")})
        if result.status != "ok" or result.state == "failed":
            raise RuntimeError(f"{action} failed; see recorded step error")
        return result

    try:
        written = call("ingest", {"content": "Nora: My name is Nora. I live in Hangzhou. I have lived in Hangzhou for five years.",
                                  "occurred_at": datetime.now(timezone.utc).isoformat()})
        operation = written.operation
        if commit:
            operation = call("flush", {"session_id": written.output.get("session_id"),
                                        "operation": operation}).operation
        if readiness:
            deadline = time.monotonic() + ready_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Index readiness deadline exceeded")
                if call("status", {"operation": operation}, timeout=remaining).state == "completed":
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        recalled = call("recall", {"query": "Which city does Nora live in?", "node_limit": 3})
        if recalled.state != "completed" or "hangzhou" not in str(recalled.output.get("evidence_text", "")).lower():
            raise RuntimeError("Written fact was not recovered in completed recall evidence")
        isolated = call("recall", {"scope_id": run_id + ":empty", "query": "Which city does Nora live in?", "node_limit": 3})
        if isolated.state != "completed" or isolated.output.get("evidence_text") or isolated.output.get("memories"):
            raise RuntimeError("Empty episode recall was not completed and empty")
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        # Do not serialize subprocess output or configuration into error messages.
        report["error"] = {"type": type(exc).__name__, "message": str(exc) if isinstance(exc, (RuntimeError, TimeoutError)) else "Probe execution failed"}
    finally:
        (run_dir / "probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-backend", required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory for the probe record")
    parser.add_argument("--ready-timeout", type=float, default=120)
    args = parser.parse_args()
    if not math.isfinite(args.ready_timeout) or args.ready_timeout <= 0:
        parser.error("--ready-timeout must be finite and positive")
    report = probe(args.memory_backend, args.output, ready_timeout=args.ready_timeout)
    print(json.dumps({"status": report["status"], "report": str(args.output.resolve() / "probe.json")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
