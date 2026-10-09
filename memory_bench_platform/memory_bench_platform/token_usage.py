"""Best-effort, replayable token accounting. Never controls execution or grading.

Observes Agent responses/transcript increments, OV tasks, and
explicitly opted-in oGMemory counter windows and generic model proxy requests. It does not claim that
these sources cover all backend requests, SDK retries, or child agents.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import urllib.request
import uuid

LOG = logging.getLogger(__name__)
_CONTEXT = ContextVar("token_usage_context", default=None)
FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
          "cache_write_tokens", "reasoning_tokens")


def enabled():
    return os.environ.get("MEMORY_BENCH_TOKEN_USAGE", "1").lower() not in {"0", "false", "off"}


def _object(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_usage(raw, profile="openai-compatible-v1", model_kind="llm"):
    raw = _object(raw)
    def get(*keys):
        return next((value for k in keys if (value := _number(raw.get(k))) is not None), None)
    inp, out = get("input_tokens", "prompt_tokens"), get("output_tokens", "completion_tokens")
    alias_issues = []
    chosen_alias = "input_output"
    if profile == "openai-compatible-v1" and model_kind != "embedding":
        modern = (_number(raw.get("input_tokens")), _number(raw.get("output_tokens")))
        legacy = (_number(raw.get("prompt_tokens")), _number(raw.get("completion_tokens")))
        reported_alias_total = get("total_tokens", "totalTokens", "total")
        if all(value is not None for value in modern + legacy) and modern != legacy:
            alias_issues.append("conflicting_usage_aliases")
            modern_matches = reported_alias_total == sum(modern)
            legacy_matches = reported_alias_total == sum(legacy)
            if legacy_matches and not modern_matches:
                inp, out = legacy
                chosen_alias = "prompt_completion"
        elif all(value is not None for value in legacy) and not all(value is not None for value in modern):
            inp, out = legacy
            chosen_alias = "prompt_completion"
    input_details = ("prompt_tokens_details" if chosen_alias == "prompt_completion"
                     else "input_tokens_details")
    output_details = ("completion_tokens_details" if chosen_alias == "prompt_completion"
                      else "output_tokens_details")
    cache = _number(_object(raw.get(input_details) or raw.get("prompt_tokens_details")).get("cached_tokens"))
    if cache is None:
        cache = get("cached_tokens")  # OpenViking task aggregates flatten usage details.
    write = get("cache_write_tokens")
    reasoning = _number(_object(raw.get(output_details) or raw.get("completion_tokens_details")).get("reasoning_tokens"))
    if reasoning is None:
        reasoning = get("reasoning_tokens")
    semantics = "included_in_input"
    if profile == "openclaw-v1":
        inp, out = get("input"), get("output")
        cache, write = get("cacheRead"), get("cacheWrite")
        inp = inp + cache + write if all(v is not None for v in (inp, cache, write)) else None
        semantics = "additive_input"
    elif profile == "anthropic-v1":
        cache, write = get("cache_read_input_tokens"), get("cache_creation_input_tokens")
        # Anthropic omits these optional fields when caching is unused.
        inp = inp + (cache or 0) + (write or 0) if inp is not None else None
        semantics = "additive_input"
    elif profile == "ogmemory-counter-v1":
        cache, write = get("cache_read"), get("cache_write")
    elif profile == "openclaw-responses-v1":
        # OpenClaw toUsage(): input_tokens excludes cache, while total_tokens
        # includes cacheRead/cacheWrite. The HTTP schema omits that breakdown.
        reported_total = get("total_tokens")
        inp = (reported_total - out if reported_total is not None and out is not None
               and inp is not None and reported_total >= inp + out else None)
        cache = write = None
        semantics = "cache_in_reported_total_breakdown_unavailable"
    elif profile != "openai-compatible-v1":
        inp = out = cache = write = reasoning = None
        semantics = "unknown"
    reported = get("total_tokens", "totalTokens", "total")
    if model_kind == "embedding":
        inp = inp if inp is not None else reported
        out = None
        total = inp
    else:
        total = inp + out if inp is not None and out is not None else None
    issues = alias_issues
    if reported is not None and total is not None and reported != total:
        issues.append("reported_total_mismatch")
    availability = "complete" if total is not None else "partial" if any(
        v is not None for v in (inp, out, reported)) else "missing"
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": total,
            "reported_total_tokens": reported, "cache_read_tokens": cache,
            "cache_write_tokens": write, "reasoning_tokens": reasoning,
            "normalization_profile": profile, "cache_semantics": semantics,
            "availability": availability, "measurement": "reported" if raw else "unknown",
            "raw_usage": raw, "issues": issues}


def memory_source(ctx=None):
    return (ctx or {}).get("memory_token_source", os.environ.get("MEMORY_BENCH_MEMORY_TOKEN_SOURCE", "native"))


def current_context():
    return _CONTEXT.get()


def context(runtime, **values):
    return {"run_dir": runtime.run_dir, "run_id": runtime.run_id,
            "memory_backend": runtime.memory_id, "memory_integration": runtime.memory_integration,
            "memory_token_source": memory_source(), **values}


def initialize(ctx):
    if enabled():
        _atomic(Path(ctx["run_dir"]) / "records/token-usage/context.json",
                json.dumps({k: v for k, v in ctx.items() if k != "run_dir"}))


def _agent_phase(meta):
    if meta.get("purpose") == "memory_ingest":
        return "ingest"
    return "qa" if meta.get("purpose") == "memory_qa" or meta.get("question_id") else "unknown"


@contextmanager
def scope(values):
    token = _CONTEXT.set(values if enabled() else None)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def best_effort(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        # Avoid leaking endpoint credentials or provider bodies into diagnostics.
        LOG.warning("Token accounting %s unavailable (%s)", function.__name__, type(exc).__name__)
        try:
            _append(_CONTEXT.get(), {"record_type": "diagnostic", "code": "collection_failed",
                                    "collector": function.__name__, "error_type": type(exc).__name__})
        except Exception:
            pass
        return None


def _append(ctx, row):
    if not ctx or not enabled():
        return
    row = {"schema_version": "token-usage/1", **{k: v for k, v in ctx.items() if k != "run_dir"}, **row}
    row.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    row.setdefault("observation_id", uuid.uuid4().hex)
    root = Path(ctx["run_dir"]) / "records/token-usage/shards"
    root.mkdir(parents=True, exist_ok=True)
    # Each thread/process has its own append-only shard. No shared JSONL writer.
    with (root / f"usage-{os.getpid()}-{threading.get_ident()}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


def warning(code, **details):
    best_effort(_append, _CONTEXT.get(), {"record_type": "diagnostic", "code": code, **details})


def record(raw=None, *, ctx=None, profile="openai-compatible-v1", **fields):
    return best_effort(_record, raw, ctx=ctx, profile=profile, **fields)


def _record(raw=None, *, ctx=None, profile="openai-compatible-v1", **fields):
    ctx = ctx or _CONTEXT.get()
    if not ctx or not enabled():
        return
    if fields.get("component") == "evaluator" or fields.get("phase", ctx.get("phase")) == "judge":
        return
    row = {"record_type": "usage", "component": "unknown", "phase": "unknown",
           "operation": "unknown", "source": "unknown", "model_kind": "llm", "model": None,
           "granularity": "request", "selected": True, "call_path_coverage": "unknown",
           **{k: ctx[k] for k in ("phase",) if k in ctx}, **fields}
    row.update(normalize_usage(raw, profile, row["model_kind"]))
    if row["component"] == "memory":
        policy = memory_source(ctx)
        is_proxy = row["source"] in {"model_proxy", "mem0_model_proxy"}
        selected_source = (policy == "model_proxy" and is_proxy) or (policy == "native" and not is_proxy)
        if not selected_source:
            row.update(selected=False, exclusion_reason="memory_source_policy")
    row.setdefault("record_id", uuid.uuid4().hex)
    best_effort(_append, ctx, row)


def _response_usage(result):
    raw = _object(result.get("raw"))
    payload = _object(raw.get("result", raw))
    meta = _object(payload.get("meta"))
    agent = _object(meta.get("agentMeta"))
    usage = payload.get("usage", agent.get("usage"))
    return usage, payload.get("model") or agent.get("model"), payload.get("id")


def _session_candidates(request, result):
    meta = request.metadata
    aid = str(meta.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID") or "main")
    folder = (Path(os.environ.get("OPENCLAW_STATE_DIR") or Path.home() / ".openclaw") / "agents" / aid / "sessions").resolve()
    paths = [Path(a["path"]) for a in result.get("artifacts", [])
             if a.get("kind") == "openclaw_session_jsonl" and a.get("path")]
    explicit = meta.get("session_jsonl") or os.environ.get("OPENCLAW_SESSION_JSONL")
    if explicit:
        paths.append(Path(explicit))
    sid = _object(result.get("output")).get("session_id")
    transport = result.get("transport") or os.environ.get("OPENCLAW_TRANSPORT", "").strip().lower()
    if not transport:
        transport = "http" if (os.environ.get("OPENCLAW_GATEWAY_URL")
                               or os.environ.get("OPENCLAW_GATEWAY_BASE_URL")) else "cli"
    key = str(meta.get("session_key") or "")
    if transport == "http":
        # The HTTP runner sends an explicit session key, not the requested ID as
        # the transcript UUID. Failed calls have no output from which to read it.
        if not key and meta.get("session_id"):
            key = f"agent:{aid}:explicit:{meta['session_id']}"
    elif not sid:
        sid = meta.get("session_id")
    if not sid and key:
        index = folder / "sessions.json"
        full_key = key.lower() if key.lower().startswith("agent:") else f"agent:{aid.lower()}:{key.lower()}"
        if index.is_file():
            sid = _object(json.loads(index.read_text()).get(full_key)).get("sessionId")
        if not sid and transport != "http":
            h = hashlib.sha256(key.encode()).hexdigest()[:32]
            sid = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    if sid:
        paths.append(folder / f"{sid}.jsonl")
    # The explicit path, state lookup and artifact can name the same file via
    # different relative paths/symlinks. They are one evidence source.
    return folder, list(dict.fromkeys(p.resolve() for p in paths))


def _session_offsets(request):
    folder, paths = _session_candidates(request, {})
    return {str(p): (p.stat().st_ino, p.stat().st_size)
            for p in set(paths + list(folder.glob("*.jsonl"))) if p.is_file()}


def _collect_agent(request, result, before, failed, attempt_id, agent_id):
    meta = request.metadata
    phase = _agent_phase(meta)
    outcome = "failed" if failed or result.get("status") == "failed" else "succeeded"
    common = {"component": "agent", "phase": phase, "operation": "answer",
              "sample_id": meta.get("sample_id"), "outcome": outcome, "agent_attempt_id": attempt_id}
    usage, model, provider_id = _response_usage(result)
    try:
        _, paths = _session_candidates(request, result) if before is not None else (None, [])
    except (OSError, ValueError):
        warning("transcript_lookup_failed")
        paths = []
    messages = []
    for path in paths:
        try:
            if not path.is_file() or before is None:
                continue
            inode, offset = before.get(str(path), (path.stat().st_ino, 0))
            if path.stat().st_ino != inode or path.stat().st_size < offset:
                warning("transcript_replaced_or_truncated", evidence_ref=str(path))
                continue
            with path.open("rb") as f:
                f.seek(offset)
                while True:
                    position = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    try:
                        row = _object(json.loads(line))
                    except (ValueError, UnicodeError):
                        warning("transcript_invalid_line", evidence_ref=str(path), offset=position)
                        continue
                    msg = _object(row.get("message"))
                    if msg.get("role") != "assistant":
                        continue
                    # Preserve model attempts without usage as unknown, not zero.
                    mid = row.get("id") or f"byte-{position}"
                    rid = "session:" + str(path) + ":" + str(mid)
                    messages.append(rid)
                    record(msg.get("usage"), profile="openclaw-v1", **common,
                           source="openclaw_session", model=msg.get("model"), provider=msg.get("provider"),
                           session_id=path.stem, record_id=rid, evidence_ref=f"{path}#byte={position}")
        except OSError:
            warning("transcript_read_failed", evidence_ref=str(path))
    # Response summaries and transcript calls are overlapping sources, never add both.
    profile = "openclaw-v1" if isinstance(usage, dict) and "input" in usage else (
        "openclaw-responses-v1" if agent_id == "openclaw" else "openai-compatible-v1")
    record(usage, profile=profile,
           **common, source="agent_response", granularity="agent_attempt", model=model,
           provider_request_id=provider_id, record_id="agent:" + attempt_id, selected=not bool(messages),
           revision=1, superseded_by=messages, evidence_ref="raw_usage in this observation")


def capture_agent(runner, agent_id, request):
    if not _CONTEXT.get() or not enabled():
        return runner(agent_id, request)
    before = best_effort(_session_offsets, request) if agent_id == "openclaw" else None
    attempt_id = uuid.uuid4().hex
    record(component="agent", source="agent_response", granularity="agent_attempt", outcome="started",
           phase=_agent_phase(request.metadata),
           record_id="agent:" + attempt_id, agent_attempt_id=attempt_id, revision=0)
    result, failed = {}, True
    try:
        result = runner(agent_id, request)
        failed = False
        return result
    finally:
        best_effort(_collect_agent, request, _object(result), before, failed, attempt_id, agent_id)


def collect_ov_task(payload):
    if not _CONTEXT.get() or _CONTEXT.get().get("memory_backend") != "openviking":
        return
    output = _object(payload.get("output"))
    task_id = output.get("task_id") or _object(payload.get("operation")).get("task_id")
    if not task_id or "task_result" not in output:
        return
    task_result = _object(output["task_result"])
    usage = _object(task_result.get("token_usage"))
    for kind in ("llm", "embedding"):
        raw = _object(usage.get(kind))
        record(raw, component="memory", phase="ingest", operation="extract",
               source="openviking_task", granularity="task", model_kind=kind, model=raw.get("model"),
               native_task_id=task_id, record_id=f"ov-task:{task_id}:{kind}",
               outcome=output.get("backend_status", payload.get("state", "unknown")))


def _counter_snapshot(ctx):
    if not enabled() or ctx.get("memory_backend") != "ogmemory":
        return None
    if os.environ.get("MEMORY_BENCH_OGMEMORY_EXCLUSIVE") != "1":
        return None
    container = os.environ.get("MEMORY_BENCH_OGMEMORY_CONTAINER")
    base = os.environ.get("OGMEM_API_URL")
    if not container or not base:
        raise ValueError("Exclusive counter collection requires container identity and API URL")
    def generation():
        p = subprocess.run(["docker", "inspect", "--format", "{{.Id}} {{.State.StartedAt}} {{.State.Running}}", container],
                           capture_output=True, text=True, check=True, timeout=3)
        if not p.stdout.strip().endswith(" true"):
            raise ValueError("counter container is not running")
        return p.stdout.strip()
    identity = generation()
    headers = {}
    if api_key := os.environ.get("OGMEM_API_KEY"):
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(base.rstrip("/") + "/api/v1/token_stats", headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        stats = json.load(response)
    if generation() != identity:
        raise ValueError("counter container restarted during snapshot")
    return {"generation": identity, "exclusive": True, "stats": stats,
            "timestamp": datetime.now(timezone.utc).isoformat()}


def start_counters(ctx):
    with scope(ctx):
        return best_effort(_counter_snapshot, ctx)


def record_counter_window(ctx, before, after):
    valid = bool(before and after and before.get("exclusive") and after.get("exclusive")
                 and before.get("generation") and before["generation"] == after.get("generation"))
    for kind in ("llm", "embedding"):
        b = _object(_object(_object(before).get("stats")).get(kind))
        a = _object(_object(_object(after).get("stats")).get(kind))
        delta = {}
        reset = False
        for field in ("input_tokens", "output_tokens", "total_tokens", "cache_read", "cache_write", "calls"):
            bv, av = _number(b.get(field)), _number(a.get(field))
            if bv is not None and av is not None:
                if av < bv:
                    reset = True
                else:
                    delta[field] = av - bv
        usable = valid and not reset
        record(delta if usable else {}, ctx=ctx, profile="ogmemory-counter-v1",
               component="memory", phase="unknown", operation="unknown",
               granularity="run", source="ogmemory_counter", model_kind=kind,
               record_id="og-counter:" + kind + ":" + str(_object(before).get("timestamp")),
               counter_valid=usable, counter_before=before, counter_after=after,
               counter_error=None if usable else "reset_restart_or_unverified_window")


def finish_counters(ctx, before):
    if before is not None or (ctx.get("memory_backend") == "ogmemory"
                              and os.environ.get("MEMORY_BENCH_OGMEMORY_EXCLUSIVE") == "1"):
        after = start_counters(ctx)
        best_effort(record_counter_window, ctx, before, after)


def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _aggregate_fields(members):
    """Field completeness concerns captured evidence, not invisible backend calls."""
    result = {"observations": len(members), "field_coverage": {}}
    for field in FIELDS:
        known = [r[field] for r in members if r.get(field) is not None]
        observed = sum(known) if known else None
        result["observed_" + field] = observed
        # A known subtotal must not masquerade as a complete cache/input total.
        result[field] = observed if len(known) == len(members) else None
        result["field_coverage"][field] = {
            "known": len(known), "missing": len(members) - len(known),
            "fraction": len(known) / len(members) if members else None,
        }
    return result


def _cost_categories(records, integration):
    selectors = {
        "memory_llm": lambda r: r.get("component") == "memory" and r.get("model_kind") == "llm",
        "agent_qa": lambda r: r.get("component") == "agent" and r.get("phase") == "qa" and r.get("model_kind") == "llm",
        "agent_ingest": lambda r: r.get("component") == "agent" and r.get("phase") == "ingest" and r.get("model_kind") == "llm",
        "agent_other": lambda r: r.get("component") == "agent" and r.get("phase") not in {"qa", "ingest"} and r.get("model_kind") == "llm",
        "memory_embedding": lambda r: r.get("component") == "memory" and r.get("model_kind") == "embedding",
    }
    costs = {}
    for label, select in selectors.items():
        members = [r for r in records if r.get("phase") != "preflight" and select(r)]
        costs[label] = _aggregate_fields(members)
        costs[label]["status"] = ("observed" if members else
                                  "not_applicable" if label == "agent_ingest" and integration == "backend_direct"
                                  else "unobserved")
        costs[label]["call_path_coverage"] = "unknown"
    return costs


def rebuild(run_dir):
    """Offline/idempotent rebuild. Reads only captured shards, never calls models."""
    root = Path(run_dir)
    if not root.is_dir():
        raise ValueError("run directory does not exist")
    rows, diagnostics = [], []
    excluded_judge_observations = 0
    for path in sorted((root / "records/token-usage/shards").glob("*.jsonl")):
        for number, line in enumerate(path.read_bytes().splitlines(), 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("schema_version") != "token-usage/1":
                    raise ValueError("invalid record")
                if row.get("record_type") == "usage" and (
                    not isinstance(row.get("record_id"), str)
                    or not isinstance(row.get("availability"), str)
                    or row.get("availability") not in {"complete", "partial", "missing"}
                    or any(k not in row or (row[k] is not None and _number(row[k]) is None) for k in FIELDS)
                ):
                    raise ValueError("invalid usage fields")
            except (ValueError, UnicodeError):
                diagnostics.append({"code": "invalid_shard_line", "file": str(path.relative_to(root)), "line": number})
                continue
            if row.get("record_type") == "diagnostic":
                diagnostics.append(row)
            else:
                # Old v1 shards remain immutable, but Judge is outside this report.
                if row.get("component") == "evaluator" or row.get("phase") == "judge":
                    excluded_judge_observations += 1
                    continue
                # Rebuild derived fields from retained evidence. The shards are
                # immutable; correcting a mapping does not require model calls.
                row.update(normalize_usage(row.get("raw_usage"),
                                           row.get("normalization_profile", "unknown"),
                                           row.get("model_kind", "llm")))
                rows.append(row)
    selected = {}
    for row in sorted(rows, key=lambda r: (r.get("timestamp", ""), r.get("observation_id", ""))):
        key = (row.get("run_id"), row.get("record_id"))
        prior = selected.get(key)
        if prior and row.get("revision", -1) > prior.get("revision", -1):
            selected[key] = row
            continue
        if prior and row.get("granularity") not in {"task", "run"}:
            if any(prior.get(k) != row.get(k) for k in FIELDS):
                diagnostics.append({"code": "source_conflict", "record_id": key[1]})
            continue
        if prior and row.get("availability") == "missing" and prior.get("availability") != "missing":
            diagnostics.append({"code": "aggregate_usage_disappeared", "record_id": key[1]})
            continue
        selected[key] = row
    records = sorted((r for r in selected.values() if r.get("selected", True)),
                     key=lambda r: (str(r.get("run_id")), str(r.get("record_id"))))
    groups = {}
    for row in records:
        key = tuple(row.get(k) for k in ("component", "phase", "model_kind", "model", "source"))
        groups.setdefault(key, []).append(row)
    breakdown = []
    for key, members in sorted(groups.items(), key=lambda pair: str(pair[0])):
        group = dict(zip(("component", "phase", "model_kind", "model", "source"), key))
        group.update(missing_usage=sum(x["availability"] == "missing" for x in members),
                     partial_usage=sum(x["availability"] == "partial" for x in members),
                     call_path_coverage="unknown", complete_total_tokens=None)
        group.update(_aggregate_fields(members))
        # Task/window/Agent-response aggregates are not single provider requests.
        requests = [x for x in members if x.get("granularity") == "request"]
        group["request_usage_coverage"] = (sum(x["availability"] == "complete" for x in requests) / len(requests)
                                           if requests else None)
        breakdown.append(group)
    def subtotal(component=None, phase=None, kind="llm"):
        values = [r["total_tokens"] for r in records if r.get("model_kind") == kind
                  and (component is None or r.get("component") == component)
                  and (phase is None or r.get("phase") == phase) and r.get("total_tokens") is not None
                  and r.get("phase") != "preflight"]
        return sum(values) if values else None
    context_file = root / "records/token-usage/context.json"
    run_context = json.loads(context_file.read_text()) if context_file.exists() else {}
    integration = run_context.get("memory_integration") or next(
        (r.get("memory_integration") for r in records if r.get("memory_integration")), None)
    backend = run_context.get("memory_backend") or next(
        (r.get("memory_backend") for r in records if r.get("memory_backend")), None)
    summary = {"schema_version": "token-summary/2", "selected_observations": len(records),
               "memory_integration": integration, "memory_backend": backend,
               "judge_accounting": "excluded", "excluded_legacy_judge_observations": excluded_judge_observations,
               "cost_categories": _cost_categories(records, integration),
               "source_observations": len(rows), "call_path_coverage": "partial" if records else "unknown",
               "observed_memory_llm_tokens": subtotal("memory"), "observed_agent_ingest_tokens": subtotal("agent", "ingest"),
               "observed_agent_qa_tokens": subtotal("agent", "qa"), "complete_system_llm_tokens": None,
               "observed_memory_embedding_tokens": subtotal("memory", kind="embedding"),
               "observed_system_llm_tokens": (sum(v for v in (subtotal("memory"), subtotal("agent")) if v is not None)
                                              if any(v is not None for v in (subtotal("memory"), subtotal("agent"))) else None),
               "system_tokens_per_correct": None, "breakdown": breakdown,
               "limitations": ["Observed subtotals, not complete cost: backend QA, SDK retries and child agents may be unobserved.",
                               "Input includes cache. CacheRead/CacheWrite are input breakdowns, never added again to total.",
                               "Field coverage describes captured observations only, not all requests. Unknown is not zero.",
                               "Judge usage is not collected or included. Grading behavior is unchanged.",
                               "Embedding is separate from LLM totals. Missing values are null, not zero.",
                               "Memory proxy accounting requires exclusive configured model routes. Native and proxy sources are never added together for a run."],
               "diagnostic_count": len(diagnostics)}
    summary["source_coverage"] = {
        "agent": "observed" if any(r.get("component") == "agent" for r in records) else "unobserved",
        "openviking_tasks": "partial" if any(r.get("source") == "openviking_task" for r in records) else "unobserved",
        "ogmemory_exclusive_window": "partial" if any(r.get("counter_valid") for r in records) else "unobserved",
        "memory_model_proxy": "partial" if any(r.get("source") in {"model_proxy", "mem0_model_proxy"} for r in records) else "unobserved",
    }
    summary["memory_token_source"] = run_context.get("memory_token_source", "legacy")
    summary["excluded_memory_source_observations"] = sum(
        r.get("exclusion_reason") == "memory_source_policy" for r in rows)
    diagnostics.extend({"code": issue, "record_id": r["record_id"]} for r in records for issue in r.get("issues", []))
    summary["diagnostic_count"] = len(diagnostics)
    _atomic(root / "records/token_usage.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    _atomic(root / "records/token_usage_diagnostics.json", json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n")
    _atomic(root / "reports/token_summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    lines = ["# Token usage (system only; Judge excluded)", "",
             "Input includes cache. Total = Input + Output; never add CacheRead/CacheWrite again.",
             "Unknown means unavailable, not zero. All numbers below are observed subtotals, not complete system costs.", "",
             "## Cost categories", "",
             "| Category | Status | Input incl. cache | Output | CacheRead | CacheWrite | Total | CacheRead known/observed |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for label, g in summary["cost_categories"].items():
        vals = [label, g["status"], *[g["observed_" + k] for k in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens")],
            f'{g["field_coverage"]["cache_read_tokens"]["known"]}/{g["observations"]}']
        lines.append("| " + " | ".join("unknown" if v is None else str(v) for v in vals) + " |")
    lines.extend(["", "## By model and source", "",
                  "| Component | Phase | Kind | Model | Source | Input incl. cache | Output | CacheRead | CacheWrite | Total |",
                  "|---|---|---|---|---|---:|---:|---:|---:|---:|"])
    for g in breakdown:
        vals = [g[k] for k in ("component", "phase", "model_kind", "model", "source", "observed_input_tokens", "observed_output_tokens", "observed_cache_read_tokens", "observed_cache_write_tokens", "observed_total_tokens")]
        lines.append("| " + " | ".join("unknown" if v is None else str(v).replace("|", "\\|") for v in vals) + " |")
    lines.extend(["", *summary["limitations"], "", f"Diagnostics: {len(diagnostics)}"])
    _atomic(root / "reports/token_summary.md", "\n".join(lines) + "\n")
    return summary
