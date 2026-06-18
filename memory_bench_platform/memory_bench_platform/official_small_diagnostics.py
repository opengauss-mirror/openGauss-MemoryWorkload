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


def _load_ov_log(run_dir: Path) -> str:
    candidates = sorted((_official_artifacts_dir(run_dir) / "remote_logs").glob("*.ov.log"))
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
    ov_log = _load_ov_log(run_dir)
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
    phase2_zero_token_sessions = 0
    phase2_llm_extract_present_sessions = 0
    durable_growth_sessions = 0
    durable_growth_with_zero_memory = 0
    durable_file_counts: list[int] = []
    for item in ingest_sessions:
        signals = item.get("extraction_signals", {}) or {}
        ov_detail = ((item.get("ov_observation") or {}).get("detail") or {}) if isinstance(item.get("ov_observation"), dict) else {}
        llm_total = int((ov_detail.get("llm_token_usage") or {}).get("total_tokens", 0) or 0)
        embedding_total = int((ov_detail.get("embedding_token_usage") or {}).get("total_tokens", 0) or 0)
        telemetry_summary = item.get("telemetry_summary") or ov_detail.get("telemetry_summary") or {}
        llm_extract_ms = (((telemetry_summary.get("memory") or {}).get("extract") or {}).get("stages") or {}).get("llm_extract_ms")
        if llm_total == 0 and embedding_total == 0:
            phase2_zero_token_sessions += 1
        if llm_extract_ms is not None:
            phase2_llm_extract_present_sessions += 1
        durable_file_count = int(signals.get("durable_memory_file_count", 0) or 0)
        durable_file_counts.append(durable_file_count)
        if durable_file_count > 0:
            durable_growth_sessions += 1
            if int(signals.get("memory_count", 0) or 0) == 0:
                durable_growth_with_zero_memory += 1

    reindex_result = meta.get("post_ingest_reindex") or {}
    observer_system = ((postrun or {}).get("observer_system") or {}).get("body", {}).get("result", {})
    queue_status = ((observer_system.get("components") or {}).get("queue") or {}).get("status", "")
    extract_compatibility = (preflight or {}).get("extract_compatibility") or {}
    signature_mismatch_errors = sorted(
        {
            match.group(1).strip()
            for match in re.finditer(
                r"unexpected keyword argument '([^']+)'",
                ov_log,
            )
        }
    )
    extract_runtime_errors = [
        line.strip()
        for line in ov_log.splitlines()
        if "Failed to extract memories with v2:" in line
        or "Agent memory extraction failed:" in line
    ]

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
                "phase2_zero_token_sessions": phase2_zero_token_sessions,
                "phase2_llm_extract_present_sessions": phase2_llm_extract_present_sessions,
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
            "post_ingest_reindex": reindex_result,
            "queue_status_text": queue_status,
            "extract_compatibility": extract_compatibility,
            "extract_runtime_errors": extract_runtime_errors[:20],
            "signature_mismatch_errors": signature_mismatch_errors,
        },
    }
    findings: list[str] = []
    if result["nodes"]["memory_capture"]["zero_memory_sessions"] >= max(1, result["nodes"]["session_construction"]["session_total"] - 1):
        findings.append("多数 session 已 commit 但未抽取到任何 memory，主异常位于 capture/extraction。")
    if durable_growth_with_zero_memory > 0:
        findings.append("durable memory 文件持续增长，但 session memory_count 仍为 0，说明平台观测口径与真实落盘结果不一致。")
    if phase2_zero_token_sessions == len(ingest_sessions) and ingest_sessions:
        findings.append("所有 phase2 commit task 都未产生 llm/embedding token，memory extraction 很可能在 prepare_inputs 之后提前空返回。")
    if phase2_llm_extract_present_sessions == 0 and ingest_sessions:
        findings.append("phase2 telemetry 中完全没有 llm_extract_ms，说明 memory extractor 没有真正进入 LLM 提取阶段。")
    if result["nodes"]["namespace_isolation"]["isolateUserScopeByAgent"] is False or result["nodes"]["namespace_isolation"]["isolateAgentScopeByUser"] is False:
        findings.append("namespace 隔离配置偏弱，可能放大 dedup / 跨 session 污染 / recall 不稳定。")
    if result["nodes"]["recall_query"]["search_find_calls"] > 0 and result["nodes"]["answer_generation"]["retrieval_miss_like_rows"] > 0:
        findings.append("recall 查询已发生，但大量回答仍是无信息模式，说明 recall 命中内容质量或可见性异常。")
    if result["nodes"]["recall_query"]["ledger_missing_rows"] > 0:
        findings.append("OpenClaw session ledger 大量缺失，session 侧可观测性不足，不能只依赖最终回答判断链路健康。")
    if isinstance(reindex_result, dict) and reindex_result and not reindex_result.get("ok", True):
        findings.append("post_ingest_reindex 未成功完成，QA 前的向量重建或索引可见性存在异常。")
    if "Embedding" in queue_status and "Requeued" in queue_status:
        findings.append("observer queue 显示 embedding 队列存在 pending/requeue 积压，索引或向量化链路可能阻塞后续可见性。")
    if isinstance(extract_compatibility, dict) and extract_compatibility:
        compat_error = extract_compatibility.get("error")
        provider_ok = bool(
            ((extract_compatibility.get("session_extract_context_provider") or {}).get("accepts_latest_archive_session_time"))
        )
        agent_ok = bool(
            ((extract_compatibility.get("extract_agent_memories") or {}).get("accepts_latest_archive_overview"))
            and ((extract_compatibility.get("extract_agent_memories") or {}).get("accepts_latest_archive_session_time"))
        )
        if compat_error:
            findings.append(
                "preflight 运行时接口自检无法完成："
                + str(compat_error)
                + "。当前 OpenViking 运行时可能缺模块或混装，不能视为可信评测环境。"
            )
        elif not provider_ok or not agent_ok:
            findings.append(
                "preflight 运行时接口自检失败：OpenViking extraction 相关函数签名与当前 session 调用路径不兼容。"
            )
    if signature_mismatch_errors:
        findings.append(
            "OpenViking 运行时出现 memory extraction 接口签名错配，关键异常参数为："
            + ", ".join(signature_mismatch_errors)
            + "。这说明当前运行时代码版本内部不自洽，extraction 在真正进入 LLM 前就已失败。"
        )
    if result["timing"]["ingest"]["count"] >= 2:
        ingest = result["timing"]["ingest"]
        if ingest["max_seconds"] >= ingest["p50_seconds"] * 2:
            findings.append("ingest 耗时分布离散，至少有一个 session 走了显著更重的 capture/extraction 路径。")
    result["findings"] = findings
    return result
