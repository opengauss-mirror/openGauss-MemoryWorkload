import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mem0_runner", ROOT / "skills/memories/mem0/scripts/run_operation.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def invoke(action, inputs, response, env=None):
    calls = []
    def open_(req, timeout):
        calls.append((req, json.loads(req.data), timeout))
        return io.BytesIO(json.dumps(response).encode())
    result = runner.run_operation({"action": action, "task_id": "one", "inputs": {"scope_id": "run:a", **inputs}}, environ=env or {}, urlopen=open_)
    return result, calls


def test_ingest_preserves_history_and_completes_without_flush():
    r,c = invoke("ingest", {"content": "Alice: I moved to Paris.", "occurred_at": "2023-05-08"}, {"results": []})
    assert r["state"] == "completed" and r["operation"]["session_id"]
    body = c[0][1]
    assert body["infer"] is True
    assert "Alice: I moved to Paris." in body["messages"][0]["content"]
    assert "2023-05-08" in body["messages"][0]["content"]
    assert body["metadata"]["occurred_at"] == "2023-05-08"
    assert "timestamp" not in body and "run_id" not in body


@pytest.mark.parametrize("profile,key", [("server", "top_k"), ("benchmark", "limit")])
def test_recall_limit_scope_and_auth(profile, key):
    r,c = invoke("recall", {"query": "Where?", "node_limit": 7}, {"results": [{"id": "m", "memory": "Paris"}]}, {"MEM0_HTTP_PROFILE": profile, "MEM0_API_KEY": "secret"})
    req,body,_ = c[0]
    assert req.full_url.endswith("/search") and req.get_header("X-api-key") == "secret"
    assert body[key] == 7 and body["filters"]["user_id"] == r["operation"]["user_id"]
    assert r["output"]["evidence_text"] == "Paris"
    other,_ = invoke("recall", {"query": "Where?", "scope_id": "run:b"}, {"results": []})
    assert other["operation"]["user_id"] != r["operation"]["user_id"]
    assert other["output"]["evidence_text"] == ""


@pytest.mark.parametrize("response", [{}, {"event_id": "async", "results": []}, {"error": "failed", "results": []}, {"results": [{"status": "PENDING"}]}, {"results": ["bad"]}, {"results": [{"id": "x"}]}, {"results": [{"id":"x", "memory":"fact", "user_id":"other"}]}])
def test_invalid_recall_is_failure(response):
    r,_ = invoke("recall", {"query": "Where?"}, response)
    assert r["state"] == "failed"


def test_transport_failure_has_no_write_retry_and_redacts():
    calls=[]
    def broken(*a, **kw):
        calls.append(1)
        raise RuntimeError("secret-token")
    r=runner.run_operation({"action":"ingest", "task_id":"x", "inputs":{"scope_id":"s", "content":"history"}}, environ={"MEM0_API_KEY":"secret-token"}, urlopen=broken)
    assert len(calls)==1 and "secret-token" not in r["error"]["message"]


def test_plan_uses_sync_ingest_with_sufficient_deadline():
    from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding
    from memory_bench_platform.composer import compose_run_plan
    from memory_bench_platform.integration import get_memory_manifest
    scenario=BenchmarkScenario.model_validate(json.loads((ROOT/"tests/golden/multi_checkpoint_scenario.json").read_text()))
    caps=get_memory_manifest("mem0").capabilities
    plan=compose_run_plan(scenario, RunBinding(benchmark_id=scenario.benchmark_id, agent_id="openclaw", memory_id="mem0", memory_integration="backend_direct", run_id="test"), {"memory":caps})
    ingests=[s for s in plan["steps"] if s["inputs"].get("action")=="ingest"]
    assert ingests and all(s["timeout_seconds"]==900 for s in ingests)
    assert not any(s["inputs"].get("action") in ("flush", "status") for s in plan["steps"])


def test_missing_write_acknowledgement_is_not_success():
    result, _ = invoke("ingest", {"content": "history"}, {"results": [{}]})
    assert result["state"] == "failed"


def test_unknown_profile_fails_before_http():
    result, calls = invoke("recall", {"query": "Where?"}, {"results": []}, {"MEM0_HTTP_PROFILE": "cloud"})
    assert result["state"] == "failed" and calls == []
