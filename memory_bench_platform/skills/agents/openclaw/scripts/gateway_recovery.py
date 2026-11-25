"""Explicit lifecycle operations for a sample-owned OpenClaw Docker Gateway."""
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import time
import urllib.request


def dedicated_container(runtime_dir, container_env):
    container = os.environ.get(container_env, "")
    if not container:
        raise ValueError(f"{container_env} must identify a dedicated Docker Gateway")
    result = subprocess.run(["docker", "inspect", container], check=True, capture_output=True, text=True, timeout=15)
    info = json.loads(result.stdout)[0]
    if (info.get("Config", {}).get("Labels") or {}).get("memory-bench.runtime") != str(runtime_dir.resolve()):
        raise ValueError("Gateway container must have memory-bench.runtime label matching the dedicated runtime directory")
    if not any(Path(m.get("Source", "")).resolve() == runtime_dir.resolve() for m in info.get("Mounts", [])):
        raise ValueError("Gateway container must mount the dedicated runtime directory")
    return container


def wait_gateway(config, container):
    helper = Path(__file__).resolve().parents[3] / "memory_plugins/scripts/deployment_openclaw.py"
    expected = runpy.run_path(str(helper))["validate_gateway_url"](config, container)
    if os.environ.get("OPENCLAW_GATEWAY_TOKEN") != config["gateway"]["auth"]["token"]:
        raise ValueError("OPENCLAW_GATEWAY_TOKEN must match the dedicated Gateway config")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(expected + "/readyz", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise TimeoutError("Dedicated OpenClaw Gateway did not become ready")


def operation_logs(container, start):
    try:
        logs = subprocess.run(["docker", "logs", "--since", start, container],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return logs.stdout + "\n" + logs.stderr if logs.returncode == 0 else ""


def engine_quarantined(logs, plugin_id, operation):
    marker = f"owner=plugin:{plugin_id} failed during {operation}:"
    return any(marker in line and "quarantining it for this process" in line
               for line in logs.splitlines())


def restart_dedicated_gateway(runtime_dir, container_env, config, *, expected_plugin=None):
    """Caller owns the sample and serializes restarts against its Agent requests."""
    container = dedicated_container(runtime_dir, container_env)
    started = str(time.time())
    subprocess.run(["docker", "restart", container], check=True, capture_output=True, timeout=90)
    wait_gateway(config, container)
    if expected_plugin is None:
        return
    deadline = time.monotonic() + 30
    diagnostic = {"container": container, "since": started, "plugin": expected_plugin,
                  "confirmed": False, "attempts": 0}
    try:
        while time.monotonic() < deadline:
            diagnostic["attempts"] += 1
            remaining = max(0.1, deadline - time.monotonic())
            try:
                logs = subprocess.run(["docker", "logs", "--since", started, container],
                                      capture_output=True, text=True, timeout=min(5, remaining))
                diagnostic["log_returncode"] = logs.returncode
                diagnostic["log_error"] = logs.stderr if logs.returncode else None
                startup = logs.stdout + "\n" + logs.stderr if logs.returncode == 0 else ""
                plugins = re.findall(r"http server listening \(\d+ plugins?: ([^;)\n]+)[;)]", startup)
                diagnostic["observed_plugins"] = plugins
                if any(expected_plugin in (name.strip() for name in names.split(",")) for names in plugins):
                    diagnostic["confirmed"] = True
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                state = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", container],
                                       capture_output=True, text=True, timeout=min(5, remaining))
                diagnostic["container_state"] = state.stdout.strip()
                if state.returncode == 0 and state.stdout.strip() == "false":
                    raise RuntimeError("Restarted Gateway exited before plugin confirmation")
            except (OSError, subprocess.SubprocessError) as exc:
                diagnostic["log_error"] = f"{type(exc).__name__}: {exc}"
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise RuntimeError(f"Restarted Gateway did not confirm the native plugin {expected_plugin}; "
                           f"see {runtime_dir / 'gateway-readiness.json'}")
    finally:
        (runtime_dir / "gateway-readiness.json").write_text(json.dumps(diagnostic, indent=2), encoding="utf-8")
