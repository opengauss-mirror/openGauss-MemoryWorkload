from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency at runtime
    OpenAI = None


def export_hypotheses_from_case_results(run_dir: Path) -> Path:
    case_results = json.loads((run_dir / "reports" / "case_results.json").read_text(encoding="utf-8"))
    out_path = run_dir / "reports" / "longmemeval_hypotheses.jsonl"
    with out_path.open("w", encoding="utf-8") as handle:
        for row in case_results:
            handle.write(
                json.dumps(
                    {
                        "question_id": row["case_id"],
                        "hypothesis": row.get("response", ""),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return out_path


def get_anscheck_prompt(task: str, question: str, answer: str, response: str, abstention: bool = False) -> str:
    if not abstention:
        if task in ["single-session-user", "single-session-assistant", "multi-session"]:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \\n\\nQuestion: {}\\n\\nCorrect Answer: {}\\n\\nModel Response: {}\\n\\nIs the model response correct? Answer yes or no only."
            return template.format(question, answer, response)
        if task == "temporal-reasoning":
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \\n\\nQuestion: {}\\n\\nCorrect Answer: {}\\n\\nModel Response: {}\\n\\nIs the model response correct? Answer yes or no only."
            return template.format(question, answer, response)
        if task == "knowledge-update":
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\\n\\nQuestion: {}\\n\\nCorrect Answer: {}\\n\\nModel Response: {}\\n\\nIs the model response correct? Answer yes or no only."
            return template.format(question, answer, response)
        if task == "single-session-preference":
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\\n\\nQuestion: {}\\n\\nRubric: {}\\n\\nModel Response: {}\\n\\nIs the model response correct? Answer yes or no only."
            return template.format(question, answer, response)
        raise NotImplementedError(task)
    template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\\n\\nQuestion: {}\\n\\nExplanation: {}\\n\\nModel Response: {}\\n\\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
    return template.format(question, answer, response)


def score_run(run_dir: Path, ref_path: Path) -> dict[str, Any]:
    hypotheses_path = export_hypotheses_from_case_results(run_dir)
    references = json.loads(ref_path.read_text(encoding="utf-8"))
    qid2ref = {item["question_id"]: item for item in references}
    case_results = json.loads((run_dir / "reports" / "case_results.json").read_text(encoding="utf-8"))
    autoeval_rows = []
    total = 0
    correct = 0
    api_key = os.environ.get("LONGMEMEVAL_API_KEY", "")
    base_url = os.environ.get("LONGMEMEVAL_BASE_URL", "")
    metric_model = os.environ.get("LONGMEMEVAL_METRIC_MODEL", "custom")
    client = (
        OpenAI(api_key=api_key, base_url=base_url)
        if (api_key and base_url and metric_model and OpenAI is not None)
        else None
    )
    if client is None or not base_url or not metric_model:
        return {
            "status": "invalid",
            "reason": "LongMemEval LLM Judge configuration is required; lexical fallback is disabled",
            "primary_metric": "accuracy",
            "metrics": [],
            "artifacts": [{"path": str(hypotheses_path), "kind": "hypotheses"}],
        }
    for row in case_results:
        ref = qid2ref.get(row["case_id"])
        if ref is None:
            continue
        total += 1
        prompt = get_anscheck_prompt(
            ref["question_type"],
            ref["question"],
            ref["answer"],
            str(row.get("response", "")),
            abstention="_abs" in ref["question_id"],
        )
        completion = client.chat.completions.create(
            model=metric_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=10,
        )
        eval_response = (completion.choices[0].message.content or "").strip().lower()
        label = "yes" in eval_response
        autoeval_rows.append(
            {
                "question_id": row["case_id"],
                "hypothesis": row.get("response", ""),
                "autoeval_label": {"model": metric_model, "label": label},
            }
        )
        if label:
            correct += 1
    eval_path = run_dir / "reports" / "longmemeval_hypotheses.jsonl.eval-results-custom"
    with eval_path.open("w", encoding="utf-8") as handle:
        for row in autoeval_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {
        "status": "ok",
        "primary_metric": "accuracy",
        "metrics": [
            {
                "name": "accuracy",
                "value": round(correct / total, 4) if total else 0.0,
                "scope": "run",
                "unit": "ratio",
                "direction": "higher_is_better",
            }
        ],
        "artifacts": [
            {"path": str(hypotheses_path), "kind": "hypotheses"},
            {"path": str(eval_path), "kind": "judge_results"},
        ],
        "hypotheses_path": str(hypotheses_path),
        "eval_results_path": str(eval_path),
        "overall_accuracy": round(correct / total, 4) if total else 0.0,
        "total_scored": total,
        "scoring_mode": "llm",
    }


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: score_predictions.py RUN_DIR REF_JSON")
    result = score_run(Path(sys.argv[1]), Path(sys.argv[2]))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
