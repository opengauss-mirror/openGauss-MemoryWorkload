import json
import os
import subprocess
import sys
from pathlib import Path


def test_ovtest_health_case_source_emits_http_workflow(monkeypatch):
    monkeypatch.setenv("OVTEST_HEALTH_URL", "http://127.0.0.1:1933/health")
    script = Path("skills/benchmarks/ovtest-health/scripts/build_tasks.py")
    proc = subprocess.run([sys.executable, str(script)], text=True, capture_output=True, check=True)
    payload = json.loads(proc.stdout)
    assert payload["source_kind"] == "native_workflow"
    assert payload["steps"][0]["operator_kind"] == "http"
    assert payload["steps"][0]["inputs"]["url"] == "http://127.0.0.1:1933/health"
