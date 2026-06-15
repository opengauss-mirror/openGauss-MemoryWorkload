from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None


SYSTEM_PROMPT = "You are an expert grader that determines if answers to questions match a gold standard answer"


def get_locomo_prompt(question: str, gold_answer: str, response: str) -> str:
    return f"""Your task is to label an answer to a question as 'CORRECT' or 'WRONG'. You will be given the following data:
    (1) a question,
    (2) a 'gold' (ground truth) answer,
    (3) a generated answer
which you will score as CORRECT/WRONG.

For time related questions, the gold answer will be a specific date, month, year, etc. The generated answer might be much longer or use relative time references (like "last Tuesday" or "next month"), but you should be generous with your grading - as long as it refers to the same date or time period as the gold answer, it should be counted as CORRECT. Even if the format differs (e.g., "May 7th" vs "7 May"), consider it CORRECT if it's the same date.

Question: {question}
Gold answer: {gold_answer}
Generated answer: {response}

Respond with JSON only: {{"is_correct": "CORRECT" or "WRONG", "reasoning": "your explanation"}}"""


def export_locomo_case_results_to_csv(run_dir: Path) -> Path:
    rows = json.loads((run_dir / "reports" / "case_results.json").read_text(encoding="utf-8"))
    out_path = run_dir / "reports" / "locomo_qa_results.csv"
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "question", "expected", "response", "category"])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "case_id": row["case_id"],
                    "question": row.get("question", ""),
                    "expected": row.get("expected_answer", ""),
                    "response": row.get("response", ""),
                    "category": row.get("category", ""),
                }
            )
    return out_path


def _parse_grade(content: str) -> tuple[bool, str]:
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1:
        result = json.loads(content[start:end + 1].strip())
        is_correct = result.get("is_correct", "WRONG").strip().upper() == "CORRECT"
        return is_correct, result.get("reasoning", "")
    return False, f"[PARSE ERROR] Invalid response: {content}"


def score_run(run_dir: Path) -> dict[str, Any]:
    csv_path = export_locomo_case_results_to_csv(run_dir)
    api_key = os.environ.get("LOCOMO_API_KEY", "")
    base_url = os.environ.get("LOCOMO_BASE_URL", "")
    model = os.environ.get("LOCOMO_METRIC_MODEL", "")
    if not (api_key and base_url and model and OpenAI is not None):
        return {
            "status": "missing_judge_config",
            "csv_path": str(csv_path),
        }

    client = OpenAI(api_key=api_key, base_url=base_url)
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    scored = []
    correct = 0
    for row in rows:
        prompt = get_locomo_prompt(row["question"], row["expected"], row["response"])
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=256,
        )
        ok, reason = _parse_grade((completion.choices[0].message.content or "").strip())
        scored.append(
            {
                "case_id": row["case_id"],
                "question": row["question"],
                "expected": row["expected"],
                "response": row["response"],
                "result": "CORRECT" if ok else "WRONG",
                "reasoning": reason,
            }
        )
        if ok:
            correct += 1
    out_path = run_dir / "reports" / "locomo_qa_results.scored.json"
    out_path.write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "csv_path": str(csv_path),
        "scored_path": str(out_path),
        "overall_accuracy": round(correct / len(scored), 4) if scored else 0.0,
        "total_scored": len(scored),
        "scoring_mode": "llm",
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: score_predictions.py RUN_DIR")
    print(json.dumps(score_run(Path(sys.argv[1])), ensure_ascii=False))


if __name__ == "__main__":
    main()
