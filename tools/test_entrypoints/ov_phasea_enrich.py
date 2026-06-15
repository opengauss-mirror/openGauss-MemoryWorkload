#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

import requests


SESSION_PATTERN = re.compile(
    r"\[phaseA\]\[session (\d+)/(\d+)\]\[direct-ov\] (session_\d+) task=([0-9a-f\-]+) session_id=([0-9a-f\-]+) memories=(\d+)"
)


def _headers(*, api_key: str, account_id: str, user_id: str, agent_id: str) -> dict[str, str]:
    return {
        "X-API-Key": api_key,
        "X-OpenViking-Account": account_id,
        "X-OpenViking-User": user_id,
        "X-Agent-ID": agent_id,
    }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _parse_session_rows(master_log_text: str) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    for match in SESSION_PATTERN.finditer(master_log_text):
        rows[int(match.group(1))] = {
            "locomo_session_key": match.group(3),
            "task_id": match.group(4),
            "session_id": match.group(5),
            "memory_count": match.group(6),
        }
    return rows


def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any] | None:
    response = requests.get(url, headers=headers, timeout=20)
    if not response.ok:
        return None
    body = response.json()
    if not isinstance(body, dict):
        return None
    result = body.get("result")
    return result if isinstance(result, dict) else None


def _extract_telemetry_summary(task_result: dict[str, Any]) -> dict[str, Any] | None:
    telemetry = task_result.get("telemetry_summary")
    if isinstance(telemetry, dict):
        return telemetry
    nested = task_result.get("result")
    if isinstance(nested, dict):
        nested_telemetry = nested.get("telemetry_summary")
        if isinstance(nested_telemetry, dict):
            return nested_telemetry
    return None


def _task_duration_seconds(task_result: dict[str, Any]) -> float | None:
    created_at = task_result.get("created_at_iso") or task_result.get("created_at")
    updated_at = task_result.get("updated_at_iso") or task_result.get("updated_at")
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        return None
    try:
        from datetime import datetime

        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (updated - created).total_seconds())


def _build_minimal_meta(*, run_id: str, csv_path: Path, session_rows: dict[int, dict[str, str]]) -> dict[str, Any]:
    qa_rows: list[dict[str, Any]] = []
    for row in _load_csv_rows(csv_path):
        usage: dict[str, int] = {}
        for key in ("input_tokens", "output_tokens", "cacheRead", "cacheWrite", "total_tokens"):
            raw = row.get(key, "")
            if raw == "":
                continue
            try:
                usage[key] = int(raw)
            except ValueError:
                continue
        try:
            qi = int(row.get("qi", "0") or 0)
        except ValueError:
            qi = 0
        try:
            sample_idx = int(row.get("sample_idx", "0") or 0)
        except ValueError:
            sample_idx = 0
        try:
            rounds = int(row.get("rounds", "0") or 0)
        except ValueError:
            rounds = 0
        try:
            elapsed_seconds = float(row.get("elapsed_seconds", "0") or 0.0)
        except ValueError:
            elapsed_seconds = 0.0
        qa_rows.append(
            {
                "sample_id": row.get("sample_id", ""),
                "sample_idx": sample_idx,
                "qi": qi,
                "question": row.get("question", ""),
                "expected": row.get("expected", ""),
                "response": row.get("response", ""),
                "category": row.get("category", ""),
                "evidence": row.get("evidence", ""),
                "elapsed_seconds": elapsed_seconds,
                "rounds": rounds,
                "jsonl_filename": row.get("jsonl_filename", ""),
                "token_usage_source": "qa_csv",
                "usage": usage,
            }
        )

    sample_idx = qa_rows[0].get("sample_idx", 0) if qa_rows else 0
    sample_id = qa_rows[0].get("sample_id", "") if qa_rows else ""
    ingest_sessions: list[dict[str, Any]] = []
    for idx in sorted(session_rows):
        row = session_rows[idx]
        ingest_sessions.append(
            {
                "index": idx,
                "locomo_session_key": row["locomo_session_key"],
                "session_key": row["locomo_session_key"],
                "memory_count": int(row["memory_count"]),
                "ov_session_id": row["session_id"],
                "compact_task_id": row["task_id"],
                "compact_status": {},
                "ov_observation": {"detail": {}},
            }
        )

    return {
        "phase": "phaseA",
        "mode": "on",
        "run_id": run_id,
        "sample": sample_idx,
        "sample_id": sample_id,
        "qa_question_count": len(qa_rows),
        "ingest_sessions": ingest_sessions,
        "qa_rows": qa_rows,
    }


def enrich_phasea_meta(
    *,
    meta_path: Path,
    csv_path: Path | None,
    master_log_path: Path,
    base_url: str,
    api_key: str,
    account_id: str,
    user_id: str,
    agent_id: str,
) -> dict[str, Any]:
    session_rows = _parse_session_rows(master_log_path.read_text(encoding="utf-8", errors="ignore"))
    if meta_path.exists():
        meta = _load_json(meta_path)
    else:
        if csv_path is None or not csv_path.exists():
            raise FileNotFoundError(f"meta missing and csv not found: {csv_path}")
        meta = _build_minimal_meta(
            run_id=meta_path.stem.removesuffix("_meta"),
            csv_path=csv_path,
            session_rows=session_rows,
        )
    headers = _headers(
        api_key=api_key,
        account_id=account_id,
        user_id=user_id,
        agent_id=agent_id,
    )
    patched_sessions = 0
    patched_tasks = 0

    for row in meta.get("ingest_sessions", []):
        try:
            idx = int(row.get("index", 0) or 0)
        except (TypeError, ValueError):
            continue
        ids = session_rows.get(idx)
        if not ids:
            continue

        session_id = ids["session_id"]
        task_id = ids["task_id"]
        observation = row.setdefault("ov_observation", {})
        detail = observation.setdefault("detail", {})

        session_result = _get_json(f"{base_url.rstrip('/')}/api/v1/sessions/{session_id}", headers)
        if session_result:
            detail.update(session_result)
            row["ov_session_id"] = session_id
            patched_sessions += 1

        task_result = _get_json(f"{base_url.rstrip('/')}/api/v1/tasks/{task_id}", headers)
        if task_result:
            detail["_ov_task"] = task_result
            telemetry_summary = _extract_telemetry_summary(task_result)
            if telemetry_summary:
                detail["telemetry_summary"] = telemetry_summary
                row["telemetry_summary"] = telemetry_summary
            duration_seconds = _task_duration_seconds(task_result)
            if duration_seconds is not None and not row.get("compact_elapsed_seconds"):
                row["compact_elapsed_seconds"] = round(duration_seconds, 3)
            row["compact_task_id"] = task_id
            compact_status = row.setdefault("compact_status", {})
            if isinstance(compact_status, dict):
                compact_status.setdefault("commit_status", task_result.get("status", ""))
                compact_status.setdefault("commit_task_id", task_id)
            patched_tasks += 1

    meta.setdefault("telemetry_backfill", {})
    meta["telemetry_backfill"].update(
        {
            "patched_sessions": patched_sessions,
            "patched_tasks": patched_tasks,
            "master_log": str(master_log_path),
        }
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta["telemetry_backfill"]


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    if len(argv) not in {7, 8}:
        print(
            "usage: ov_phasea_enrich.py <meta_path> [csv_path] <master_log_path> <base_url> <api_key> <account_id> <user_id> <agent_id>",
            file=sys.stderr,
        )
        return 2

    meta_path = Path(argv[0])
    if len(argv) == 8:
        csv_path = Path(argv[1])
        master_log_path = Path(argv[2])
        arg_offset = 1
    else:
        csv_path = None
        master_log_path = Path(argv[1])
        arg_offset = 0
    result = enrich_phasea_meta(
        meta_path=meta_path,
        csv_path=csv_path,
        master_log_path=master_log_path,
        base_url=argv[2 + arg_offset],
        api_key=argv[3 + arg_offset],
        account_id=argv[4 + arg_offset],
        user_id=argv[5 + arg_offset],
        agent_id=argv[6 + arg_offset],
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
