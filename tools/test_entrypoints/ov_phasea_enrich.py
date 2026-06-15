#!/usr/bin/env python3
from __future__ import annotations

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


def _parse_session_rows(master_log_text: str) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    for match in SESSION_PATTERN.finditer(master_log_text):
        rows[int(match.group(1))] = {
            "locomo_session_key": match.group(3),
            "task_id": match.group(4),
            "session_id": match.group(5),
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


def enrich_phasea_meta(
    *,
    meta_path: Path,
    master_log_path: Path,
    base_url: str,
    api_key: str,
    account_id: str,
    user_id: str,
    agent_id: str,
) -> dict[str, Any]:
    meta = _load_json(meta_path)
    session_rows = _parse_session_rows(master_log_path.read_text(encoding="utf-8", errors="ignore"))
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
            task_payload = task_result.get("result")
            if isinstance(task_payload, dict):
                telemetry_summary = task_payload.get("telemetry_summary")
                if isinstance(telemetry_summary, dict):
                    detail["telemetry_summary"] = telemetry_summary
            row["compact_task_id"] = task_id
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
    if len(argv) != 7:
        print(
            "usage: ov_phasea_enrich.py <meta_path> <master_log_path> <base_url> <api_key> <account_id> <user_id> <agent_id>",
            file=sys.stderr,
        )
        return 2

    meta_path = Path(argv[0])
    master_log_path = Path(argv[1])
    result = enrich_phasea_meta(
        meta_path=meta_path,
        master_log_path=master_log_path,
        base_url=argv[2],
        api_key=argv[3],
        account_id=argv[4],
        user_id=argv[5],
        agent_id=argv[6],
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
