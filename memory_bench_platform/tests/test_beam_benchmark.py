import json
from pathlib import Path
import runpy

import pytest

from memory_bench_platform.benchmark_scenario import BenchmarkScenario


def test_beam_preserves_turns_and_separates_rubric(tmp_path):
    script = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam/scripts/build_scenario.py"
    build = runpy.run_path(str(script))["build_scenario"]
    messages = [
        {"role": "user", "content": "a" * 10000, "time_anchor": "March-15-2024"},
        {"role": "assistant", "content": "b" * 3000},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "answer"},
    ]
    row = {"conversation_id": "sample", "chat": [{"turns": [messages[:2], messages[2:]]}],
           "probing_questions": json.dumps({"information_extraction": [
               {"question": "What happened?", "rubric": ["judge-only criterion"]}
           ]})}
    data = tmp_path / "beam.jsonl"
    data.write_text(json.dumps(row), encoding="utf-8")
    first, second, checkpoint = BenchmarkScenario.model_validate(build(data)).samples[0].timeline
    assert first.payload["messages"] == [{k: m[k] for k in ("role", "content")} for m in messages[:2]]
    assert second.payload["messages"] == messages[2:]
    assert "March-15-2024" in first.payload["content"]
    assert "judge-only criterion" not in first.payload["content"] + second.payload["content"]
    assert checkpoint.evaluation.questions[0].reference == ["judge-only criterion"]


@pytest.mark.parametrize("structure", ["raw", "turns", "plans"])
@pytest.mark.parametrize("questions_format", ["object", "json", "literal"])
def test_beam_official_structures_preserve_order_and_time(tmp_path, structure, questions_format):
    script = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam/scripts/build_scenario.py"
    build = runpy.run_path(str(script))["build_scenario"]
    first = [{"role": "user", "content": "first", "time_anchor": "2024-03-16"},
             {"role": "assistant", "content": "second"}]
    last = [{"role": "user", "content": "third"},
            {"role": "assistant", "content": "fourth"}]
    if structure == "raw":
        chat = [first, last]
    else:
        batches = [{"time_anchor": "2024-03-15", "turns": [first, last]}]
        chat = batches if structure == "turns" else [{"plan-1": batches}]
    groups = {"temporal_reasoning": [{"question": "When?", "rubric": ["criterion"]}]}
    if questions_format == "json":
        groups = json.dumps(groups)
    elif questions_format == "literal":
        groups = repr(groups)
    data = tmp_path / "beam.json"
    data.write_text(json.dumps({"conversation_id": "official", "chat": chat,
                               "probing_questions": groups}))
    scenario = BenchmarkScenario.model_validate(build(data))
    history = scenario.samples[0].timeline[:-1]
    messages = [m for event in history for m in event.payload["messages"]]
    assert [m["content"] for m in messages] == ["first", "second", "third", "fourth"]
    content = "\n".join(event.payload["content"] for event in history)
    assert "[2024-03-16] user: first" in content
    if structure != "raw":
        assert "[2024-03-15] user: third" in content
    assert scenario.samples[0].timeline[-1].evaluation.questions[0].reference == ["criterion"]


def test_beam_plan_groups_keep_chronological_order(tmp_path):
    script = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam/scripts/build_scenario.py"
    build = runpy.run_path(str(script))["build_scenario"]
    chat = [{f"plan-{index}": [{"time_anchor": date, "turns": [
        [{"role": "user", "content": content}]]}]}
        for index, date, content in [(1, "2024-01-01", "tea"), (2, "2024-02-01", "coffee")]]
    data = tmp_path / "plans.json"
    data.write_text(json.dumps({"conversation_id": "plans", "chat": chat,
        "probing_questions": {"knowledge_update": [{"question": "Latest?", "rubric": ["coffee"]}]}}))
    events = build(data)["samples"][0]["timeline"]
    assert events[0]["payload"]["content"] == "[2024-01-01] user: tea"
    assert events[1]["payload"]["content"] == "[2024-02-01] user: coffee"


@pytest.mark.parametrize(("anchor", "expected"), [
    ("March-15-2024", "2024-03-15T00:00:00"),
    ("March 15, 2024", "2024-03-15T00:00:00"),
    ("March 15, 2024, 09:30", "2024-03-15T09:30:00"),
    ("2024-03-15T09:30:00+02:00", "2024-03-15T09:30:00+02:00"),
    ("2024-03-15T09:30:00Z", "2024-03-15T09:30:00+00:00"),
    ("sometime next spring", None),
    (None, None),
])
def test_beam_timestamp_normalization(anchor, expected):
    script = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam/scripts/build_scenario.py"
    normalize = runpy.run_path(str(script))["_historical_timestamp"]
    assert normalize(anchor) == expected


@pytest.mark.parametrize("mode", ["backend_direct", "agent_plugin"])
def test_beam_dates_reach_ingest_and_native_observation_date(tmp_path, monkeypatch, mode):
    import io
    from types import SimpleNamespace
    from memory_bench_platform.benchmark_scenario import RunBinding
    from memory_bench_platform.composer import compose_run_plan

    root = Path(__file__).resolve().parents[1]
    build = runpy.run_path(str(root / "skills/benchmarks/beam/scripts/build_scenario.py"))["build_scenario"]
    data = tmp_path / "dates.json"
    data.write_text(json.dumps({"conversation_id": "dates", "chat": [{
        "time_anchor": "March-15-2024", "turns": [
            [{"role": "user", "content": "first"}, {"role": "assistant", "content": "noted"}],
            [{"role": "user", "content": "second", "time_anchor": "2024-03-16"},
             {"role": "assistant", "content": "noted", "time_anchor": "2024-03-17"}]]}],
        "probing_questions": {"temporal_reasoning": [{"question": "When?", "rubric": ["March"]}]}}))
    scenario = BenchmarkScenario.model_validate(build(data))
    history = scenario.samples[0].timeline[:-1]
    expected = ["2024-03-15T00:00:00", "2024-03-16T00:00:00"]
    assert [event.timestamp for event in history] == expected
    assert [len(event.payload["messages"]) for event in history] == [2, 2]
    assert "[March-15-2024]" in history[0].payload["content"]
    assert "[2024-03-17] assistant: noted" in history[1].payload["content"]

    binding = RunBinding(benchmark_id="beam", agent_id="openclaw", memory_id="jiuwenmemory",
        memory_plugin_id="openclaw-jiuwenmemory" if mode == "agent_plugin" else None,
        memory_integration=mode, run_id="dates")
    plan = compose_run_plan(scenario, binding)
    stamps = []
    def collect(value):
        if isinstance(value, dict):
            if value.get("purpose") == "memory_ingest" or value.get("action") == "ingest":
                stamps.append(value["occurred_at"])
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(plan)
    assert stamps == expected

    adapter = runpy.run_path(str(root / "skills/memories/jiuwenmemory/scripts/run_operation.py"))
    captured = []
    def open_request(request, timeout):
        captured.append(json.loads(request.data))
        return io.BytesIO(b'[{"id": "native-memory"}]')
    monkeypatch.setitem(adapter["run_operation"].__globals__, "build_opener",
                        lambda *args: SimpleNamespace(open=open_request))
    for index, stamp in enumerate(stamps):
        result = adapter["run_operation"]({"action": "ingest", "inputs": {
            "scope_id": "dates", "event_id": str(index),
            "content": history[index].payload["content"], "occurred_at": stamp}})
        assert result["status"] == "ok"
    assert [row["system_metadata"]["observation_date"] for row in captured] == ["2024-03-15", "2024-03-16"]


@pytest.mark.parametrize("category", [
    "abstention", "contradiction_resolution", "information_extraction", "instruction_following",
    "knowledge_update", "multi_session_reasoning", "preference_following", "summarization",
    "temporal_reasoning",
])
def test_official_beam_judges_each_criterion(category):
    root = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam/evaluation"
    evaluate = runpy.run_path(str(root / "official.py"))["evaluate"]
    prompts = []
    def invoke(prompt, system):
        assert system is None
        prompts.append(prompt)
        return json.dumps({"score": 1 if len(prompts) == 1 else 0.5, "reason": "checked"})
    result = evaluate({"protocol_version": "judge-handler/1", "reference": {"expected_answer": ["criterion one", "criterion two"], "category": category}, "answer": "answer", "prompt_template": (root / "judge.txt").read_text()}, invoke)
    score, rationale = result["score"], result["rationale"]
    assert score == 0.75
    assert len(prompts) == 2
    template = (root / "judge.txt").read_text()
    assert prompts == [template.replace("<rubric_item>", criterion).replace("<llm_response>", "answer")
                       for criterion in ["criterion one", "criterion two"]]
    assert json.loads(rationale)["llm_judge_score"] == 0.75


@pytest.mark.parametrize(("answer", "expected"), [("a\nb", 1), ("b\na", 0)])
def test_official_beam_event_ordering(answer, expected):
    root = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam/evaluation"
    evaluate = runpy.run_path(str(root / "official.py"))["evaluate"]
    def invoke(prompt, system):
        if system is None:
            return '{"score": 1, "reason": "contains fact"}'
        first = prompt.split("First snippet: ", 1)[1].split("\n", 1)[0].strip()
        second = prompt.split("Second snippet: ", 1)[1].strip()
        return "YES" if first == second else "NO"
    result = evaluate({"protocol_version": "judge-handler/1", "reference": {"expected_answer": ["a", "b"], "category": "event_ordering"}, "answer": answer, "prompt_template": (root / "judge.txt").read_text()}, invoke)
    score, rationale = result["score"], result["rationale"]
    metrics = json.loads(rationale)
    assert score == metrics["tau_norm"] == expected
    assert metrics["f1"] == 1
    assert metrics["llm_judge_score"] == 1


def test_official_beam_http_dispatch_and_retry_count(monkeypatch):
    from memory_bench_platform.judges import run_llm_judge
    from test_llm_judge import _input, _FakeResponse
    for key, value in [("KEY", "test"), ("URL", "http://judge.invalid/v1"), ("MODEL", "test")]:
        monkeypatch.setenv("BEAM_TEST_" + key, value)
    item = _input()
    item.reference.update(expected_answer=["criterion one", "criterion two"], category="information_extraction")
    calls = []
    def opener(request, timeout):
        body = json.loads(request.data)
        calls.append(body)
        assert len(body["messages"]) == 1
        assert body["messages"][0]["content"].startswith("configured criterion")
        return _FakeResponse({"choices": [{"message": {"content": '{"score": 0.5, "reason": "partial"}'}}]})
    result = run_llm_judge("test", item, runtime_config={
        "profile": "beam_official@1",
        "handler_path": str(Path(__file__).resolve().parents[1] / "skills/benchmarks/beam/evaluation/official.py"),
        "handler_protocol": "judge-handler/1",
        "prompt_template_text": "configured <rubric_item> / <llm_response>",
        "env": {"api_key": "BEAM_TEST_KEY", "base_url": "BEAM_TEST_URL", "model": "BEAM_TEST_MODEL"},
    }, urlopen=opener)
    assert result.score == 0.5
    assert result.label == "partial"
    assert result.retry_count == 0
    assert len(calls) == 2


def test_official_report_averages_samples_before_categories(tmp_path):
    root = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam"
    report = runpy.run_path(str(root / "scripts/report_scores.py"))["write_reports"]
    (tmp_path / "records").mkdir()
    (tmp_path / "reports").mkdir()
    cases, results = [], []
    for i, (sample, score) in enumerate([("a", 0), ("b", 1), ("b", 1), ("b", 1)]):
        case = str(i)
        cases.append({"case_id": case, "reference": {
            "evaluation_target": "qa_answer", "sample_id": sample, "category": "information_extraction",
            "question": "Question", "expected_answer": ["criterion"]}})
        results.append({"case_id": case, "score": score, "response": "answer",
                        "rationale": '{"profile": "beam_official@1"}'})
    (tmp_path / "records/cases.json").write_text(json.dumps(cases))
    path = tmp_path / "reports/case_results.json"
    path.write_text(json.dumps(results))
    summary = report(tmp_path)
    assert summary["primary_metric"] == "mean_category_score"
    assert summary["metrics"][0]["value"] == 0.5
    assert all(Path(item["path"]).exists() for item in summary["artifacts"])
    assert summary["mean_score"] == 0.75
    assert summary["official_category_scores"]["information_extraction"] == 0.5
    results[0]["rationale"] = "Legacy rubric Judge"
    path.write_text(json.dumps(results))
    with pytest.raises(ValueError, match="rejudge saved answers"):
        report(tmp_path)


def test_official_ordering_ties_match_known_kendall_value():
    import math
    root = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam/evaluation"
    ordering = runpy.run_path(str(root / "official.py"))["_ordering"]
    def invoke(prompt, system):
        first = prompt.split("First snippet: ", 1)[1].split("\n", 1)[0].strip()
        second = prompt.split("Second snippet: ", 1)[1].strip()
        return "YES" if first == second else "NO"
    metrics = ordering(["a", "b", "c"], ["a"], invoke)
    assert metrics["tau_norm"] == pytest.approx((2 / math.sqrt(6) + 1) / 2)
    with pytest.raises(ValueError, match="undefined Kendall tau"):
        ordering(["a"], ["a"], invoke)


def test_beam_message_time_does_not_override_batch_default(tmp_path):
    root = Path(__file__).resolve().parents[1]
    build = runpy.run_path(str(root / "skills/benchmarks/beam/scripts/build_scenario.py"))["build_scenario"]
    data = tmp_path / "inheritance.json"
    data.write_text(json.dumps({"conversation_id": "dates", "chat": [{
        "time_anchor": "2024-01-01", "turns": [
            [{"role": "user", "content": "first", "time_anchor": "2024-02-01"},
             {"role": "assistant", "content": "first reply"}],
            [{"role": "user", "content": "second"},
             {"role": "assistant", "content": "second reply"}]]}],
        "probing_questions": {"temporal_reasoning": [{"question": "When?", "rubric": ["date"]}]}}))
    history = build(data)["samples"][0]["timeline"][:-1]
    assert [event["timestamp"] for event in history] == [
        "2024-02-01T00:00:00", "2024-01-01T00:00:00"]
    assert [len(event["payload"]["messages"]) for event in history] == [2, 2]
    assert "[2024-02-01] user: first" in history[0]["payload"]["content"]
    assert "[2024-01-01] assistant: first reply" in history[0]["payload"]["content"]
    assert "[2024-01-01] user: second" in history[1]["payload"]["content"]

def test_beam_archives_input_digest(tmp_path, monkeypatch):
    import hashlib
    root = Path(__file__).resolve().parents[1]
    build = runpy.run_path(str(root / "skills/benchmarks/beam/scripts/build_scenario.py"))["build_scenario"]
    row = {"conversation_id": "digest", "chat": [{"role": "user", "content": "tea"}],
           "probing_questions": {"information_extraction": [{"question": "Drink?", "rubric": ["tea"]}]}}
    path = tmp_path / "same-name.json"
    monkeypatch.setenv("BEAM_DATA_REVISION", "release-example")
    path.write_text(json.dumps(row))
    first = build(path)["metadata"]["dataset"]
    assert first["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first["revision"] == "release-example"
    row["chat"][0]["content"] = "coffee"
    path.write_text(json.dumps(row))
    assert build(path)["metadata"]["dataset"]["sha256"] != first["sha256"]


def test_skill_declares_judge_handler_and_template(tmp_path, monkeypatch):
    import copy
    from memory_bench_platform import integration
    bundle = copy.deepcopy(integration.resolve_run_skill_bundle("beam", "openclaw", "ogmemory"))
    skill = tmp_path / "benchmarks/beam"
    skill.mkdir(parents=True)
    (skill / "custom.txt").write_text("chosen <rubric_item> / <llm_response>")
    (skill / "custom.py").write_text("PROTOCOL_VERSION = 'judge-handler/1'\ndef evaluate(request, invoke): return {}\ndef aggregate(rows): return {}")
    bundle.benchmark.judging.update(handler="custom.py", handler_protocol="judge-handler/1", prompt_template="custom.txt")
    monkeypatch.setattr(integration, "resolve_run_skill_bundle", lambda *args, **kwargs: bundle)
    contract = integration.build_run_contract("beam", "openclaw", "ogmemory", skills_root=tmp_path)
    assert contract["judge_runtime"]["handler_path"] == str(skill / "custom.py")
    assert contract["judge_runtime"]["prompt_template_text"] == "chosen <rubric_item> / <llm_response>"
    bundle.benchmark.judging["handler"] = "../escape.py"
    (tmp_path / "benchmarks/escape.py").write_text("raise RuntimeError('must not execute')")
    with pytest.raises(ValueError, match="Invalid benchmark judge handler"):
        integration.build_run_contract("beam", "openclaw", "ogmemory", skills_root=tmp_path)


@pytest.mark.parametrize("response", [None, "", "   "])
def test_beam_report_keeps_missing_answer_zero_and_exports_other_answers(tmp_path, response):
    root = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam"
    report = runpy.run_path(str(root / "scripts/report_scores.py"))["write_reports"]
    (tmp_path / "records").mkdir()
    (tmp_path / "reports").mkdir()
    cases = [{"case_id": case, "reference": {
        "evaluation_target": "qa_answer", "sample_id": "sample", "category": "information_extraction",
        "question": "Drink?", "expected_answer": ["tea"]}} for case in ["normal", "empty"]]
    results = [
        {"case_id": "normal", "score": 1, "response": "tea",
         "rationale": '{"profile": "beam_official@1"}'},
        {"case_id": "empty", "score": 0.0, "label": "missing-answer", "response": response,
         "rationale": "No agent answer extracted from step results."},
    ]
    (tmp_path / "records/cases.json").write_text(json.dumps(cases))
    path = tmp_path / "reports/case_results.json"
    path.write_text(json.dumps(results))
    summary = report(tmp_path)
    assert summary["graded"] == 2
    assert summary["mean_score"] == 0.5
    assert summary["official_category_scores"]["information_extraction"] == 0.5
    exported = json.loads((tmp_path / "reports/beam_official_inputs/sample-1-answers.json").read_text())
    assert len(exported["information_extraction"]) == 2
    assert exported["information_extraction"][0]["llm_response"] == "tea"
    assert exported["information_extraction"][1]["llm_response"] == ""

    for changes in [
        {"label": "wrong"},
        {"response": "nonempty answer"},
        {"score": 0.5},
        {"score": False},
    ]:
        altered = {**results[1], **changes}
        path.write_text(json.dumps([results[0], altered]))
        with pytest.raises(ValueError, match="rejudge saved answers"):
            report(tmp_path)


@pytest.mark.parametrize("source", [
    "PROTOCOL_VERSION='unknown'\ndef evaluate(request, invoke): pass\ndef aggregate(rows): pass",
    "PROTOCOL_VERSION='judge-handler/1'\ndef aggregate(rows): pass",
    "PROTOCOL_VERSION='judge-handler/1'\ndef evaluate(): pass\ndef aggregate(rows): pass",
])
def test_judge_protocol_rejects_bad_entry_before_execution(tmp_path, source):
    from memory_bench_platform.judge_handlers import load_handler
    path = tmp_path / "handler.py"
    path.write_text(source)
    with pytest.raises(ValueError):
        load_handler(path, "judge-handler/1")


@pytest.mark.parametrize("result", [
    (0.5, "tuple"), {"protocol_version": "other", "score": 0.5, "rationale": "bad"},
    {"protocol_version": "judge-handler/1", "score": float("nan"), "rationale": "bad"},
])
def test_judge_protocol_rejects_malformed_responses(tmp_path, result):
    from memory_bench_platform.judge_handlers import evaluate_handler
    path = tmp_path / "handler.py"
    path.write_text("PROTOCOL_VERSION='judge-handler/1'\ndef aggregate(rows): return {}\n"
                    "def evaluate(request, invoke):\n    return " + repr(result).replace("nan", "float('nan')"))
    with pytest.raises(ValueError):
        evaluate_handler({"handler_path": str(path), "handler_protocol": "judge-handler/1",
                          "prompt_template_text": "test"}, {}, "answer", lambda *args: "")


def test_beam_partial_score_has_one_primary_source(tmp_path):
    from types import SimpleNamespace
    from memory_bench_platform.judge_handlers import apply_primary_metric
    from memory_bench_platform.result_analysis import analyze_run
    from test_result_analysis import _build_minimal_run

    root = Path(__file__).resolve().parents[1]
    handler = root / "skills/benchmarks/beam/evaluation/official.py"
    case = SimpleNamespace(case_id="case", reference={"evaluation_target": "qa_answer",
                           "sample_id": "sample", "category": "information_extraction"})
    config = {"handler_path": str(handler), "handler_protocol": "judge-handler/1",
              "primary_metric": "mean_category_score"}
    summary = apply_primary_metric({"raw_benchmark_score": 0, "benchmark_score": 0}, [case],
                                    [SimpleNamespace(case_id="case", score=0.5)], config)
    assert summary["benchmark_score"] == 0.5
    assert summary["full_score_pass_rate"] == 0

    _build_minimal_run(tmp_path)
    path = tmp_path / "reports/summary.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **summary}))
    analysis = analyze_run(tmp_path)
    assert analysis["primary_metric"] == "mean_category_score"
    assert analysis["primary_score"] == 0.5
    assert analysis["overall_accuracy"] is None


def test_scorer_uses_manifest_and_archived_aggregation_handler(tmp_path, monkeypatch):
    import shutil
    from types import SimpleNamespace
    import yaml
    from memory_bench_platform import paths
    from memory_bench_platform.judge_handlers import resolve_handler_config, apply_primary_metric
    from memory_bench_platform.loader import load_benchmark_skill

    source = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam"
    skills = tmp_path / "skills"
    skill = skills / "benchmarks/beam"
    shutil.copytree(source, skill)
    monkeypatch.setattr(paths, "SKILLS_ROOT", skills)
    alternate = skill / "evaluation/alternate.py"
    alternate.write_text((skill / "evaluation/official.py").read_text() + """
original_aggregate = aggregate
def aggregate(request):
    result = original_aggregate(request)
    result["value"] = 0.25
    result["official_category_scores"] = {"information_extraction": 0.25}
    result["metrics"][0]["value"] = 0.25
    return result
""")
    manifest_path = skill / "manifest.yaml"
    raw = yaml.safe_load(manifest_path.read_text())
    raw["judging"]["handler"] = "evaluation/alternate.py"
    manifest_path.write_text(yaml.safe_dump(raw))
    manifest = load_benchmark_skill(skills, "beam")
    config = resolve_handler_config(manifest.judging, skill, manifest.evaluation["primary_metric"])

    run = tmp_path / "run"
    (run / "records").mkdir(parents=True)
    (run / "reports").mkdir()
    reference = {"evaluation_target": "qa_answer", "sample_id": "sample", "category": "information_extraction",
                 "question": "Drink?", "expected_answer": ["tea"]}
    (run / "records/cases.json").write_text(json.dumps([{"case_id": "case", "reference": reference}]))
    (run / "reports/case_results.json").write_text(json.dumps([{
        "case_id": "case", "score": 1.0, "response": "tea", "rationale": '{"profile": "beam_official@1"}'}]))
    report = runpy.run_path(str(source / "scripts/report_scores.py"))["write_reports"]
    online = apply_primary_metric({"raw_benchmark_score": 1}, [SimpleNamespace(case_id="case", reference=reference)],
                                  [SimpleNamespace(case_id="case", score=1.0)], config)
    assert report(run)["mean_category_score"] == online["benchmark_score"] == 0.25

    (run / "records/run_contract.json").write_text(json.dumps({"judge_runtime": config}))
    raw["judging"]["handler"] = "evaluation/official.py"
    manifest_path.write_text(yaml.safe_dump(raw))
    assert report(run)["mean_category_score"] == 0.25
    config["primary_metric"] = "different_metric"
    (run / "records/run_contract.json").write_text(json.dumps({"judge_runtime": config}))
    with pytest.raises(ValueError, match="primary metric differs"):
        report(run)


@pytest.mark.parametrize("with_data_path", [False, True])
def test_beam_score_run_accepts_optional_data_path(tmp_path, with_data_path):
    (tmp_path / "records").mkdir()
    (tmp_path / "reports").mkdir()
    reference = {"evaluation_target": "qa_answer", "sample_id": "sample",
                 "category": "information_extraction", "question": "Drink?", "expected_answer": ["tea"]}
    (tmp_path / "records/cases.json").write_text(json.dumps([{"case_id": "case", "reference": reference}]))
    (tmp_path / "reports/case_results.json").write_text(json.dumps([{
        "case_id": "case", "score": 1.0, "response": "tea",
        "rationale": '{"profile": "beam_official@1"}'}]))
    # A missing input path must be harmless: this scorer uses archived questions and answers.
    source = str(tmp_path / "unused-source.json") if with_data_path else None
    import subprocess
    import sys
    command = [sys.executable, "-m", "memory_bench_platform.cli", "score-run",
               "--benchmark", "beam", "--run-dir", str(tmp_path)]
    if source:
        command += ["--data-path", source]
    process = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                             capture_output=True, text=True, check=True)
    result = json.loads(process.stdout)
    assert result["primary_metric"] == "mean_category_score"
    assert result["mean_category_score"] == 1.0
    assert (tmp_path / "reports/beam_scores.json").is_file()
    assert (tmp_path / "reports/beam_official_inputs/sample-1-answers.json").is_file()


def test_archived_handler_migrates_only_with_matching_implementation(tmp_path):
    import shutil
    from memory_bench_platform.judge_handlers import resolve_handler_config, restore_handler_config, aggregate_handler
    source = Path(__file__).resolve().parents[1] / "skills/benchmarks/beam"
    first, second = tmp_path / "first/beam", tmp_path / "second/beam"
    shutil.copytree(source, first)
    config = resolve_handler_config({"handler": "evaluation/official.py",
                                     "handler_protocol": "judge-handler/1"}, first, "mean_category_score")
    shutil.copytree(first, second)
    restored = restore_handler_config(config, second)
    assert restored["handler_path"] == str(second / "evaluation/official.py")
    assert aggregate_handler(restored, [])["primary_metric"] == "mean_category_score"
    (second / "evaluation/official.py").write_text("raise RuntimeError('must not execute')")
    with pytest.raises(ValueError, match="hash does not match"):
        restore_handler_config(config, second)


@pytest.mark.parametrize("selector", ["absolute", "relative", "symlink"])
def test_archived_handler_rejects_outside_skill_before_loading(tmp_path, selector):
    from memory_bench_platform.judge_handlers import restore_handler_config
    root = tmp_path / "beam"
    root.mkdir()
    marker = tmp_path / "executed"
    outside = tmp_path / "outside.py"
    outside.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()")
    config = {"handler_path": str(outside), "handler_protocol": "judge-handler/1"}
    if selector == "relative":
        config["handler"] = "../outside.py"
    elif selector == "symlink":
        (root / "linked.py").symlink_to(outside)
        config["handler"] = "linked.py"
    with pytest.raises(ValueError, match="outside"):
        restore_handler_config(config, root)
    assert not marker.exists()


def test_legacy_archived_handler_requires_original_trusted_location(tmp_path):
    from memory_bench_platform.judge_handlers import restore_handler_config
    root = tmp_path / "beam"
    root.mkdir()
    handler = root / "judge.py"
    handler.write_text("pass")
    config = {"handler": "judge.py", "handler_path": str(handler)}
    assert restore_handler_config(config, root)["handler_path"] == str(handler)
    config["handler_path"] = "/unavailable/previous-workspace/judge.py"
    with pytest.raises(ValueError, match="cannot verify"):
        restore_handler_config(config, root)
