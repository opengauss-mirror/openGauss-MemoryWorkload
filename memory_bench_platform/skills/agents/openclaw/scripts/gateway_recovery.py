"""Explicit lifecycle operations for a sample-owned OpenClaw Docker Gateway."""
import json
import os
from pathlib import Path
import re
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


def wait_gateway(config):
    expected = f"http://127.0.0.1:{config['gateway']['port']}"
    if os.environ.get("OPENCLAW_GATEWAY_URL", "").rstrip("/") != expected:
        raise ValueError("OPENCLAW_GATEWAY_URL must match the dedicated loopback Gateway port")
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
    wait_gateway(config)
    if expected_plugin is None:
        return
    startup = operation_logs(container, started)
    for plugin_list in re.findall(r"http server listening \(\d+ plugins?: ([^;)\n]+)[;)]", startup):
        if expected_plugin in (name.strip() for name in plugin_list.split(",")):
            return
    raise RuntimeError(f"Restarted Gateway did not confirm the native plugin {expected_plugin}")
