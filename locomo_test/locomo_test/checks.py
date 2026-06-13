"""Health checks and post-step validation."""

from __future__ import annotations

import csv
import os
import sys

import requests

from .config import Config


def check_health(cfg: Config) -> bool:
    """Check Gateway, selected memory backend, and Judge API."""
    ok = True

    # Gateway — use /health endpoint (lightweight, no LLM call)
    gw_health = cfg.gateway.base_url + "/health"
    print(f"  Gateway: {gw_health} ... ", end="", file=sys.stderr)
    try:
        resp = requests.get(gw_health, timeout=10)
        print(f"OK ({resp.status_code})", file=sys.stderr)
    except Exception as e:
        print(f"FAIL ({e})", file=sys.stderr)
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
    csv_path = os.path.join(output_dir, "qa_results.csv")
    issues = {}

    if not os.path.exists(csv_path):
        issues["missing_csv"] = csv_path
        return issues

    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        issues["csv_read_error"] = str(e)
        return issues

    empty_responses = sum(1 for r in rows if not r.get("response") or r["response"].startswith("[ERROR"))
    if empty_responses:
        issues["empty_or_error_responses"] = empty_responses

    return issues


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
