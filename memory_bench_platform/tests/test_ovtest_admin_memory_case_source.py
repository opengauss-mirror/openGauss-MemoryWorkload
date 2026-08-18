import json
import subprocess
import sys
from pathlib import Path


def test_ovtest_admin_memory_case_source_emits_bash_workflow():
    script = Path("skills/benchmarks/ovtest-admin-memory/scripts/build_tasks.py")
    proc = subprocess.run([sys.executable, str(script)], text=True, capture_output=True, check=True)
    payload = json.loads(proc.stdout)
    assert payload["source_kind"] == "native_workflow"
    assert payload["cases"][0]["reference"]["expected_step_id"] == "find-memory"
    operator_kinds = [step["operator_kind"] for step in payload["steps"]]
    assert operator_kinds.count("bash") >= 3
    assert "wait" in operator_kinds
