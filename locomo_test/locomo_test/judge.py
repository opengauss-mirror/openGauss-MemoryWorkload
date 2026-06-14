"""LLM judge — grade QA answers as CORRECT/WRONG."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys

import httpx

from .config import Config

try:
    from openai import AsyncOpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

SYSTEM_PROMPT = "You are an expert grader that determines if answers to questions match a gold standard answer"

ACCURACY_TEMPLATE = """
Your task is to label an answer to a question as 'CORRECT' or 'WRONG'. You will be given the following data:
    (1) a question (posed by one user to another user),
    (2) a 'gold' (ground truth) answer,
    (3) a generated answer
which you will score as CORRECT/WRONG.

The point of the question is to ask about something one user should know about the other user based on their prior conversations.
The gold answer will usually be a concise and short answer that includes the referenced topic, for example:
Question: Do you remember what I got the last time I went to Hawaii?
Gold answer: A shell necklace
The generated answer might be much longer, but you should be generous with your grading - as long as it touches on the same topic as the gold answer, it should be counted as CORRECT.

For time related questions, the gold answer will be a specific date, month, year, etc. The generated answer might be much longer or use relative time references (like "last Tuesday" or "next month"), but you should be generous with your grading - as long as it refers to the same date or time period as the gold answer, it should be counted as CORRECT. Even if the format differs (e.g., "May 7th" vs "7 May"), consider it CORRECT if it's the same date.

Now it's time for the real question:
Question: {question}
Gold answer: {gold_answer}
Generated answer: {response}

First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG.
Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.

Respond with JSON only: {{"is_correct": "CORRECT" or "WRONG", "reasoning": "your explanation"}}
"""


def _parse_grade(content: str) -> tuple[bool, str]:
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1:
        result = json.loads(content[start:end + 1].strip())
        is_correct = result.get("is_correct", "WRONG").strip().upper() == "CORRECT"
        return is_correct, result.get("reasoning", "")
    return False, f"[PARSE ERROR] Invalid response: {content}"


async def _grade_anthropic(
    client: httpx.AsyncClient, base_url: str, token: str, model: str,
    question: str, gold: str, response: str,
) -> tuple[bool, str]:
    prompt = ACCURACY_TEMPLATE.format(question=question, gold_answer=gold, response=response)
    body = {"model": model, "max_tokens": 1024, "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": token, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        resp = await client.post(f"{base_url}/v1/messages", json=body, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
        return _parse_grade(text.strip())
    except Exception as e:
        return False, f"[API ERROR] {e}"


async def _grade_openai(
    client, model: str,
    question: str, gold: str, response: str,
) -> tuple[bool, str]:
    prompt = ACCURACY_TEMPLATE.format(question=question, gold_answer=gold, response=response)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            temperature=0, timeout=60,
        )
        return _parse_grade(resp.choices[0].message.content.strip())
    except Exception as e:
        return False, f"[API ERROR] {e}"


def _load_csv(path: str) -> tuple[list[dict], list[str]]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        if "reasoning" not in fieldnames:
            fieldnames.append("reasoning")
        return list(reader), fieldnames


async def _run_judge(cfg: Config, csv_path: str):
    j = cfg.judge_env
    if not j.api_key:
        print("Error: judge API key is required (set in env.toml or ARK_API_KEY env var)", file=sys.stderr)
        sys.exit(1)

    api_format = j.api_format
    if api_format is None:
        api_format = "openai"
    print(f"    Judge: {api_format}, model={j.model}, parallel={j.parallel}", file=sys.stderr)

    rows, fieldnames = _load_csv(csv_path)
    ungraded = [i for i, r in enumerate(rows) if r.get("category") != "5" and not r.get("result")]
    print(f"    Total: {len(rows)}, ungraded: {len(ungraded)}", file=sys.stderr)
    if not ungraded:
        print("    All graded.", file=sys.stderr)
        return

    openai_client = None
    http_client = None
    if api_format == "openai":
        if not HAS_OPENAI:
            print("Error: openai package not installed", file=sys.stderr)
            sys.exit(1)
        openai_client = AsyncOpenAI(base_url=j.base_url, api_key=j.api_key)
    else:
        http_client = httpx.AsyncClient()

    sem = asyncio.Semaphore(j.parallel)
    file_lock = asyncio.Lock()

    async def save():
        async with file_lock:
            tmp = f"{csv_path}.tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            os.replace(tmp, csv_path)

    async def process(idx: int):
        async with sem:
            row = rows[idx]
            gold = row.get("expected") or row.get("answer")
            print(f"    Grading {idx+1}/{len(rows)}: {row['question'][:50]}...", file=sys.stderr)
            if api_format == "anthropic":
                ok, reason = await _grade_anthropic(http_client, j.base_url, j.api_key, j.model, row["question"], gold, row["response"])
            else:
                ok, reason = await _grade_openai(openai_client, j.model, row["question"], gold, row["response"])
            row["result"] = "CORRECT" if ok else "WRONG"
            row["reasoning"] = reason
            await save()

    try:
        await asyncio.gather(*[process(i) for i in ungraded])
    finally:
        if http_client:
            await http_client.aclose()

    correct = sum(1 for r in rows if r.get("category") != "5" and r.get("result") == "CORRECT")
    total = sum(1 for r in rows if r.get("category") != "5" and r.get("result"))
    acc = correct / total if total else 0
    print(f"\n    Judging done: {correct}/{total} correct, accuracy: {acc:.2%}", file=sys.stderr)


def run_judge(cfg: Config, output_dir: str):
    csv_path = os.path.join(output_dir, "qa_results.csv")
    if not os.path.exists(csv_path):
        print(f"Error: QA results not found: {csv_path}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(_run_judge(cfg, csv_path))
