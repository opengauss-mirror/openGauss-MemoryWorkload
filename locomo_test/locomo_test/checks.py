"""Health checks and post-step validation."""

from __future__ import annotations

import csv
import json
import os
import sys
import time

import requests

from .config import Config
from .eval import get_session_id_from_key, reset_session, send_message
from memory_bench_platform.locomo_test_metrics_bridge import (
    check_locomo_qa_results,
    summarize_locomo_qa_results,
    write_locomo_qa_diagnostics,
)


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def bootstrap_openviking_context(cfg: Config) -> bool:
    """Warm up the OpenViking-backed memory path so a fresh data dir creates schema eagerly."""
    bootstrap_user = f"_ov_bootstrap_{cfg.user}"
    session_key = f"bootstrap-{cfg.agent_id}-{cfg.user}"
    message = "OpenViking bootstrap probe. Reply with OK only."
    print("  OpenViking bootstrap: sending warmup request ... ", end="", file=sys.stderr)
    try:
        reply, _ = send_message(
            cfg.gateway.base_url,
            cfg.gateway.token,
            bootstrap_user,
            message,
            cfg.agent_id,
            session_key,
        )
        print(f"OK ({reply[:32]})", file=sys.stderr)
    except Exception as e:
        print(f"FAIL ({e})", file=sys.stderr)
        return False

    found = get_session_id_from_key(session_key, bootstrap_user, cfg.agent_id, cfg.gateway.state_dir)
    if found:
        session_file, sessions_dir = found
        session_path = session_file if os.path.isabs(session_file) else os.path.join(sessions_dir, session_file)
        if not session_path.endswith(".jsonl"):
            session_path += ".jsonl"
        reset_session(session_path, cfg.agent_id, cfg.gateway.state_dir)
    return True


def _check_http_with_retry(
    label: str,
    url: str,
    *,
    attempts: int = 3,
    timeout: int = 10,
) -> bool:
    for attempt in range(1, attempts + 1):
        print(f"  {label}: {url} ... ", end="", file=sys.stderr)
        try:
            resp = requests.get(url, timeout=timeout)
            print(f"OK ({resp.status_code})", file=sys.stderr)
            return True
        except Exception as e:
            if attempt >= attempts:
                print(f"FAIL ({e})", file=sys.stderr)
                return False
            print(f"RETRY ({e})", file=sys.stderr)
            time.sleep(attempt)
    return False


def check_health(cfg: Config) -> bool:
    """Check Gateway, selected memory backend, and Judge API."""
    ok = True

    # Gateway — use /health endpoint (lightweight, no LLM call)
    gw_health = cfg.gateway.base_url + "/health"
    if not _check_http_with_retry("Gateway", gw_health, attempts=4, timeout=10):
        ok = False

    if cfg.memory_mode == "openviking":
        ov_url = f"{cfg.openviking.api_url}/health"
        print(f"  OpenViking: {ov_url} ... ", end="", file=sys.stderr)
        try:
            resp = requests.get(ov_url, timeout=10)
            if resp.status_code == 200:
                print("OK", file=sys.stderr)
            else:
                print(f"WARN ({resp.status_code})", file=sys.stderr)
        except Exception as e:
            print(f"FAIL ({e})", file=sys.stderr)
            ok = False
        if ok and _truthy_env("LOCOMO_OPENVIKING_BOOTSTRAP", default=True):
            ok = bootstrap_openviking_context(cfg) and ok
    elif cfg.memory_mode == "ogmem":
        og_url = f"{cfg.ogmem.api_url}/api/v1/health"
        print(f"  oGMemory: {og_url} ... ", end="", file=sys.stderr)
        try:
            resp = requests.get(og_url, timeout=10)
            if resp.status_code == 200:
                print("OK", file=sys.stderr)
            else:
                print(f"FAIL ({resp.status_code})", file=sys.stderr)
                ok = False
        except Exception as e:
            print(f"FAIL ({e})", file=sys.stderr)
            ok = False
    else:
        print(f"  Memory backend: skipped ({cfg.memory_mode})", file=sys.stderr)

    # Judge API — send a minimal request to verify auth
    j = cfg.judge_env
    api_format = j.api_format or ("anthropic" if "/coding" in j.base_url else "openai")
    print(f"  Judge API: {j.base_url} ({api_format}) ... ", end="", file=sys.stderr)
    try:
        # Always try openai-style Bearer auth first (works for Volcengine /coding too)
        # Only use anthropic x-api-key when explicitly set
        if api_format == "anthropic" and j.api_format == "anthropic":
            resp = requests.post(
                f"{j.base_url}/v1/messages",
                json={"model": j.model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]},
                headers={"x-api-key": j.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                timeout=15,
            )
        else:
            resp = requests.post(
                f"{j.base_url}/chat/completions",
                json={"model": j.model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]},
                headers={"Authorization": f"Bearer {j.api_key}", "Content-Type": "application/json"},
                timeout=15,
            )
        if resp.status_code in (401, 403):
            print(f"AUTH FAIL ({resp.status_code})", file=sys.stderr)
            ok = False
        elif resp.status_code in (200, 400):
            # 200=OK, 400=bad request (model/subscription issue but auth works)
            print(f"OK ({resp.status_code})", file=sys.stderr)
        else:
            print(f"WARN ({resp.status_code})", file=sys.stderr)
    except Exception as e:
        print(f"FAIL ({e})", file=sys.stderr)
        ok = False

    return ok


def check_qa_results(output_dir: str) -> dict:
    """Post-QA check: verify CSV integrity. Returns issues dict."""
    return check_locomo_qa_results(output_dir)


def summarize_qa_results(output_dir: str) -> dict:
    """Build run-level QA diagnostics from qa_results.csv."""
    return summarize_locomo_qa_results(output_dir)


def write_qa_diagnostics(output_dir: str) -> dict:
    """Write qa_diagnostics.json and return its content."""
    return write_locomo_qa_diagnostics(output_dir)


def check_judge_results(output_dir: str) -> dict:
    """Post-judge check: verify grading completeness and sanity."""
    csv_path = os.path.join(output_dir, "qa_results.csv")
    issues = {}

    if not os.path.exists(csv_path):
        return {"missing_csv": csv_path}

    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        return {"csv_read_error": str(e)}

    valid = [r for r in rows if r.get("category") != "5"]
    ungraded = sum(1 for r in valid if not r.get("result"))
    if ungraded:
        issues["ungraded"] = ungraded

    graded = [r for r in valid if r.get("result")]
    if graded:
        correct = sum(1 for r in graded if r["result"] == "CORRECT")
        acc = correct / len(graded)
        if acc == 0.0:
            issues["accuracy_zero"] = True
        elif acc == 1.0:
            issues["accuracy_perfect"] = True

    return issues


def report_issues(step: str, issues: dict):
    """Print issues as warnings."""
    if not issues:
        return
    print(f"\n  [{step}] Warnings:", file=sys.stderr)
    for k, v in issues.items():
        print(f"    - {k}: {v}", file=sys.stderr)
