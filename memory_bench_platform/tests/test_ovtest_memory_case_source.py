import json
import subprocess
import sys
from pathlib import Path


def test_ovtest_memory_case_source_emits_native_workflow_shape():
    script = Path("skills/benchmarks/ovtest-memory/scripts/build_tasks.py")
    proc = subprocess.run([sys.executable, str(script)], text=True, capture_output=True, check=True)
    payload = json.loads(proc.stdout)
    assert payload["source_kind"] == "native_workflow"
    case = payload["cases"][0]
    assert case["reference"]["expected_step_id"] == "agent-answer"

    steps = payload["steps"]
    assert [step["step_id"] for step in steps] == [
        "memory-ingest",
        "memory-flush",
        "poll-ingest",
        "memory-recall",
        "agent-answer",
    ]
    assert [step["operator_kind"] for step in steps] == ["memory", "memory", "poll", "memory", "agent"]
    assert steps[0]["inputs"]["action"] == "ingest"
    assert steps[0]["retry_limit"] == 0
    assert steps[1]["inputs"]["action"] == "flush"
    assert steps[1]["depends_on"] == ["memory-ingest"]
    assert steps[2]["depends_on"] == ["memory-flush"]
    assert steps[2]["inputs"]["probe"]["inputs"]["operation"] == {
        "$ref": "steps.memory-flush.output.operation"
    }
    assert steps[3]["depends_on"] == ["poll-ingest"]
    assert steps[3]["inputs"]["action"] == "recall"
    assert steps[4]["depends_on"] == ["memory-recall"]
    assert steps[4]["inputs"]["question"] == {
        "$template": "Use this recalled evidence to answer the preference question: {{ steps.memory-recall.output.evidence_text }}"
    }
    assert payload["execution_spec"]["case_mode"] == "single_path"
