import pytest

from memory_bench_platform.memory_probe import probe


def fake_backend(*, leak=False, fail_flush=False, pending=False):
    requests = []
    def invoke(request):
        requests.append(request)
        output = {}
        operation = {"session_id": "test-session", "scope_id": request.inputs["scope_id"]}
        state = "completed"
        status = "ok"
        if request.action == "ingest":
            output = {"session_id": "test-session"}
            state = "accepted"
        elif request.action == "flush":
            assert request.inputs["session_id"] == "test-session"
            if fail_flush:
                state, status = "failed", "failed"
            operation["compact_completed"] = True
        elif request.action == "status":
            assert request.inputs["operation"]["compact_completed"]
            state = "accepted" if pending else "completed"
        elif request.action == "recall":
            empty = request.inputs["scope_id"].endswith(":empty")
            output = {"evidence_text": "Hangzhou" if leak or not empty else ""}
        return {"status": status, "state": state, "operation": operation, "output": output}
    return invoke, requests


def test_agent_free_lifecycle_and_isolation(tmp_path):
    invoke, requests = fake_backend()
    report = probe("ogmemory", tmp_path / "probe", invoke=invoke)
    assert report["status"] == "passed"
    assert [r.action for r in requests] == ["ingest", "flush", "status", "recall", "recall"]
    assert all(r.runtime_context.agent_id == "none" for r in requests)
    assert requests[-1].inputs["scope_id"] != requests[-2].inputs["scope_id"]
    assert (tmp_path / "probe/probe.json").exists()


@pytest.mark.parametrize("options", [{"leak": True}, {"fail_flush": True}, {"pending": True}])
def test_failures_are_recorded_without_write_retries(tmp_path, options):
    invoke, requests = fake_backend(**options)
    report = probe("ogmemory", tmp_path / "probe", invoke=invoke, ready_timeout=0.01)
    assert report["status"] == "failed"
    assert sum(r.action == "ingest" for r in requests) == 1
    assert sum(r.action == "flush" for r in requests) == 1
    if options.get("fail_flush") or options.get("pending"):
        assert not any(r.action == "recall" for r in requests)
    assert (tmp_path / "probe/probe.json").exists()


def test_refuses_to_overwrite_probe_records(tmp_path):
    invoke, requests = fake_backend()
    with pytest.raises(FileExistsError):
        probe("ogmemory", tmp_path, invoke=invoke)
    assert not requests
