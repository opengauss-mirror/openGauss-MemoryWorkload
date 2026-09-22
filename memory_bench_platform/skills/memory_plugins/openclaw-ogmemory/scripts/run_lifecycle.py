"""memory-plugin/1 lifecycle using native oGMemory capture/recall switches."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PLUGIN = "og-memory-context-engine"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def save(path, state):
    temp = path.with_suffix(".tmp")
    with open(temp, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as out:
        os.chmod(temp, 0o600)
        json.dump(state, out, indent=2)
    temp.replace(path)


def prepare_runtime(source_config, upstream, destination, api_url, runtime_path=None, gateway_port=None):
    """Copy an unmodified native plugin into an isolated, initially inactive runtime."""
    config = json.loads(source_config.read_text())
    schema = json.loads((upstream / "openclaw.plugin.json").read_text())
    props = schema.get("configSchema", {}).get("properties", {})
    if schema.get("id") != PLUGIN or any(props.get(k, {}).get("type") != "boolean"
                                        for k in ("autoCapture", "autoRecall")):
        raise ValueError("Native oGMemory plugin with autoCapture/autoRecall is required")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    runtime = Path(runtime_path) if runtime_path else destination.resolve()
    shutil.copytree(upstream, destination / "plugin",
                    ignore=shutil.ignore_patterns("node_modules", ".git", "__pycache__"))
    entry = {"mode": "remote", "memoryApiBaseUrl": api_url,
             "autoCapture": False, "autoRecall": False,
             "prefetchEnabled": False, "toolResultExternalizationEnabled": False,
             "toolResultFirstViewExternalizationEnabled": False, "traceCaptureContent": False}
    if os.environ.get("OGMEM_API_KEY"):
        entry["authApiKey"] = os.environ["OGMEM_API_KEY"]
    defaults = config.get("agents", {}).get("defaults", {})
    output = {"models": config.get("models", {}),
        "agents": {"defaults": {"model": defaults.get("model", {}),
            "workspace": str(runtime / "workspace"), "skipBootstrap": True,
            "memorySearch": {"enabled": False}, "heartbeat": {"every": "0m"}, "timeoutSeconds": 900}},
        "tools": {"deny": ["*"]},
        "plugins": {"enabled": True, "allow": [PLUGIN],
            "load": {"paths": [str(runtime / "plugin")]},
            "slots": {"memory": "none", "contextEngine": PLUGIN},
            "entries": {PLUGIN: {"enabled": True, "config": entry}}},
        "gateway": {"mode": "local"}}
    if gateway_port is not None:
        if not 1 <= gateway_port <= 65535:
            raise ValueError("gateway_port must be between 1 and 65535")
        output["gateway"].update(port=gateway_port, bind="loopback",
            auth={"mode": "token", "token": secrets.token_hex(24)},
            reload={"mode": "off"},
            http={"endpoints": {"responses": {"enabled": True}}})
    target = destination / "openclaw.json"
    save(target, output)
    save(destination / "phase.json", {"active": False})
    save(destination / "provenance.json", {
        "upstream_plugin_sha256": hashlib.sha256((upstream / "index.js").read_bytes()).hexdigest(),
        "upstream_schema_sha256": hashlib.sha256((upstream / "openclaw.plugin.json").read_bytes()).hexdigest(),
        "phase_control": "native_autoCapture_autoRecall",
        "extraction_trigger": "agent_native_after_turn"})
    return target


def backend(base, path, body, identity, *, method="POST", timeout=60):
    headers = {"Content-Type": "application/json", "X-Account-ID": identity["accountId"],
               "X-User-ID": identity["userId"], "X-Agent-ID": identity["agentId"]}
    if os.environ.get("OGMEM_API_KEY"):
        headers["X-API-Key"] = os.environ["OGMEM_API_KEY"]
    url = base.rstrip("/") + path
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(identity)
    else:
        data = json.dumps({**body, **identity}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers,
                                                      method=method), timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("ok") is False or result.get("error"):
        raise RuntimeError("oGMemory rejected readiness request")
    return result


def configure(path, config, state, phase):
    config["agents"]["defaults"]["heartbeat"] = {"every": "0m"}
    entry = config["plugins"]["entries"][PLUGIN]["config"]
    entry.update(state.get("identity", {}))
    if state.get("identity"):
        entry["authAccountId"] = state["identity"]["accountId"]
    entry.update(autoCapture=phase == "ingest", autoRecall=phase == "qa")
    save(path.with_name("openclaw.json"), config)
    # Mark the new state usable only after the dedicated Gateway has loaded it.
    save(path, {**state, "phase": phase, "gateway_ready": False})
    if os.environ.get("OPENCLAW_TRANSPORT") == "http":
        container = gateway_container(path)
        subprocess.run(["docker", "restart", container], check=True, capture_output=True, timeout=90)
        wait_gateway(config)
        save(path, {**state, "phase": phase, "gateway_ready": True,
                    "config_sha256": hashlib.sha256(path.with_name("openclaw.json").read_bytes()).hexdigest()})


def gateway_container(path):
    container = os.environ.get("OGMEM_GATEWAY_CONTAINER", "")
    if not container:
        raise ValueError("HTTP phase switching requires OGMEM_GATEWAY_CONTAINER for a dedicated Docker Gateway")
    result = subprocess.run(["docker", "inspect", container], check=True, capture_output=True, text=True, timeout=15)
    info = json.loads(result.stdout)[0]
    if (info.get("Config", {}).get("Labels") or {}).get("memory-bench.runtime") != str(path.parent):
        raise ValueError("Gateway container must have memory-bench.runtime label matching the dedicated runtime directory")
    if not any(Path(m.get("Source", "")).resolve() == path.parent for m in info.get("Mounts", [])):
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


def wait_ready(base, state, inputs):
    if inputs.get("scope_id") != state["scope_id"]:
        raise ValueError("Readiness scope does not match active episode")
    sid = (inputs.get("session_handle") or {}).get("session_id")
    if not sid:
        raise ValueError("Native agent session_handle.session_id is required")
    identity = state["identity"]
    body = {"sessionId": sid, "timeoutSeconds": 1, "drainOutbox": False, "waitOutbox": False}
    timeout = float(inputs.get("timeout_seconds", 600))
    interval = float(inputs.get("interval_seconds", 2))
    if not math.isfinite(timeout) or not math.isfinite(interval) or interval < 0:
        raise ValueError("Readiness timeout and interval must be finite; interval must be non-negative")
    deadline = time.monotonic() + timeout

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("Native capture/indexing did not settle before deadline")
        return budget

    def request(path, payload, **kwargs):
        budget = remaining()
        result = backend(base, path, payload, identity, timeout=min(60, budget), **kwargs)
        remaining()
        return result

    while True:
        idle = request("/api/v1/call/wait_until_idle", body)
        # Do not call GET session first: that endpoint can create an empty session.
        if idle.get("reason") == "session_not_found":
            raise RuntimeError("Native capture session missing; inspect OpenClaw/backend logs")
        if idle.get("idle") is True:
            session = request("/api/v1/sessions/" + urllib.parse.quote(sid, safe=""),
                              {}, method="GET")
            if int(session.get("message_count", 0)) <= 0 and int(session.get("commit_count", 0)) <= 0:
                raise RuntimeError("Native capture has no accepted history")
            if int(session.get("commit_count", 0)) == 0:
                # An idle buffer does not prove extraction; preserve native threshold behavior.
                remaining()
                return {"session_id": sid, "history_archived": False,
                        "reason": "buffered_without_archive", "session": session,
                        "backend_result": idle}
            drained = request("/api/v1/call/wait_until_idle",
                              {**body, "drainOutbox": True})
            if drained.get("reason") == "session_not_found" or int((drained.get("drain") or {}).get("failed", 0)):
                raise RuntimeError("Captured session missing or indexing failed")
            settled = request("/api/v1/call/wait_until_idle",
                              {**body, "waitOutbox": True})
            outbox = settled.get("outbox") or {}
            if settled.get("reason") == "session_not_found" or outbox.get("supported") is False or outbox.get("error"):
                raise RuntimeError("Backend cannot verify readiness")
            if settled.get("idle") is True and "total" in outbox and int(outbox["total"]) == 0:
                remaining()
                return {"session_id": sid, "history_archived": True, "session": session,
                        "backend_result": settled, "extraction_trigger": "agent_native_after_turn"}
        time.sleep(min(interval, remaining()))


def check_ogmemory_hook_errors(text, phase=None):
    failures = [line for line in text.splitlines()
                if "[og-memory]" in line and any(marker in line.lower() for marker in ("http ", "failed", "compose returned null"))
                and not (phase == "ingest" and "HTTP 503 from after_turn" in line and '"status":"processing"' in line)]
    if failures:
        details = [m.group(0) for line in failures
                   if (m := re.search(r"HTTP (?:[0-9]+ from )?[a-z_]+|compose returned null|failed", line, re.I))]
        raise RuntimeError("Native oGMemory hook failed: " + ", ".join(details)
                           + "; inspect OpenClaw/backend logs")


def check_qa_recall(text, session_id, expected_key):
    # The pinned native plugin emits these two lines consecutively, after the
    # null-result branch. Never combine unrelated markers from different calls.
    previous = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        identity = re.search(r"\[og-memory\] assemble params keys: .*?, sessionKey=([^,]+), sessionId=(\S+)\s*$", line)
        if (identity and re.search(r"\[og-memory\] assemble return: \d+ msgs, ws_injected=(?:true|false),", previous)
                and identity[2] == session_id
                and (not expected_key or identity[1] == expected_key)):
            return {"session_id": session_id, "session_key": identity[1],
                    "recall_receipt": "native_assemble_return"}
        previous = line
    raise RuntimeError("No successful native assemble receipt for this QA session; inspect Gateway logs")


def before_agent(path, config, state, request):
    metadata = request.get("metadata", {})
    phase = "ingest" if metadata.get("purpose") == "memory_ingest" else "qa"
    transport = os.environ.get("OPENCLAW_TRANSPORT", "cli")
    if transport == "http":
        if not state.get("gateway_ready") or state.get("config_sha256") != hashlib.sha256(path.with_name("openclaw.json").read_bytes()).hexdigest():
            raise ValueError("oGMemory Gateway has not loaded the active config; run lifecycle prepare/set_phase")
    elif not metadata.get("local"):
        raise ValueError("oGMemory CLI phase switching requires --local")
    elif request.get("system_prompt"):
        raise ValueError("oGMemory benchmark instructions require HTTP transport to keep retrieval input separate")
    if not state.get("active") or state.get("phase") != phase:
        raise ValueError("oGMemory runtime is inactive or in the wrong phase")
    if metadata.get("scope_id") != state.get("scope_id"):
        raise ValueError("oGMemory runtime scope does not match this task")
    entry = config["plugins"]["entries"][PLUGIN]["config"]
    if (entry.get("autoCapture") is not (phase == "ingest")
            or entry.get("autoRecall") is not (phase == "qa")
            or any(entry.get(k) != v for k, v in state["identity"].items())
            or entry.get("authAccountId") != state["identity"]["accountId"]):
        raise ValueError("oGMemory native configuration does not match the active phase/identity")
    expected_key = ""
    if phase == "qa" and transport == "http":
        agent_id = str(metadata.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID") or "main").lower()
        expected_key = str(metadata.get("session_key") or "").lower()
        if not expected_key and metadata.get("session_id"):
            expected_key = f"agent:{agent_id}:explicit:{metadata['session_id']}".lower()
        if not expected_key:
            raise ValueError("QA recall validation requires an explicit session key or ID")
        if not expected_key.startswith("agent:"):
            expected_key = f"agent:{agent_id}:{expected_key}"
    return {"log_since": str(time.time()), "transport": transport,
            "phase": phase, "expected_session_key": expected_key}


def after_agent(path, inputs):
    result = inputs["agent_result"]
    context = inputs["check_context"]
    if result.get("transport") != context["transport"]:
        raise ValueError("Agent transport changed after plugin validation")
    if context["transport"] == "http":
        logs = subprocess.run(["docker", "logs", "--since", context["log_since"],
                               gateway_container(path)], check=True, capture_output=True,
                              text=True, timeout=20)
        text = logs.stdout + "\n" + logs.stderr
        check_ogmemory_hook_errors(text, context["phase"])
        if not result.get("output", {}).get("session_id"):
            raise RuntimeError("Cannot resolve native Gateway session ID from OPENCLAW_STATE_DIR")
    else:
        stderr = result.get("stderr", "")
        if "og-memory: mode=remote" not in stderr:
            raise RuntimeError("Native oGMemory plugin was not confirmed loaded; inspect OpenClaw logs")
        text = stderr
        check_ogmemory_hook_errors(text, context["phase"])
    receipt = {}
    if context["phase"] == "qa":
        sid = str(result.get("output", {}).get("session_id") or "")
        if not sid:
            raise RuntimeError("QA recall validation requires the actual Agent session ID")
        receipt = check_qa_recall(text, sid, context["expected_session_key"])
    return {"checked": True, "phase": context["phase"], **receipt}


def run(request):
    inputs = request.get("inputs", {})
    action = request.get("action") or inputs.get("action")
    configured = os.environ.get("OGMEM_PLUGIN_STATE_FILE")
    if not configured:
        raise ValueError("OGMEM_PLUGIN_STATE_FILE must point to the dedicated phase.json")
    path = Path(configured).resolve()
    config = json.loads(path.with_name("openclaw.json").read_text())
    plugins = config.get("plugins", {})
    entry = plugins.get("entries", {}).get(PLUGIN, {})
    if plugins.get("slots", {}).get("contextEngine") != PLUGIN or not entry.get("enabled"):
        raise ValueError("Dedicated native oGMemory context engine is not enabled")
    if os.environ.get("OG_AUTH_ACCOUNT_ID"):
        raise ValueError("OG_AUTH_ACCOUNT_ID must be unset; account identity is episode-specific")
    transport = os.environ.get("OPENCLAW_TRANSPORT", "cli")
    if transport == "http":
        gateway_container(path)
        state_dir = os.environ.get("OPENCLAW_STATE_DIR")
        if not state_dir or Path(state_dir).resolve() != path.parent / "state":
            raise ValueError("OPENCLAW_STATE_DIR must point to this dedicated runtime's state directory")
    elif transport != "cli" or os.environ.get("MEMORY_BENCH_AGENT_LOCAL") != "1":
        raise ValueError("Use dedicated HTTP Gateway or OPENCLAW_TRANSPORT=cli with MEMORY_BENCH_AGENT_LOCAL=1")
    state = json.loads(path.read_text())
    owner = str(request.get("runtime_context", {}).get("run_dir") or "")
    if not owner:
        raise ValueError("runtime_context.run_dir is required")
    if state.get("active") and state.get("owner") != owner:
        raise ValueError("Runtime is owned by another active run; use a dedicated runtime")
    events = Path(str(path) + "." + digest(owner) + ".events.jsonl")
    output, artifacts = {}, []
    if action == "validate":
        props = json.loads((path.parent / "plugin/openclaw.plugin.json").read_text())["configSchema"]["properties"]
        if any(props.get(k, {}).get("type") != "boolean" for k in ("autoCapture", "autoRecall")):
            raise ValueError("Native oGMemory capture/recall switches are required")
        output = {"context_engine": PLUGIN, "phase_control": "native_autoCapture_autoRecall"}
    elif action == "before_agent":
        output = before_agent(path, config, state, inputs["agent_request"])
    elif action == "after_agent":
        output = after_agent(path, inputs)
    elif action == "prepare":
        scope = str(inputs.get("scope_id") or "")
        if not scope:
            raise ValueError("scope_id is required")
        suffix = digest(owner + ":" + scope)
        state = {"active": True, "owner": owner, "phase": "ingest", "scope_id": scope,
            "identity": {"accountId": os.environ.get("OGMEM_ACCOUNT_ID", "memory-bench") + "-" + suffix,
                         "userId": "mbp-user-" + suffix, "agentId": "mbp-agent-" + suffix}}
        configure(path, config, state, "ingest")
        provenance = path.with_name("provenance.json")
        output = {"scope_id": scope, "identity": state["identity"],
                  "extraction_trigger": "agent_native_after_turn",
                  "runtime_version": os.environ.get("OGMEM_RUNTIME_VERSION", "unrecorded"),
                  "provenance": json.loads(provenance.read_text()) if provenance.exists() else {}}
    elif action == "finalize":
        configure(path, config, {**state, "active": False}, "inactive")
        output = {"inactive": True}
        if events.exists():
            archive = Path(owner) / "artifacts/memory_plugin/openclaw-ogmemory-events.jsonl"
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(events, archive)
            archive.chmod(0o600)
            artifacts.append({"kind": "memory_plugin_events", "path": str(archive),
                              "content_type": "application/x-ndjson"})
    else:
        if not state.get("active"):
            raise ValueError("prepare must run before phase or readiness actions")
        if action == "set_phase":
            phase = inputs.get("phase")
            if phase not in ("ingest", "qa"):
                raise ValueError("phase must be ingest or qa")
            configure(path, config, state, phase)
            output = {"phase": phase, "autoCapture": phase == "ingest", "autoRecall": phase == "qa"}
        elif action == "wait_ready":
            output = wait_ready(entry["config"]["memoryApiBaseUrl"], state, inputs)
        else:
            raise ValueError(f"Unsupported action: {action}")
    if action != "finalize":
        with open(events, "a", opener=lambda n, f: os.open(n, f, 0o600)) as log:
            log.write(json.dumps({"action": action, "owner": owner,
                                  "scope_id": inputs.get("scope_id", state.get("scope_id") if state.get("owner") == owner else None),
                                  "output": output, "at": time.time()}) + "\n")
    return {"protocol_version": "memory-plugin/1", "status": "ok", "state": "completed",
            "operation": {}, "output": output, "metrics": [], "artifacts": artifacts, "error": {}}


def main():
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="Prepare a dedicated native oGMemory runtime")
        parser.add_argument("--source-config", type=Path, required=True)
        parser.add_argument("--upstream-plugin", type=Path, required=True)
        parser.add_argument("--destination", type=Path, required=True)
        parser.add_argument("--api-url", required=True)
        parser.add_argument("--openclaw-runtime-dir")
        parser.add_argument("--gateway-port", type=int)
        args = parser.parse_args()
        print(prepare_runtime(args.source_config, args.upstream_plugin, args.destination,
                              args.api_url, args.openclaw_runtime_dir, args.gateway_port))
        return
    try:
        result = run(json.load(sys.stdin))
    except Exception as exc:
        message = str(exc)
        for key, value in os.environ.items():
            if value and any(k in key.upper() for k in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                message = message.replace(value, "[REDACTED]")
        result = {"protocol_version": "memory-plugin/1", "status": "failed", "state": "failed",
                  "operation": {}, "output": {}, "metrics": [], "artifacts": [],
                  "error": {"code": type(exc).__name__, "message": message}}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
