"""Workload lifecycle adapter for the official JiuwenMemory MCP service."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import runpy
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

SEARCH = "jiuwen__memory_search"
ADD = "jiuwen__memory_add"
INGEST_AGENT = "jiuwen-ingest"
QA_AGENT = "main"


def transcript(result, state_dir, agent):
    if result.get("status") != "ok":
        return None, []
    sid = result.get("output", {}).get("session_id")
    if not sid:
        return None, []
    sid = str(UUID(sid))
    file = state_dir / "agents" / agent / "sessions" / f"{sid}.jsonl"
    if not file.is_file():
        return None, []
    return sid, [json.loads(line).get("message", {}) for line in file.read_text().splitlines()]


def tool_results(messages, tool, matches, *, prefer_structured=False):
    calls = [part for message in messages if message.get("role") == "assistant"
             for part in message.get("content", []) if isinstance(part, dict) and part.get("type") == "toolCall"]
    ids = {call["id"] for call in calls if call.get("name") == tool and matches(call.get("arguments"))}
    results = [message for message in messages if message.get("role") == "toolResult"
               and message.get("toolCallId") in ids and message.get("toolName") == tool
               and message.get("isError") is False]
    payloads = []
    for result in results:
        if prefer_structured:
            details = result.get("details")
            structured = details.get("structuredContent") if isinstance(details, dict) else None
            if structured is not None:
                payloads.append(structured)
                continue
        for part in result.get("content", []):
            try:
                payloads.append(json.loads(part.get("text", "")))
            except (ValueError, TypeError):
                continue
    return calls, results, payloads


def receipt(result, scope, state_dir):
    sid, messages = transcript(result, state_dir, QA_AGENT)

    def matches(args):
        context = args.get("context") if isinstance(args, dict) else None
        return isinstance(context, dict) and context.get("scope") == scope

    calls, _, payloads = tool_results(messages, SEARCH, matches, prefer_structured=True)
    verified = False
    evidence = False
    for payload in payloads:
        if not isinstance(payload, dict) or payload.get("errors"):
            continue
        items = payload.get("items")
        if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
            continue
        verified = True
        evidence |= any(isinstance(row.get("content"), str) and bool(row["content"].strip()) for row in items)
    return {"session_id": sid, "calls": sum(call.get("name") == SEARCH for call in calls) if sid else None,
            "verified_search": verified, "evidence": evidence}


def ingest_receipt(result, expected, content, state_dir):
    sid, messages = transcript(result, state_dir, INGEST_AGENT)

    def matches(args):
        return (isinstance(args, dict) and args.get("content") == content
                and {key: value for key, value in args.items() if key != "content"} == expected)

    calls, results, payloads = tool_results(messages, ADD, matches)
    ids = None
    verified = False
    if sid and len(calls) == len(results) == 1:
        details = results[0].get("details") or {}
        structured = details.get("structuredContent")
        if isinstance(structured, dict):
            units = structured.get("result")
        elif payloads and len(payloads) == len(results[0].get("content", [])):
            units = payloads
        else:
            units = None
        if isinstance(units, list) and all(
            isinstance(unit, dict) and isinstance(unit.get("id"), str) and unit["id"] for unit in units
        ):
            ids = [unit["id"] for unit in units]
            verified = True
        elif details.get("persistedDetailsTruncated") is True and {
            "mcpServer", "mcpTool", "structuredContent"
        }.issubset(details.get("originalDetailKeys", [])):
            # OpenClaw retained the success flag and correlation but trimmed the payload.
            verified = True
    return {"session_id": sid, "calls": len(calls), "ingest_verified": verified,
            "memory_ids": ids}



def validate_gateway_binding(container, config_path, state_dir):
    info, = json.loads(subprocess.check_output(
        ["docker", "inspect", "--type", "container", container], timeout=15,
    ))
    state = info["State"]
    config = info["Config"]
    env = dict(item.split("=", 1) for item in config["Env"])
    if (not state["Running"] or info["HostConfig"]["NetworkMode"] != "host"
            or (config.get("Cmd") or [])[:2] != ["gateway", "run"]):
        raise ValueError("Expected the dedicated running host-network OpenClaw Gateway")

    def host_path(variable):
        raw = env.get(variable, "")
        path = PurePosixPath(raw)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Gateway must set an absolute {variable}")
        mounts = [(PurePosixPath(m["Destination"]), m) for m in info["Mounts"]]
        matches = [(target, mount) for target, mount in mounts if path.is_relative_to(target)]
        if matches:
            target, mount = max(matches, key=lambda match: len(match[0].parts))
            if mount["Type"] == "bind":
                return (Path(mount["Source"]) / path.relative_to(target)).resolve()
        raise ValueError(f"Gateway must bind-mount {variable}")

    if host_path("OPENCLAW_CONFIG_PATH") != config_path or host_path("OPENCLAW_STATE_DIR") != state_dir:
        raise ValueError("Gateway config and state mounts must match the Workload paths")
    started = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00")).timestamp()
    if config_path.stat().st_mtime > started:
        raise ValueError("Restart the dedicated Gateway after editing its configuration")


def validate():
    config_path = Path(os.environ["OPENCLAW_CONFIG_PATH"]).resolve(strict=True)
    state = Path(os.environ["OPENCLAW_STATE_DIR"]).resolve(strict=True)
    validate_gateway_binding(os.environ["JIUWEN_MCP_GATEWAY_CONTAINER"], config_path, state)
    config = json.loads(config_path.read_text())
    url = os.environ["JIUWENMEMORY_MCP_URL"]
    server = config.get("mcp", {}).get("servers", {}).get("jiuwen", {})
    port = config.get("gateway", {}).get("port", 18789)
    endpoint = urlsplit(url)
    agents = {item["id"]: item for item in config.get("agents", {}).get("list", [])}
    if (os.environ.get("OPENCLAW_GATEWAY_URL", "").rstrip("/") != f"http://127.0.0.1:{port}"
            or os.environ.get("OPENCLAW_TRANSPORT") != "http"
            or os.environ.get("OPENCLAW_POOL_ENABLED") == "1" or os.environ.get("OPENCLAW_SESSION_JSONL")
            or endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1"
            or endpoint.username is not None or endpoint.query or endpoint.fragment
            or server.get("url") != url or server.get("transport") != "streamable-http"
            or set(server.get("toolFilter", {}).get("include", [])) != {"memory_add", "memory_search"}
            or config.get("tools", {}).get("allow") != [ADD, SEARCH]
            or set(agents) != {INGEST_AGENT, QA_AGENT}
            or agents[INGEST_AGENT].get("tools", {}).get("allow") != [ADD]
            or agents[QA_AGENT].get("tools", {}).get("allow") != [SEARCH]
            or config.get("plugins", {}).get("slots", {}).get("memory") != "none"):
        raise ValueError("Expected a dedicated HTTP Gateway with separate JiuwenMemory write and read Agents")


def memory_scope(request, scope_id):
    if not scope_id:
        raise ValueError("Missing history/QA scope")
    owner = request["runtime_context"]["run_dir"]
    return {"org": os.environ.get("JIUWENMEMORY_ORG", "local"),
            "user": os.environ.get("JIUWENMEMORY_USER", "developer"),
            "session": hashlib.sha256(f"{owner}:{scope_id}".encode()).hexdigest()}


def run(request):
    action = request["action"]
    inputs = request["inputs"]
    output = {}
    root = Path(request["runtime_context"]["run_dir"])
    receipts = root / "artifacts/memory_plugin/openclaw-jiuwenmemory"
    if action == "validate":
        validate()
    elif action == "before_agent":
        meta = inputs["agent_request"]["metadata"]
        scope = memory_scope(request, meta["scope_id"])
        if meta["purpose"] == "memory_ingest":
            params = {"scope": scope, "system_metadata": {"infer": "true"},
                      "user_metadata": {"history_session_id": meta["event_id"]}}
            if stamp := meta.get("occurred_at"):
                direct = Path(__file__).resolve().parents[3] / "memories/jiuwenmemory/scripts/run_operation.py"
                occurred_at = runpy.run_path(str(direct))["historical_time"](stamp)
                params["occurred_at"] = occurred_at
                params["system_metadata"]["observation_date"] = occurred_at[:10]
            output = {"agent_id": INGEST_AGENT, "write_arguments": params, "system_prompt_suffix":
                      "For this history ingestion turn, first call " + ADD + " exactly once. "
                      "OpenClaw may prepend a current clock timestamp in square brackets before the first line. "
                      "Omit that prefix, if present. Copy the history verbatim starting at the benchmark "
                      "conversation header, preserving all historical dates and line breaks. "
                      "Use exactly these additional arguments and omit all other optional arguments: "
                      + json.dumps(params) + ". After the call, reply exactly INGEST_OK."}
        elif meta["purpose"] == "memory_qa":
            output = {"agent_id": QA_AGENT, "scope": scope, "system_prompt_suffix":
                      "Before answering, call " + SEARCH + " once. Use the question as query and this exact "
                      "context: " + json.dumps({"scope": scope})}
        else:
            raise ValueError("Unexpected JiuwenMemory Agent purpose")
        key = meta["session_key"].lower()
        prefix = f'agent:{output["agent_id"]}:'
        if key.startswith("agent:") and not key.startswith(prefix):
            raise ValueError("Wrong Agent in the OpenClaw session key")
        output["session_key"] = key if key.startswith(prefix) else prefix + key
    elif action == "after_agent":
        agent_result = inputs["agent_result"]
        context = inputs["check_context"]
        state = Path(os.environ["OPENCLAW_STATE_DIR"])
        ingest = context["agent_id"] == INGEST_AGENT
        if ingest:
            content = inputs["agent_request"]["messages"][0]["content"]
            output = ingest_receipt(agent_result, context["write_arguments"], content, state)
        else:
            try:
                output = receipt(agent_result, context["scope"], state)
            except (OSError, ValueError) as exc:
                output = {
                    "session_id": agent_result.get("output", {}).get("session_id"),
                    "calls": None,
                    "verified_search": False,
                    "evidence": False,
                    "recall_verification_reason": type(exc).__name__,
                }
            output["recall_verified"] = output["verified_search"]
        receipts.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(request["task_id"].encode()).hexdigest()
        (receipts / f"{key}.json").write_text(json.dumps(output), encoding="utf-8")
        if ingest and not output["ingest_verified"]:
            return {"status": "failed", "state": "failed", "output": output,
                    "error": {"message": "Expected one verified MCP write with the original history"}}
    elif action == "finalize":
        rows = [json.loads(p.read_text()) for p in receipts.glob("*.json")]
        plan = json.loads((root / "records/composed_run_plan.json").read_text())
        total = sum(case.get("reference", {}).get("evaluation_target") == "qa_answer" for case in plan["cases"])
        writes = [row for row in rows if "ingest_verified" in row]
        rows = [row for row in rows if "verified_search" in row]
        planned_writes = sum(step.get("inputs", {}).get("metadata", {}).get("purpose") == "memory_ingest"
                             for step in plan["steps"])
        verified_writes = sum(row["ingest_verified"] for row in writes)
        called = sum((row["calls"] or 0) > 0 for row in rows)
        verified = sum(row["verified_search"] for row in rows)
        output = {"recall_mode": "agent_tool", "ingest_mode": "agent_tool",
                  "ingest_verification": {"planned": planned_writes, "verified": verified_writes,
                                          "coverage": verified_writes / planned_writes if planned_writes else None},
                  "recall_verification": {"planned_qa": total, "checked_qa": len(rows), "verified": verified,
                                          "unverified": total - verified,
                                          "coverage": verified / total if total else None,
                                          "tool_called": called,
                                          "tool_call_coverage": called / total if total else None,
                                          "with_evidence": sum(row["evidence"] for row in rows)}}
    elif action not in ("prepare", "set_phase"):
        raise ValueError("Unsupported JiuwenMemory MCP action")
    return {"status": "ok", "state": "completed", "output": output}


if __name__ == "__main__":
    try:
        result = run(json.load(sys.stdin))
    except Exception as exc:
        result = {"status": "failed", "state": "failed", "error": {
            "code": type(exc).__name__, "message": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}}
    json.dump(result, sys.stdout)
