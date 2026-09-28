"""Dedicated OpenClaw runtime for the unmodified official @mem0/openclaw-mem0 1.2.0 plugin."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import subprocess
import urllib.request
import shutil
import sys
import time

PLUGIN = "openclaw-mem0"


def save(path, value):
    temp = path.with_suffix(".tmp")
    with open(temp, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as out:
        os.chmod(temp, 0o600)
        json.dump(value, out, ensure_ascii=False, indent=2)
    temp.replace(path)


def prepare_runtime(source, destination, upstream, runtime_dir=None, gateway_port=18789,
                    top_k=10, timeout=300, plugin_runtime_path=None):
    """Load an installed official npm package; never copy or modify its implementation."""
    if not 1 <= gateway_port <= 65535 or not 1 <= top_k <= 1000 or not 1 <= timeout <= 600:
        raise ValueError("Invalid port, top_k or timeout")
    upstream = upstream.resolve()
    package = json.loads((upstream / "package.json").read_text())
    if package.get("name") != "@mem0/openclaw-mem0" or package.get("version") != "1.2.0":
        raise ValueError("Install official @mem0/openclaw-mem0@1.2.0 (receipt format is version-specific)")
    config = json.loads(source.read_text())
    entry = dict(config.get("plugins", {}).get("entries", {}).get(PLUGIN, {}).get("config", {}))
    if entry.get("mode") != "open-source" or not entry.get("oss"):
        raise ValueError("Source config must supply official plugin mode=open-source and oss settings")
    if entry.get("skills"):
        raise ValueError("Use native automatic hooks; skills mode is not supported by this adapter")
    entry.pop("skills", None)
    entry.update(autoCapture=False, autoRecall=False, userId="mbp-inactive", topK=top_k)
    runtime = Path(runtime_dir) if runtime_dir else destination.resolve()
    plugin_path = plugin_runtime_path or str(upstream)
    if not runtime.is_absolute() or not Path(plugin_path).is_absolute():
        raise ValueError("Runtime and plugin paths must be absolute")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    (destination / "receipts").mkdir(mode=0o700)
    defaults = config.get("agents", {}).get("defaults", {})
    output = {"models": config.get("models", {}), "agents": {"defaults": {
        "model": defaults.get("model", {}), "workspace": str(runtime / "workspace"),
        "skipBootstrap": True, "memorySearch": {"enabled": False},
        "heartbeat": {"every": "0m"}, "timeoutSeconds": 900}}, "tools": {"deny": ["*"]},
        "plugins": {"enabled": True, "allow": [PLUGIN], "load": {"paths": [plugin_path]},
            "slots": {"memory": PLUGIN}, "entries": {PLUGIN: {"enabled": True,
                "hooks": {"allowConversationAccess": True, "allowPromptInjection": True}, "config": entry}}},
        "gateway": {"mode": "local", "bind": "loopback", "port": gateway_port,
            "auth": {"mode": "token", "token": secrets.token_hex(24)},
            "reload": {"mode": "off"}, "http": {"endpoints": {"responses": {"enabled": True}}}}}
    save(destination / "openclaw.json", output)
    save(destination / "phase.json", {"active": False})
    files = {name: hashlib.sha256((upstream / name).read_bytes()).hexdigest()
             for name in ("package.json", "openclaw.plugin.json", "dist/index.js")}
    dependency_root = upstream.parents[1]
    for dependency_file in (dependency_root / "mem0ai/package.json", dependency_root / "mem0ai/dist/oss/index.js",
                            dependency_root.parent / "package-lock.json"):
        if dependency_file.exists():
            files[os.path.relpath(dependency_file, upstream)] = hashlib.sha256(dependency_file.read_bytes()).hexdigest()
    save(destination / "provenance.json", {"implementation": "official_mem0_openclaw",
        "package": package["name"], "version": package["version"], "upstream": str(upstream),
        "plugin_runtime_path": plugin_path, "files": files, "timeout_seconds": timeout,
        "base_config_sha256": config_digest(output)})
    return destination / "openclaw.json"


def config_digest(config):
    value = json.loads(json.dumps(config))
    entry = value["plugins"]["entries"][PLUGIN]["config"]
    for key in ("autoCapture", "autoRecall", "userId"):
        entry.pop(key, None)
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def gateway_container(path):
    name = os.environ.get("MEM0_GATEWAY_CONTAINER", "")
    if not name:
        raise ValueError("MEM0_GATEWAY_CONTAINER must identify the dedicated Docker Gateway")
    result = subprocess.run(["docker", "inspect", name], check=True, capture_output=True, text=True, timeout=15)
    info = json.loads(result.stdout)[0]
    if (info.get("Config", {}).get("Labels") or {}).get("memory-bench.runtime") != str(path.parent):
        raise ValueError("Gateway must have memory-bench.runtime label matching the runtime directory")
    mounts = info.get("Mounts", [])
    runtime_mount = next((m for m in mounts if Path(m.get("Source", "")).resolve() == path.parent), None)
    if not runtime_mount:
        raise ValueError("Gateway must mount the dedicated runtime")
    env = dict(item.split("=", 1) for item in info.get("Config", {}).get("Env", []) if "=" in item)
    destination = runtime_mount["Destination"].rstrip("/")
    if env.get("OPENCLAW_CONFIG_PATH") != destination + "/openclaw.json" or env.get("OPENCLAW_STATE_DIR") != destination + "/state":
        raise ValueError("Gateway config/state paths must use the dedicated mount")
    provenance = json.loads(path.with_name("provenance.json").read_text())
    upstream, loaded = Path(provenance["upstream"]), Path(provenance["plugin_runtime_path"])
    for mount in mounts:
        source = Path(mount.get("Source", "")).resolve()
        if not mount.get("RW", True) and upstream.is_relative_to(source):
            if Path(mount["Destination"]) / upstream.relative_to(source) == loaded:
                break
    else:
        raise ValueError("Gateway must mount the verified official package read-only at its configured path")
    return name


def logs(container, since, *, timeout=20):
    result = subprocess.run(["docker", "logs", "--timestamps", "--since", str(since), container],
                            check=True, capture_output=True, text=True, timeout=timeout)
    return "\n".join(sorted((result.stdout + "\n" + result.stderr).splitlines()))


def configure(path, config, state, phase):
    container = gateway_container(path)
    cfg = config["plugins"]["entries"][PLUGIN]["config"]
    cfg.update(autoCapture=phase == "ingest", autoRecall=phase == "qa",
               userId=state.get("user_id", "mbp-inactive"))
    state.update(phase=phase, gateway_ready=False)
    save(path, state)
    save(path.with_name("openclaw.json"), config)
    since = time.time()
    subprocess.run(["docker", "restart", container], check=True, capture_output=True, timeout=90)
    deadline = time.monotonic() + 90
    marker = (f"openclaw-mem0: registered (mode: open-source, user: {cfg['userId']}, "
              f"autoRecall: {str(cfg['autoRecall']).lower()}, autoCapture: {str(cfg['autoCapture']).lower()}, skills: false)")
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(os.environ["OPENCLAW_GATEWAY_URL"] + "/readyz", timeout=2) as response:
                if response.status == 200 and marker in logs(container, since):
                    state.update(gateway_ready=True, config_sha256=hashlib.sha256(path.with_name("openclaw.json").read_bytes()).hexdigest())
                    return
        except OSError:
            pass
        time.sleep(1)
    raise TimeoutError("Gateway did not load the official plugin with the requested phase/identity")


def native_receipt(text, phase):
    # Official 1.2.0 logs do not carry request IDs. This check is valid only with
    # an exclusive Gateway, serial calls, fresh sessions and a per-call log window.
    lines = [line for line in text.splitlines() if "openclaw-mem0:" in line]
    if any("capture failed:" in line or "recall failed:" in line for line in lines):
        raise RuntimeError("Official Mem0 hook failed; inspect Gateway logs")
    pattern = r"auto-captured ([1-9][0-9]*) memories" if phase == "ingest" else r"injecting ([1-9][0-9]*) memories into context"
    matches = [int(m[1]) for line in lines if (m := re.search(pattern, line))]
    if phase == "qa" and any("auto-captured" in line for line in lines):
        raise RuntimeError("Memory capture occurred during QA")
    if len(matches) > 1:
        raise RuntimeError("Multiple native receipts in one request window; exclusive Gateway required")
    if phase == "qa":
        # Upstream 1.2.0 leaves its timeout timer armed even after success.
        # A timeout before injection is a failure; one after injection is that
        # known late warning. Drain timers before opening the next log window.
        for line in lines:
            if re.search(pattern, line):
                break
            if "recall timed out" in line:
                raise RuntimeError("Official recall exceeded its deadline")
    return {"native_marker": "auto-captured" if phase == "ingest" else "injecting",
            "memory_count": matches[0],
            "native_log": next(line for line in lines if re.search(pattern, line))} if matches else None


def load_runtime():
    configured = os.environ.get("MEM0_PLUGIN_STATE_FILE")
    if not configured:
        raise ValueError("MEM0_PLUGIN_STATE_FILE must point to the dedicated phase.json")
    path = Path(configured).resolve()
    if path.name != "phase.json":
        raise ValueError("Expected phase.json from the runtime preparation command")
    provenance = json.loads(path.with_name("provenance.json").read_text())
    for name, expected in provenance["files"].items():
        if hashlib.sha256((Path(provenance["upstream"]) / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Prepared runtime changed; create a fresh dedicated runtime")
    config = json.loads(path.with_name("openclaw.json").read_text())
    if config_digest(config) != provenance["base_config_sha256"]:
        raise ValueError("Runtime base configuration changed; prepare a new runtime")
    if os.environ.get("OPENCLAW_TRANSPORT") != "http":
        raise ValueError("Mem0 plugin requires OPENCLAW_TRANSPORT=http")
    gateway = config["gateway"]
    if os.environ.get("OPENCLAW_GATEWAY_URL", "").rstrip("/") != f"http://127.0.0.1:{gateway['port']}":
        raise ValueError("Gateway URL does not match dedicated runtime")
    if os.environ.get("OPENCLAW_GATEWAY_TOKEN") != gateway["auth"]["token"]:
        raise ValueError("Gateway token does not match dedicated runtime")
    state_dir = os.environ.get("OPENCLAW_STATE_DIR", "")
    if not state_dir or Path(state_dir).resolve() != path.parent / "state":
        raise ValueError("OPENCLAW_STATE_DIR must use this runtime's state directory")
    gateway_container(path)
    return path, config


def run(request):
    path, config = load_runtime()
    # Protect ownership and pending-request updates across lifecycle processes.
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return dispatch(request, path, config)


def dispatch(request, path, config):
    inputs = request.get("inputs", {})
    action = request.get("action") or inputs.get("action")
    owner = str(request.get("runtime_context", {}).get("run_dir") or "")
    if not owner:
        raise ValueError("runtime_context.run_dir is required")
    state = json.loads(path.read_text())
    if state.get("active") and state.get("owner") != owner:
        raise ValueError("Dedicated runtime belongs to another run")
    output, artifacts = {}, []
    if action == "validate":
        output = {"implementation": "official_mem0_openclaw", "package": "@mem0/openclaw-mem0", "version": "1.2.0"}
    elif action == "prepare":
        if state.get("pending"):
            raise ValueError("Cannot prepare an episode during an unfinished request")
        scope = str(inputs.get("scope_id") or "").strip()
        if not scope:
            raise ValueError("scope_id is required")
        seen = state.get("seen_scopes", []) if state.get("owner") == owner else []
        if scope in seen:
            raise ValueError("Episode already prepared; use a fresh run")
        state = {"active": True, "owner": owner, "scope_id": scope, "phase": "ingest",
                 "seen_scopes": [*seen, scope]}
        state["user_id"] = "mbp-" + hashlib.sha256((owner + ":" + scope).encode()).hexdigest()[:24]
        configure(path, config, state, "ingest")
        output = {"scope_id": scope, "user_id": state["user_id"], "version": "1.2.0"}
    elif action == "finalize":
        if state.get("owner") != owner:
            raise ValueError("Cannot finalize another run's runtime")
        configure(path, config, state, "inactive")
        archive = Path(owner) / "artifacts/memory_plugin/openclaw-mem0"
        archive.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(path.with_name("provenance.json"), archive / "provenance.json")
        (archive / "provenance.json").chmod(0o600)
        for receipt in (path.parent / "receipts").glob("*.json"):
            if json.loads(receipt.read_text()).get("scope_id") in state.get("seen_scopes", []):
                shutil.copyfile(receipt, archive / receipt.name)
                (archive / receipt.name).chmod(0o600)
        state = {**state, "active": False, "phase": "inactive", "pending": None}
        output = {"inactive": True}
        artifacts = [{"kind": "memory_plugin_receipts", "path": str(archive)}]
    else:
        if not state.get("active"):
            raise ValueError("prepare must run first")
        if action == "set_phase":
            if state.get("pending"):
                raise ValueError("Cannot change phase during an unfinished request")
            if inputs.get("phase") not in {"ingest", "qa"}:
                raise ValueError("phase must be ingest or qa")
            if inputs["phase"] != state["phase"]:
                configure(path, config, state, inputs["phase"])
            output = {"phase": state["phase"], "capture": state["phase"] == "ingest",
                      "recall": state["phase"] == "qa"}
        elif action == "before_agent":
            if not state.get("gateway_ready") or state.get("config_sha256") != hashlib.sha256(path.with_name("openclaw.json").read_bytes()).hexdigest():
                raise ValueError("Gateway has not loaded the active configuration")
            # Let 1.2.0's uncancelled recall timer expire outside the next window.
            time.sleep(max(0, state.get("next_request_after", 0) - time.time()))
            req = inputs["agent_request"]
            meta = req.get("metadata", {})
            phase = "ingest" if meta.get("purpose") == "memory_ingest" else "qa"
            if meta.get("scope_id") != state["scope_id"] or phase != state["phase"]:
                raise ValueError("Agent request scope/phase mismatch")
            if state.get("pending"):
                raise ValueError("Parallel/replayed requests are not supported")
            messages = req.get("messages", [])
            if len(messages) != 1 or messages[0].get("role") != "user" or req.get("attachments"):
                raise ValueError("Mem0 benchmark requires one historical session or question as user text")
            content = messages[0].get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Empty user content")
            agent = str(meta.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID") or "main").lower()
            if agent != "main":
                raise ValueError("This dedicated runtime supports the main Agent only")
            key = str(meta.get("session_key") or "").lower()
            if not key and meta.get("session_id"):
                key = f"agent:{agent}:explicit:{meta['session_id']}".lower()
            if not key:
                raise ValueError("Explicit session key or ID is required")
            if not key.startswith("agent:"):
                key = f"agent:{agent}:{key}"
            if not key.startswith("agent:main:"):
                raise ValueError("Session key must belong to main Agent")
            # Fresh OpenClaw sessions prevent previous QA messages influencing this question.
            store = path.parent / f"state/agents/{agent}/sessions/sessions.json"
            if store.exists() and key in json.loads(store.read_text()):
                raise ValueError("OpenClaw session already exists; use a fresh run/session")
            state["pending"] = {"nonce": secrets.token_hex(16), "session_key": key,
                "scope_id": state["scope_id"], "phase": phase, "log_since": str(time.time())}
            output = dict(state["pending"])
        elif action == "after_agent":
            context = inputs["check_context"]
            pending = state.get("pending") or {}
            if not pending or any(context.get(k) != v for k, v in pending.items()):
                raise ValueError("Request receipt context mismatch")
            result = inputs["agent_result"]
            handle = result.get("output", {}).get("session_handle", {})
            if result.get("status") != "ok" or result.get("transport") != "http":
                raise ValueError("Agent call failed or transport changed")
            if not handle.get("session_id") or handle.get("gateway_session_key") != pending["session_key"]:
                raise ValueError("Actual Gateway session does not match request")
            operation = "ingest" if pending["phase"] == "ingest" else "recall"
            receipt = path.parent / "receipts" / f"{pending['nonce']}-{operation}.json"
            timeout = json.loads(path.with_name("provenance.json").read_text())["timeout_seconds"]
            container = gateway_container(path)
            # Allow brief Docker log visibility lag without extending native recall.
            deadline = time.monotonic() + (min(timeout, 3) if pending["phase"] == "qa" else timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("No positive native Mem0 completion marker; empty results/skipped hooks cannot be verified by official 1.2.0")
                text = logs(container, pending["log_since"], timeout=min(20, remaining))
                proof = native_receipt(text, pending["phase"])
                if proof and time.monotonic() < deadline:
                    break
                interval = 0.2 if pending["phase"] == "qa" else 1
                time.sleep(min(interval, max(0, deadline - time.monotonic())))
            data = {**pending, **proof, "session_id": handle["session_id"], "user_id": state["user_id"],
                    "verification": "exclusive_serial_gateway_log_window", "package_version": "1.2.0"}
            save(receipt, data)
            state["pending"] = None
            state["next_request_after"] = time.time() + (9 if pending["phase"] == "qa" else 0)
            output = {"checked": True, "phase": pending["phase"], "receipt": str(receipt), **proof}
            artifacts = [{"kind": "memory_plugin_receipt", "path": str(receipt),
                          "content_type": "application/json"}]
        else:
            raise ValueError(f"Unsupported action: {action}")
    save(path, state)
    return {"protocol_version": "memory-plugin/1", "status": "ok", "state": "completed",
            "operation": {}, "output": output, "artifacts": artifacts, "metrics": [], "error": {}}


def main():
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--source-config", type=Path, required=True)
        parser.add_argument("--destination", type=Path, required=True)
        parser.add_argument("--upstream-plugin", type=Path, required=True)
        parser.add_argument("--plugin-runtime-path")
        parser.add_argument("--openclaw-runtime-dir")
        parser.add_argument("--gateway-port", type=int, default=18789)
        parser.add_argument("--top-k", type=int, default=10)
        parser.add_argument("--timeout", type=int, default=300)
        args = parser.parse_args()
        print(prepare_runtime(args.source_config, args.destination, args.upstream_plugin,
            args.openclaw_runtime_dir, args.gateway_port, args.top_k, args.timeout, args.plugin_runtime_path))
        return
    try:
        result = run(json.load(sys.stdin))
    except Exception as exc:
        result = {"protocol_version": "memory-plugin/1", "status": "failed", "state": "failed",
            "output": {}, "metrics": [], "artifacts": [],
            "error": {"code": type(exc).__name__, "message": str(exc)}}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
