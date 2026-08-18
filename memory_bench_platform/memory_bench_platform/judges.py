from __future__ import annotations

import json
import os
import re
from typing import Any
import urllib.request

from .protocol import JudgeInput, JudgeResult
from .evaluation import extract_observation
from .evaluation_profiles import resolve_evaluation_profile


SYSTEM_PROMPT = "You are an expert grader that determines if answers to questions match a gold standard answer"

LOCOMO_ACCURACY_TEMPLATE = """
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

LONGMEMEVAL_ACCURACY_TEMPLATE = """
Determine whether the model response correctly answers the question using the reference answer.
Accept semantically equivalent wording. For temporal-reasoning questions, do not penalize an
off-by-one difference when the requested answer is a duration. For knowledge-update questions,
accept the response when it contains the latest value even if it also mentions older information.
For preference questions, accept a response that correctly uses the stated personal preference.

Question type: {question_type}
Question: {question}
Reference answer or rubric: {gold_answer}
Generated answer: {response}
Unanswerable question: {abstention}

If the question is unanswerable, mark it correct only when the response recognizes that the
available information is insufficient. Respond with JSON only:
{{"is_correct": "CORRECT" or "WRONG", "reasoning": "one short explanation"}}
"""


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def run_builtin_judge(run_id: str, judge_input: JudgeInput) -> JudgeResult:
    expected = _normalize(str(judge_input.reference.get("expected_answer", "")))
    actual = _normalize(extract_observation(judge_input))

    passed = False
    label = "missing-answer"
    rationale = "No agent answer extracted from step results."
    score = 0.0

    profile = str(judge_input.reference.get("evaluation_profile") or "").strip() or None
    handler = resolve_evaluation_profile(profile)
    if handler.judge_mode != "builtin":
        raise ValueError(f"evaluation profile {handler.profile!r} requires an external judge")

    if actual:
        if actual == expected:
            passed = True
            label = "exact-match"
            rationale = "Agent answer matches expected answer after normalization."
            score = 1.0
        elif handler.builtin_strategy == "contains_match" and expected and expected in actual:
            passed = True
            label = "contains-match"
            rationale = "Agent answer contains expected answer after normalization."
            score = 1.0
        else:
            passed = False
            label = "mismatch"
            rationale = "Agent answer does not match expected answer."
            score = 0.0

    return JudgeResult(
        judge_id=f"{judge_input.case_id}-builtin",
        run_id=run_id,
        case_id=judge_input.case_id,
        score=score,
        label=label,
        passed=passed,
        rationale=rationale,
    )


def _judge_env(runtime_config: dict[str, Any], name: str, default: str) -> tuple[str, str]:
    env_config = runtime_config.get("env", {})
    env_name = str(env_config.get(name) or default) if isinstance(env_config, dict) else default
    return env_name, str(os.environ.get(env_name, "") or "").strip()


def _parse_llm_grade(content: str) -> tuple[bool, str]:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM judge returned non-JSON output")
    payload = json.loads(content[start : end + 1])
    grade = payload.get("is_correct", "WRONG")
    passed = grade if isinstance(grade, bool) else str(grade).strip().upper() == "CORRECT"
    return bool(passed), str(payload.get("reasoning", "") or "").strip()


def _llm_response_text(payload: dict[str, Any], api_format: str) -> str:
    if api_format == "anthropic":
        blocks = payload.get("content", [])
        if isinstance(blocks, list):
            return "".join(
                str(item.get("text", ""))
                for item in blocks
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
        return ""
    choices = payload.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    return str(message.get("content", "") or "").strip() if isinstance(message, dict) else ""


def _judge_prompt(runtime_config: dict[str, Any], judge_input: JudgeInput, actual: str) -> str:
    question = str(judge_input.reference.get("question", "") or "").strip()
    expected = str(judge_input.reference.get("expected_answer", "") or "").strip()
    profile = str(runtime_config.get("profile") or runtime_config.get("prompt_style") or "").strip().lower()
    template = runtime_config.get("prompt_template_text")
    if isinstance(template, str) and template.strip():
        return template.format(
            question=question,
            gold_answer=expected,
            response=actual,
            question_type=str(judge_input.reference.get("question_type", "") or ""),
            abstention=(
                "yes"
                if str(judge_input.reference.get("question_id", "")).endswith("_abs")
                else "no"
            ),
        )
    if profile in {"longmemeval", "longmemeval_qa@1"}:
        question_type = str(judge_input.reference.get("question_type", "") or "")
        question_id = str(judge_input.reference.get("question_id", judge_input.case_id) or "")
        return LONGMEMEVAL_ACCURACY_TEMPLATE.format(
            question_type=question_type,
            question=question,
            gold_answer=expected,
            response=actual,
            abstention="yes" if question_id.endswith("_abs") else "no",
        )
    if profile in {"locomo", "locomo_qa@1"}:
        return LOCOMO_ACCURACY_TEMPLATE.format(
            question=question,
            gold_answer=expected,
            response=actual,
        )
    raise ValueError(f"unknown judge profile: {profile or '<missing>'}")


def run_llm_judge(
    run_id: str,
    judge_input: JudgeInput,
    *,
    runtime_config: dict[str, Any] | None = None,
    urlopen=None,
) -> JudgeResult:
    runtime_config = runtime_config or {}
    question = str(judge_input.reference.get("question", "") or "").strip()
    expected = str(judge_input.reference.get("expected_answer", "") or "").strip()
    try:
        actual = extract_observation(judge_input)
    except ValueError as exc:
        return JudgeResult(
            judge_id=f"{judge_input.case_id}-llm",
            run_id=run_id,
            case_id=judge_input.case_id,
            score=None,
            label="judge-profile-invalid",
            passed=None,
            rationale=str(exc),
        )
    if not actual:
        return JudgeResult(
            judge_id=f"{judge_input.case_id}-llm",
            run_id=run_id,
            case_id=judge_input.case_id,
            score=0.0,
            label="missing-answer",
            passed=False,
            rationale="No agent answer extracted from step results.",
        )
    if not question or not expected:
        return JudgeResult(
            judge_id=f"{judge_input.case_id}-llm",
            run_id=run_id,
            case_id=judge_input.case_id,
            score=None,
            label="judge-input-invalid",
            passed=None,
            rationale="LLM judge requires both question and expected_answer.",
        )

    key_env, api_key = _judge_env(runtime_config, "api_key", "MEMORY_BENCH_JUDGE_API_KEY")
    base_env, base_url = _judge_env(runtime_config, "base_url", "MEMORY_BENCH_JUDGE_BASE_URL")
    model_env, model = _judge_env(runtime_config, "model", "MEMORY_BENCH_JUDGE_MODEL")
    missing = [name for name, value in ((key_env, api_key), (base_env, base_url), (model_env, model)) if not value]
    if missing:
        return JudgeResult(
            judge_id=f"{judge_input.case_id}-llm",
            run_id=run_id,
            case_id=judge_input.case_id,
            score=None,
            label="judge-config-missing",
            passed=None,
            rationale="Missing LLM judge configuration: " + ", ".join(missing),
        )

    api_format = str(runtime_config.get("api_format") or "openai").strip().lower()
    timeout = float(runtime_config.get("timeout_seconds") or 60)
    try:
        prompt = _judge_prompt(runtime_config, judge_input, actual)
    except ValueError as exc:
        return JudgeResult(
            judge_id=f"{judge_input.case_id}-llm",
            run_id=run_id,
            case_id=judge_input.case_id,
            score=None,
            label="judge-profile-invalid",
            passed=None,
            rationale=str(exc),
        )
    if api_format == "anthropic":
        endpoint = base_url.rstrip("/")
        endpoint += "/messages" if endpoint.endswith("/v1") else "/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": 256,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 256,
        }

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        content = _llm_response_text(payload, api_format)
        passed, rationale = _parse_llm_grade(content)
    except Exception as exc:
        return JudgeResult(
            judge_id=f"{judge_input.case_id}-llm",
            run_id=run_id,
            case_id=judge_input.case_id,
            score=None,
            label="judge-error",
            passed=None,
            rationale=f"LLM judge failed: {type(exc).__name__}: {exc}",
        )

    return JudgeResult(
        judge_id=f"{judge_input.case_id}-llm",
        run_id=run_id,
        case_id=judge_input.case_id,
        score=1.0 if passed else 0.0,
        label="correct" if passed else "wrong",
        passed=passed,
        rationale=rationale or ("LLM judge marked the answer correct." if passed else "LLM judge marked the answer wrong."),
    )
