"""Accuracy statistics and meta.json generation."""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

from .config import Config


def run_stats(
    cfg: Config,
    output_dir: str,
    *,
    memory_token_totals: dict | None = None,
    ov_token_totals: dict | None = None,
):
    """Compute accuracy by category, token totals, write meta.json."""
    if memory_token_totals is None:
        memory_token_totals = ov_token_totals
    csv_path = os.path.join(output_dir, "qa_results.csv")
    if not os.path.exists(csv_path):
        print(f"Error: {csv_path} not found", file=sys.stderr)
        return

    with open(csv_path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Filter category 5
    valid = [r for r in rows if r.get("category") != "5"]

    # Per-category stats
    cat_stats: dict[str, dict] = {}
    for r in valid:
        cat = r.get("category", "?")
        if cat not in cat_stats:
            cat_stats[cat] = {"correct": 0, "total": 0}
        if r.get("result"):
            cat_stats[cat]["total"] += 1
            if r["result"] == "CORRECT":
                cat_stats[cat]["correct"] += 1

    # Overall
    total_correct = sum(s["correct"] for s in cat_stats.values())
    total_graded = sum(s["total"] for s in cat_stats.values())
    overall_acc = total_correct / total_graded if total_graded else 0.0

    # Token totals
    token_keys = ["input_tokens", "output_tokens", "cacheRead", "cacheWrite", "total_tokens"]
    token_totals = {}
    for k in token_keys:
        token_totals[k] = sum(int(r.get(k, 0) or 0) for r in valid)

    # Print summary
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  Overall: {total_correct}/{total_graded} = {overall_acc:.2%}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"  {'Category':<10} {'Correct':<10} {'Total':<10} {'Accuracy':<10}", file=sys.stderr)
    print(f"  {'-'*40}", file=sys.stderr)
    for cat in sorted(cat_stats.keys()):
        s = cat_stats[cat]
        acc = s["correct"] / s["total"] if s["total"] else 0
        print(f"  {cat:<10} {s['correct']:<10} {s['total']:<10} {acc:.2%}", file=sys.stderr)
    print(f"  {'-'*40}", file=sys.stderr)
    print(f"  QA tokens: in={token_totals['input_tokens']:,} out={token_totals['output_tokens']:,} cacheRead={token_totals['cacheRead']:,} total={token_totals['total_tokens']:,}", file=sys.stderr)
    if total_correct > 0:
        tok_per_correct = token_totals["total_tokens"] / total_correct
        print(f"  Tokens/correct: {tok_per_correct:,.0f}", file=sys.stderr)
    if memory_token_totals and (memory_token_totals.get("llm_total") or memory_token_totals.get("embedding")):
        provider = memory_token_totals.get("provider") or cfg.memory_mode
        label = "OV" if provider == "openviking" else "oGMemory" if provider == "ogmem" else provider
        print(
            f"  {label} tokens: llm_prompt={memory_token_totals['llm_prompt']:,} "
            f"llm_completion={memory_token_totals['llm_completion']:,} "
            f"llm_total={memory_token_totals['llm_total']:,} "
            f"embed={memory_token_totals['embedding']:,} "
            f"memories={memory_token_totals['memories']:,}",
            file=sys.stderr,
        )

    memory_provider = (memory_token_totals or {}).get("provider", cfg.memory_mode)

    # Write meta.json
    meta = {
        "name": cfg.name,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": cfg.dataset,
        "data_file": cfg.data_file,
        "samples": cfg.samples,
        "session_policy": cfg.session.policy.value,
        "memory_mode": cfg.memory_mode,
        "parallel": cfg.parallel,
        "overall_accuracy": round(overall_acc, 4),
        "total_correct": total_correct,
        "total_graded": total_graded,
        "total_questions": len(valid),
        "accuracy_by_category": {
            cat: {
                "correct": s["correct"],
                "total": s["total"],
                "accuracy": round(s["correct"] / s["total"], 4) if s["total"] else 0,
            }
            for cat, s in sorted(cat_stats.items())
        },
        "token_totals": token_totals,
        "memory_token_totals": memory_token_totals or {},
        "ov_token_totals": memory_token_totals if memory_provider == "openviking" else {},
        "ogmem_token_totals": memory_token_totals if memory_provider == "ogmem" else {},
    }

    meta_path = os.path.join(output_dir, "meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"  meta.json written to {meta_path}", file=sys.stderr)
