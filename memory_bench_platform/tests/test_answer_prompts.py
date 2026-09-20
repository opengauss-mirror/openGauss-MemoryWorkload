import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_bench_platform.answer_prompts import load_answer_prompt, render_answer_prompt, snapshot_prompts
from memory_bench_platform.benchmark_scenario import ScenarioQuestion
from memory_bench_platform.composer import compose_run_plan
from test_scenario_composer_golden import _scenario, _binding


def config(name):
    root = Path("skills/benchmarks") / name
    import yaml
    data = yaml.safe_load((root / "manifest.yaml").read_text())
    return root, SimpleNamespace(id=name, answering=data["answering"], judging=data["judging"])


def test_both_integrations_share_dataset_rules():
    root, manifest = config("locomo")
    prompt = load_answer_prompt(root, manifest)
    systems = []
    scenario = _scenario()
    for event in scenario.samples[0].timeline:
        if event.evaluation:
            for question in event.evaluation.questions:
                question.metadata["question_date"] = "2023-06-27"
    for mode in ("backend_direct", "agent_plugin"):
        plan = compose_run_plan(scenario, _binding(mode), answer_prompt=prompt)
        systems.append([s["inputs"]["system_prompt"] for s in plan["steps"]
                        if s["operator_kind"] == "agent" and "answer" in s["step_id"]])
    assert systems[0] and systems[0] == systems[1]
    assert all("reasonable inferences" in text for text in systems[0])


def test_longmemeval_requires_and_renders_question_date():
    root, manifest = config("longmemeval")
    prompt = load_answer_prompt(root, manifest)
    question = ScenarioQuestion(question_id="q", question="How long ago?")
    with pytest.raises(ValueError, match="question_date"):
        render_answer_prompt(prompt, question)
    question.metadata["question_date"] = "2024-01-02"
    assert "Question date: 2024-01-02" in render_answer_prompt(prompt, question)
    assert "2024-01-02" not in prompt["template"]


def test_snapshot_hash_matches_actual_bytes(tmp_path):
    import hashlib
    root, manifest = config("locomo")
    prompt = load_answer_prompt(root, manifest)
    snapshot_prompts(tmp_path, prompt, root, manifest, "backend_direct")
    record = json.loads((tmp_path / "prompt_manifest.json").read_text())
    for item in record["prompts"].values():
        assert item["sha256"] == hashlib.sha256((tmp_path / item["snapshot"]).read_bytes()).hexdigest()
    changed = dict(prompt, template=prompt["template"] + "Changed.")
    snapshot_prompts(tmp_path, changed, root, manifest, "backend_direct")
    updated = json.loads((tmp_path / "prompt_manifest.json").read_text())
    assert updated["prompts"]["answer"]["sha256"] != record["prompts"]["answer"]["sha256"]


def test_unknown_field_and_path_escape_fail(tmp_path):
    manifest = SimpleNamespace(answering={"profile": "x@1", "prompt_template": "answer.txt"})
    (tmp_path / "answer.txt").write_text("{gold_answer}")
    with pytest.raises(ValueError, match="unsupported"):
        load_answer_prompt(tmp_path, manifest)
    manifest.answering["prompt_template"] = "../outside.txt"
    with pytest.raises(ValueError, match="inside"):
        load_answer_prompt(tmp_path, manifest)


@pytest.mark.parametrize("benchmark", ["locomo", "longmemeval"])
def test_missing_date_fails_both_plans_before_execution(benchmark):
    root, manifest = config(benchmark)
    prompt = load_answer_prompt(root, manifest)
    for mode in ("backend_direct", "agent_plugin"):
        with pytest.raises(ValueError, match="question_date"):
            compose_run_plan(_scenario(), _binding(mode), answer_prompt=prompt)


def test_answer_does_not_receive_reference_answer():
    root, manifest = config("longmemeval")
    prompt = load_answer_prompt(root, manifest)
    question = ScenarioQuestion(question_id="q", question="When?", reference="SECRET_GOLD",
                                metadata={"question_date": "2024-01-02"})
    assert "SECRET_GOLD" not in render_answer_prompt(prompt, question)


def test_observed_model_identity_is_recorded_without_secrets(tmp_path):
    from memory_bench_platform.answer_prompts import finalize_prompt_models
    (tmp_path / "prompt_manifest.json").write_text('{"models": {}}')
    artifacts = tmp_path / "artifacts/step-stdout"
    artifacts.mkdir(parents=True)
    (artifacts / "q1-agent-answer.json").write_text(json.dumps({
        "raw": {"meta": {"agentMeta": {"provider": "test", "model": "actual-model", "api_key": "DO_NOT_COPY"}}}
    }))
    finalize_prompt_models(tmp_path)
    result = (tmp_path / "prompt_manifest.json").read_text()
    assert "actual-model" in result and "DO_NOT_COPY" not in result

@pytest.mark.parametrize("mode", ["backend_direct", "agent_plugin"])
def test_locomo_date_reaches_answer_and_judge(tmp_path, mode):
    from skills.benchmarks.locomo.scripts.build_scenario import build_scenario
    from memory_bench_platform.benchmark_scenario import BenchmarkScenario
    from memory_bench_platform.judges import _judge_prompt
    from memory_bench_platform.protocol import JudgeInput
    data = [{"sample_id": "s", "conversation": {
        "session_1": [{"speaker": "A", "text": "Ten years ago."}],
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_2": [{"speaker": "A", "text": "Hello."}],
        "session_2_date_time": "10:37 am on 27 June, 2023",
        "session_3": [], "session_3_date_time": "2024-01-01"
    }, "qa": [{"question": "How long ago?", "answer": "10 years", "category": 2}]}]
    path = tmp_path / "data.json"
    path.write_text(json.dumps(data))
    scenario = BenchmarkScenario.model_validate(build_scenario(path))
    root, manifest = config("locomo")
    plan = compose_run_plan(scenario, _binding(mode), answer_prompt=load_answer_prompt(root, manifest))
    case = next(c for c in plan["cases"] if c["reference"].get("question_id"))
    reference = case["reference"]
    assert reference["question_date"] == "2023-06-27"
    assert reference["question_date_source"] == "last_nonempty_session"
    answer = next(s for s in plan["steps"] if s["step_id"] == reference["expected_step_id"])
    assert "Question date: 2023-06-27" in answer["inputs"]["system_prompt"]
    template = (root / manifest.judging["prompt_template"]).read_text()
    judge = _judge_prompt({"prompt_template_text": template},
                         JudgeInput(case_id=case["case_id"], reference=reference, step_results=[]), "ten years")
    assert "Question date: 2023-06-27" in judge
    assert "10 years" in judge
    del reference["question_date"]
    with pytest.raises(ValueError, match="question_date"):
        _judge_prompt({"prompt_template_text": template},
                      JudgeInput(case_id=case["case_id"], reference=reference, step_results=[]), "ten years")


@pytest.mark.parametrize("value", [None, "", "bad", "1:00 pm on 31 February, 2023"])
def test_locomo_rejects_invalid_latest_date(value):
    from skills.benchmarks.locomo.scripts.build_scenario import _question_date
    sample = {"conversation": {
        "session_1": [{"text": "Earlier"}], "session_1_date_time": "2023-01-01",
        "session_2": [{"text": "Later"}], "session_2_date_time": value}}
    with pytest.raises(ValueError, match="invalid date"):
        _question_date(sample)
