from __future__ import annotations

import json
import math
import re
import statistics
from pathlib import Path
from typing import Any


def _official_artifacts_dir(run_dir: Path) -> Path:
    root = run_dir / "external_artifacts"
    candidates = []
    for child in sorted(root.glob("official_*")):
        if child.is_dir() and list(child.glob("phaseA*_meta.json")):
            candidates.append(child)
    if not candidates:
        raise FileNotFoundError(f"phaseA meta not found under {run_dir}")
    return candidates[0]


def _load_meta(run_dir: Path) -> dict[str, Any]:
    candidates = sorted(_official_artifacts_dir(run_dir).glob("phaseA*_meta.json"))
    return json.loads(candidates[0].read_text(encoding="utf-8"))


def _load_master_log(run_dir: Path) -> str:
    candidates = sorted((_official_artifacts_dir(run_dir) / "remote_logs").glob("*.master.log"))
    return candidates[0].read_text(encoding="utf-8", errors="ignore") if candidates else ""


def _load_remote_snapshot(run_dir: Path, suffix: str) -> dict[str, Any] | None:
    candidates = sorted((_official_artifacts_dir(run_dir) / "remote_logs").glob(f"*.{suffix}.json"))
    if not candidates:
        return None
    try:
        return json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def _quantiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "min_seconds": 0.0, "max_seconds": 0.0, "mean_seconds": 0.0, "p50_seconds": 0.0, "p90_seconds": 0.0}
    ordered = sorted(values)
    p50 = statistics.median(ordered)
    p90_index = max(0, math.ceil(len(ordered) * 0.9) - 1)
    return {
        "count": len(ordered),
        "min_seconds": round(ordered[0], 4),
        "max_seconds": round(ordered[-1], 4),
        "mean_seconds": round(sum(ordered) / len(ordered), 4),
        "p50_seconds": round(p50, 4),
        "p90_seconds": round(ordered[p90_index], 4),
    }


def diagnose_official_small_run(run_dir: Path) -> dict[str, Any]:
    meta = _load_meta(run_dir)
    master_log = _load_master_log(run_dir)
    preflight = _load_remote_snapshot(run_dir, "preflight")
    postrun = _load_remote_snapshot(run_dir, "postrun")
    ingest_sessions = meta.get("ingest_sessions", [])
    qa_rows = meta.get("qa_rows", [])
    namespace = meta.get("plugin_namespace_config", {}).get("final", {})
    ov_log_tail = meta.get("ov_log_tail", [])
    memory_counts = [int(match.group(1)) for match in re.finditer(r"memories=(\d+)", master_log)]
    search_find_calls = sum(1 for line in ov_log_tail if "POST /api/v1/search/find" in line)
    content_read_calls = sum(1 for line in ov_log_tail if "GET /api/v1/content/read" in line)
    retrieval_miss_like = sum(
        1
        for row in qa_rows
        if any(
            phrase in str(row.get("response", "")).lower()
            for phrase in ["no information", "no mention", "don't have", "recalled memories"]
        )
    )
    ledger_missing = sum(1 for row in qa_rows if not row.get("openclaw_session_ledger", {}).get("found"))
    ingest_times = [float(item.get("compact_elapsed_seconds", 0.0) or 0.0) for item in ingest_sessions]
    qa_times = [float(item.get("elapsed_seconds", 0.0) or 0.0) for item in qa_rows]
    qa_tokens = [int((item.get("usage") or {}).get("total_tokens", 0) or 0) for item in qa_rows]
    durable_growth_sessions = 0
    durable_growth_with_zero_memory = 0
    durable_file_counts: list[int] = []
    for item in ingest_sessions:
        signals = item.get("extraction_signals", {}) or {}
        durable_file_count = int(signals.get("durable_memory_file_count", 0) or 0)
        durable_file_counts.append(durable_file_count)
        if durable_file_count > 0:
            durable_growth_sessions += 1
            if int(signals.get("memory_count", 0) or 0) == 0:
                durable_growth_with_zero_memory += 1

    result = {
        "run_id": meta.get("run_id", run_dir.name),
        "nodes": {
            "session_construction": {
                "session_total": len(ingest_sessions),
                "ov_session_ids": [item.get("ov_session_id", "") for item in ingest_sessions],
                "commit_completed": sum(1 for item in ingest_sessions if item.get("compact_status", {}).get("commit_status") == "completed"),
            },
            "namespace_isolation": {
                "accountId": namespace.get("accountId", ""),
                "userId": namespace.get("userId", ""),
                "agent_prefix": namespace.get("agent_prefix", ""),
                "isolateUserScopeByAgent": namespace.get("isolateUserScopeByAgent"),
                "isolateAgentScopeByUser": namespace.get("isolateAgentScopeByUser"),
            },
            "memory_capture": {
                "memory_counts": memory_counts,
                "zero_memory_sessions": sum(1 for item in memory_counts if item == 0),
                "sessions_with_extracted_memories": sum(1 for item in memory_counts if item > 0),
                "durable_memory_file_counts": durable_file_counts,
                "durable_growth_sessions": durable_growth_sessions,
                "durable_growth_with_zero_memory": durable_growth_with_zero_memory,
            },
            "recall_query": {
                "search_find_calls": search_find_calls,
                "content_read_calls": content_read_calls,
                "ledger_missing_rows": ledger_missing,
            },
            "answer_generation": {
                "qa_total": len(qa_rows),
                "retrieval_miss_like_rows": retrieval_miss_like,
                "qa_total_tokens": sum(qa_tokens),
            },
        },
        "timing": {
            "ingest": _quantiles(ingest_times),
            "qa": _quantiles(qa_times),
            "qa_tokens": {
                "count": len(qa_tokens),
                "min_tokens": min(qa_tokens) if qa_tokens else 0,
                "max_tokens": max(qa_tokens) if qa_tokens else 0,
                "mean_tokens": round(sum(qa_tokens) / len(qa_tokens), 2) if qa_tokens else 0.0,
            },
        },
        "runtime": {
            "preflight": preflight,
            "postrun": postrun,
        },
    }
    findings: list[str] = []
    if result["nodes"]["memory_capture"]["zero_memory_sessions"] >= max(1, result["nodes"]["session_construction"]["session_total"] - 1):
        findings.append("多数 session 已 commit 但未抽取到任何 memory，主异常位于 capture/extraction。")
    if durable_growth_with_zero_memory > 0:
        findings.append("durable memory 文件持续增长，但 session memory_count 仍为 0，说明平台观测口径与真实落盘结果不一致。")
    if result["nodes"]["namespace_isolation"]["isolateUserScopeByAgent"] is False or result["nodes"]["namespace_isolation"]["isolateAgentScopeByUser"] is False:
        findings.append("namespace 隔离配置偏弱，可能放大 dedup / 跨 session 污染 / recall 不稳定。")
    if result["nodes"]["recall_query"]["search_find_calls"] > 0 and result["nodes"]["answer_generation"]["retrieval_miss_like_rows"] > 0:
        findings.append("recall 查询已发生，但大量回答仍是无信息模式，说明 recall 命中内容质量或可见性异常。")
    if result["nodes"]["recall_query"]["ledger_missing_rows"] > 0:
        findings.append("OpenClaw session ledger 大量缺失，session 侧可观测性不足，不能只依赖最终回答判断链路健康。")
    if result["timing"]["ingest"]["count"] >= 2:
        ingest = result["timing"]["ingest"]
        if ingest["max_seconds"] >= ingest["p50_seconds"] * 2:
            findings.append("ingest 耗时分布离散，至少有一个 session 走了显著更重的 capture/extraction 路径。")
    result["findings"] = findings
    return result
