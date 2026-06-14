import json
import subprocess
import sys
from pathlib import Path


def test_ovtest_memory_case_source_emits_native_workflow_shape():
    script = Path("skills/benchmarks/ovtest-memory/scripts/build_tasks.py")
    proc = subprocess.run([sys.executable, str(script)], text=True, capture_output=True, check=True)
    payload = json.loads(proc.stdout)
    assert payload["source_kind"] == "native_workflow"
    assert payload["cases"]
    assert payload["steps"]
    operator_kinds = {step["operator_kind"] for step in payload["steps"]}
    assert {"bash", "wait"} <= operator_kinds
