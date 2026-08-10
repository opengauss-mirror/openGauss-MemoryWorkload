from __future__ import annotations

import json
import sys

from memory_bench_platform.judges import run_llm_judge
from memory_bench_platform.protocol import JudgeInput
from skills.benchmarks.locomo.scripts import score_predictions


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _input() -> JudgeInput:
    return JudgeInput(
        case_id="case-1",
        reference={
            "question": "When did Caroline attend the group?",
            "expected_answer": "7 May 2023",
            "expected_step_id": "answer-step",
        },
        step_results=[
            {
                "step_id": "answer-step",
                "structured_output": {"agent_answer": "Caroline attended on May 7, 2023."},
            }
        ],
    )


def test_llm_judge_uses_semantic_grade(monkeypatch):
    monkeypatch.setenv("TEST_JUDGE_KEY", "secret")
    monkeypatch.setenv("TEST_JUDGE_URL", "https://judge.example/v1")
    monkeypatch.setenv("TEST_JUDGE_MODEL", "judge-model")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "is_correct": "CORRECT",
                                    "reasoning": "The two date formats are equivalent.",
                                }
                            )
                        }
                    }
                ]
            }
        )

    result = run_llm_judge(
        "run-1",
        _input(),
        runtime_config={
            "profile": "locomo_qa@1",
            "api_format": "openai",
            "timeout_seconds": 12,
            "env": {
                "api_key": "TEST_JUDGE_KEY",
                "base_url": "TEST_JUDGE_URL",
                "model": "TEST_JUDGE_MODEL",
            },
        },
        urlopen=fake_urlopen,
    )

    assert result.passed is True
    assert result.label == "correct"
    assert result.rationale == "The two date formats are equivalent."
    assert captured["url"] == "https://judge.example/v1/chat/completions"
    assert captured["timeout"] == 12
    assert captured["body"]["model"] == "judge-model"
    assert "7 May 2023" in captured["body"]["messages"][1]["content"]
    assert "May 7, 2023" in captured["body"]["messages"][1]["content"]


def test_llm_judge_does_not_fall_back_to_string_matching(monkeypatch):
    monkeypatch.delenv("MEMORY_BENCH_JUDGE_API_KEY", raising=False)
    monkeypatch.delenv("MEMORY_BENCH_JUDGE_BASE_URL", raising=False)
    monkeypatch.delenv("MEMORY_BENCH_JUDGE_MODEL", raising=False)

    result = run_llm_judge("run-1", _input())

    assert result.passed is None
    assert result.score is None
    assert result.label == "judge-config-missing"
    assert "MEMORY_BENCH_JUDGE_API_KEY" in result.rationale


def test_llm_judge_timeout_is_ungraded(monkeypatch):
    monkeypatch.setenv("TEST_JUDGE_KEY", "secret")
    monkeypatch.setenv("TEST_JUDGE_URL", "https://judge.example/v1")
    monkeypatch.setenv("TEST_JUDGE_MODEL", "judge-model")

    def fail_urlopen(request, timeout):
        del request, timeout
        raise TimeoutError("judge timed out")

    result = run_llm_judge(
        "run-1",
        _input(),
        runtime_config={
            "profile": "locomo_qa@1",
            "env": {
                "api_key": "TEST_JUDGE_KEY",
                "base_url": "TEST_JUDGE_URL",
                "model": "TEST_JUDGE_MODEL",
            }
        },
        urlopen=fail_urlopen,
    )

    assert result.passed is None
    assert result.score is None
    assert result.label == "judge-error"


def test_longmemeval_prompt_uses_question_type(monkeypatch):
    monkeypatch.setenv("TEST_JUDGE_KEY", "secret")
    monkeypatch.setenv("TEST_JUDGE_URL", "https://judge.example/v1")
    monkeypatch.setenv("TEST_JUDGE_MODEL", "judge-model")
    captured = {}

    def fake_urlopen(request, timeout):
        del timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"is_correct": "CORRECT", "reasoning": "Equivalent."}
                            )
                        }
                    }
                ]
            }
        )

    judge_input = _input().model_copy(deep=True)
    judge_input.reference["question_type"] = "knowledge-update"
    judge_input.reference["question_id"] = "q-update"
    result = run_llm_judge(
        "run-1",
        judge_input,
        runtime_config={
            "prompt_style": "longmemeval",
            "env": {
                "api_key": "TEST_JUDGE_KEY",
                "base_url": "TEST_JUDGE_URL",
                "model": "TEST_JUDGE_MODEL",
            },
        },
        urlopen=fake_urlopen,
    )

    assert result.passed is True
    assert "knowledge-update" in captured["body"]["messages"][1]["content"]


def test_retrieval_observation_is_judged(monkeypatch):
    monkeypatch.setenv("TEST_JUDGE_KEY", "secret")
    monkeypatch.setenv("TEST_JUDGE_URL", "https://judge.example/v1")
    monkeypatch.setenv("TEST_JUDGE_MODEL", "judge-model")
    captured = {}

    def fake_urlopen(request, timeout):
        del timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": '{"is_correct":"CORRECT","reasoning":"Evidence matches."}'}}]})

    judge_input = _input().model_copy(deep=True)
    judge_input.reference["evaluation_extractor"] = "evidence_text"
    judge_input.step_results[0]["structured_output"] = {
        "output": {"evidence_text": "Caroline attended on May 7, 2023."}
    }
    result = run_llm_judge(
        "run-1",
        judge_input,
        runtime_config={
            "profile": "locomo_qa@1",
            "env": {"api_key": "TEST_JUDGE_KEY", "base_url": "TEST_JUDGE_URL", "model": "TEST_JUDGE_MODEL"},
        },
        urlopen=fake_urlopen,
    )
    assert result.passed is True
    assert "May 7, 2023" in captured["body"]["messages"][1]["content"]


def test_unknown_judge_profile_is_not_treated_as_locomo(monkeypatch):
    monkeypatch.setenv("TEST_JUDGE_KEY", "secret")
    monkeypatch.setenv("TEST_JUDGE_URL", "https://judge.example/v1")
    monkeypatch.setenv("TEST_JUDGE_MODEL", "judge-model")
    result = run_llm_judge(
        "run-1",
        _input(),
        runtime_config={
            "profile": "unknown@1",
            "env": {"api_key": "TEST_JUDGE_KEY", "base_url": "TEST_JUDGE_URL", "model": "TEST_JUDGE_MODEL"},
        },
    )
    assert result.passed is None
    assert result.label == "judge-profile-invalid"


def test_legacy_score_cli_accepts_optional_data_path(monkeypatch, capsys):
    captured = {}

    def fake_score_run(run_dir):
        captured["run_dir"] = str(run_dir)
        return {"status": "ok"}

    monkeypatch.setattr(score_predictions, "score_run", fake_score_run)
    monkeypatch.setattr(sys, "argv", ["score_predictions.py", "runs/run-1", "data/locomo.json"])

    score_predictions.main()

    assert captured["run_dir"] == "runs/run-1"
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}
