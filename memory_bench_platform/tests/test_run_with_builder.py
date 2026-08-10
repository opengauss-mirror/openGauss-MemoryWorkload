import json
from pathlib import Path

from memory_bench_platform.cli import _extract_case_result_rows, main
from memory_bench_platform.protocol import ArtifactRecord, CaseRecord, JudgeResult, StepResultRecord


class _FakeMonitor:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    def setup_writers(self):
        return None

    def start_background_sampling(self):
        return None

    def stop_background_sampling(self):
        return None

    def capture_once(self):
        return {"summary_util_idle": 100.0}


def test_run_passes_resolved_memory_id_and_runtime_context(tmp_path: Path, monkeypatch):
    captured = {}
    version_selection = {
        "benchmark": {"selection_mode": "latest_official_release_tag"},
        "agent": {"selection_mode": "latest_official_release_tag"},
    }
    case_payload = {
        "source_kind": "native_workflow",
        "cases": [
            {
                "case_id": "case-setup",
                "title": "setup",
                "goal": "prepare memory",
                "capability": "memory/ingest",
                "judge_mode": "none",
            },
            {
                "case_id": "case-1",
                "title": "case",
                "goal": "answer",
                "capability": "memory/question-answering",
                "depends_on_cases": ["case-setup"],
                "reference": {"expected_answer": "ok"},
            }
        ],
        "steps": [
            {
                "step_id": "case-1-agent",
                "case_id": "case-1",
                "name": "answer",
                "operator_kind": "agent",
                "inputs": {"question": "answer"},
            }
        ],
        "execution_spec": {"case_mode": "single_path"},
    }

    def fake_execute_cases(**kwargs):
        captured.update(kwargs)
        return {
            "step_results": [
                StepResultRecord(
                    step_result_id="case-1-agent-1",
                    step_id="case-1-agent",
                    attempt=1,
                    status="passed",
                    structured_output={
                        "output": {"text": "ok"},
                        "agent_answer": "ok",
                        "text_output": "ok",
                    },
                )
            ],
            "traces": [],
            "judge_results": [
                JudgeResult(
                    judge_id="case-1-builtin",
                    run_id="run-1",
                    case_id="case-1",
                    passed=True,
                    score=1.0,
                    label="exact-match",
                )
            ],
            "metrics": [],
            "artifacts": [
                ArtifactRecord(
                    artifact_id="case-1-agent-output",
                    run_id="run-1",
                    case_id="case-1",
                    step_id="case-1-agent",
                    kind="agent_output",
                    path="artifacts/agent-output.json",
                )
            ],
        }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("memory_bench_platform.cli._build_version_selection", lambda *args, **kwargs: version_selection)
    monkeypatch.setattr("memory_bench_platform.cli.build_cases_from_source", lambda *args, **kwargs: case_payload)
    monkeypatch.setattr("memory_bench_platform.cli.execute_cases", fake_execute_cases)
    monkeypatch.setattr("memory_bench_platform.cli.ResourceMonitor", _FakeMonitor)
    monkeypatch.setattr("memory_bench_platform.cli.analyze_run", lambda run_dir: {"run_dir": str(run_dir)})

    main(
        [
            "run",
            "--benchmark",
            "ovtest-memory",
            "--agent",
            "openclaw",
            "--run-id",
            "run-1",
        ]
    )

    assert captured["memory_id"] == "openviking"
    context = captured["runtime_context"]
    assert context.run_id == "run-1"
    assert context.benchmark_id == "ovtest-memory"
    assert context.agent_id == "openclaw"
    assert context.memory_id == "openviking"
    assert context.run_contract["selection"]["memory_id"] == "openviking"
    assert context.version_selection == version_selection
    assert context.run_dir == str(captured["run_dir"])

    run_dir = tmp_path / "runs" / "run-1"
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "reports" / "summary.json").read_text(encoding="utf-8"))
    artifacts = json.loads((run_dir / "records" / "artifacts.json").read_text(encoding="utf-8"))
    assert run_record["memory_backend"] == "openviking"
    assert run_record["source_kind"] == "native_workflow"
    assert summary["case_total"] == 1
    assert summary["case_passed"] == 1
    assert artifacts[0]["kind"] == "agent_output"


def test_case_results_use_expected_step_id_without_case_prefix():
    case = CaseRecord(
        case_id="case-1",
        run_id="run-1",
        title="case",
        goal="answer",
        capability="memory/question-answering",
        reference={"expected_answer": "expected", "expected_step_id": "agent-answer"},
    )
    step_result = StepResultRecord(
        step_result_id="agent-answer-1",
        step_id="agent-answer",
        attempt=1,
        status="passed",
        structured_output={"agent_answer": "expected", "output": {"text": "expected"}},
    )
    judge = JudgeResult(
        judge_id="case-1-builtin",
        run_id="run-1",
        case_id="case-1",
        passed=True,
        score=1.0,
    )

    rows = _extract_case_result_rows([case], [judge], [step_result])

    assert rows[0]["response"] == "expected"


def test_case_results_extract_retrieval_evidence():
    case = CaseRecord(
        case_id="case-retrieval",
        run_id="run-1",
        title="case",
        goal="retrieve",
        capability="memory/retrieval",
        reference={
            "expected_answer": "tea",
            "expected_step_id": "memory-recall",
            "evaluation_extractor": "evidence_text",
        },
    )
    step_result = StepResultRecord(
        step_result_id="memory-recall-1",
        step_id="memory-recall",
        attempt=1,
        status="passed",
        structured_output={"output": {"evidence_text": "The user prefers tea."}},
    )
    judge = JudgeResult(
        judge_id="case-retrieval-llm",
        run_id="run-1",
        case_id="case-retrieval",
        passed=True,
        score=1.0,
    )
    rows = _extract_case_result_rows([case], [judge], [step_result])
    assert rows[0]["response"] == "The user prefers tea."
