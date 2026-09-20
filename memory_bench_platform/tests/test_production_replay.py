from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/benchmarks/production-http-replay/scripts/run_replay.py"
)
VALIDATE_SCRIPT = SCRIPT.with_name("validate.py")


def _module():
    spec = importlib.util.spec_from_file_location("production_replay_runner", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_row(path: Path, request: dict, response: dict | None = None):
    path.write_text(
        json.dumps({"request": request, "response": response or {}}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def _valid_dataset(path: Path) -> tuple[Path, Path]:
    add = path / "openmem_add_sample.jsonl"
    search = path / "openmem_search_sample.jsonl"
    _write_row(add, {"user_id": "", "messages": []})
    _write_row(search, {"user_id": "", "query": "needle"})
    return add, search


def test_discover_dataset_requires_one_regular_file_per_operation(tmp_path: Path):
    add, search = _valid_dataset(tmp_path)
    dataset = _module().discover_dataset(tmp_path)
    assert dataset.add_path == add
    assert dataset.search_path == search


@pytest.mark.parametrize(
    "extra_name",
    ["second-add.JSONL", "contains_add_and_search.jsonl"],
)
def test_discover_dataset_rejects_ambiguous_operation_files(
    tmp_path: Path, extra_name: str
):
    _valid_dataset(tmp_path)
    _write_row(tmp_path / extra_name, {"user_id": ""})
    with pytest.raises(ValueError, match="ambiguous"):
        _module().discover_dataset(tmp_path)


def test_discover_dataset_rejects_missing_search(tmp_path: Path):
    _write_row(tmp_path / "openmem_add_sample.jsonl", {"user_id": ""})
    with pytest.raises(ValueError, match="search"):
        _module().discover_dataset(tmp_path)


def test_discover_dataset_ignores_nested_files_and_rejects_symlinks(tmp_path: Path):
    add, search = _valid_dataset(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    _write_row(nested / "another_add.jsonl", {"user_id": ""})
    (tmp_path / "linked-add.jsonl").symlink_to(add)
    dataset = _module().discover_dataset(tmp_path)
    assert dataset.add_path == add
    assert dataset.search_path == search


def test_discover_dataset_accepts_uppercase_suffix(tmp_path: Path):
    add = tmp_path / "requests-ADD.JSONL"
    search = tmp_path / "requests-SEARCH.JSONL"
    _write_row(add, {"user_id": ""})
    _write_row(search, {"user_id": ""})
    assert _module().discover_dataset(tmp_path).add_path == add


def test_iter_records_preserves_payload_and_replaces_only_empty_user(tmp_path: Path):
    path = tmp_path / "openmem_add_sample.jsonl"
    request = {
        "user_id": "",
        "messages": [{"role": "user", "content": "secret"}],
    }
    _write_row(path, request)
    record = next(_module().iter_replay_records(path, "add", "run-7"))
    assert record.request["user_id"] == "replay-run-7"
    assert record.request["messages"][0]["content"] == "secret"
    assert request["user_id"] == ""
    assert record.request_id.startswith("add-000001-")
    assert record.payload_size_bytes > 0
    assert record.top_level_fields == ("messages", "user_id")


def test_iter_records_preserves_nonempty_user_and_stable_hash(tmp_path: Path):
    path = tmp_path / "openmem_search_sample.jsonl"
    _write_row(path, {"query": "needle", "user_id": "existing"})
    first = next(_module().iter_replay_records(path, "search", "run-1"))
    second = next(_module().iter_replay_records(path, "search", "run-2"))
    assert first.request["user_id"] == "existing"
    assert first.request_id == second.request_id
    assert first.payload_sha256 == second.payload_sha256


@pytest.mark.parametrize("content", ["\nnot-secret-valid-json\n", "42\n"])
def test_iter_records_reports_line_without_echoing_bad_content(
    tmp_path: Path, content: str
):
    path = tmp_path / "openmem_search_sample.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        list(_module().iter_replay_records(path, "search", "run-7"))
    assert path.name in str(exc.value)
    assert "line 1" in str(exc.value)
    assert "not-secret-valid-json" not in str(exc.value)


def test_baseline_contains_only_content_free_metadata(tmp_path: Path):
    path = tmp_path / "openmem_search_sample.jsonl"
    _write_row(
        path,
        {"query": "REQUEST_SECRET"},
        {
            "status": "success",
            "code": 200,
            "data": {"memory_detail_list": [{"content": "RESPONSE_SECRET"}]},
        },
    )
    record = next(_module().iter_replay_records(path, "search", "run-7"))
    baseline_text = json.dumps(record.baseline)
    assert record.baseline["result_count"] == 1
    assert "REQUEST_SECRET" not in baseline_text
    assert "RESPONSE_SECRET" not in baseline_text


def test_validate_cli_streams_counts_and_shapes(tmp_path: Path):
    add, search = _valid_dataset(tmp_path)
    result = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "ok",
        "source_path": str(tmp_path),
        "add_file": add.name,
        "search_file": search.name,
        "add_count": 1,
        "search_count": 1,
        "request_shapes": {
            "add": [["messages", "user_id"]],
            "search": [["query", "user_id"]],
        },
    }


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    data = tmp_path / "dataset"
    data.mkdir()
    _write_row(
        data / "openmem_add_sample.jsonl",
        {"user_id": "", "conversation_id": "conv-1", "messages": []},
    )
    _write_row(
        data / "openmem_search_sample.jsonl",
        {"user_id": "", "query": "needle", "memory_limit_number": 5},
    )
    return data


def _output(module, *, status="ok", state="completed", operation=None, output=None):
    return module.MemoryTaskOutput(
        status=status,
        state=state,
        operation=operation or {},
        output=output or {},
        metrics=[],
        artifacts=[],
        error={} if status == "ok" else {"type": "backend_failed"},
    )


def _config(module, dataset_dir: Path, tmp_path: Path, **overrides):
    config = module.ReplayConfig(
        data_path=dataset_dir,
        output_dir=tmp_path / "out",
        run_id="run-1",
        memory_id="fake-memory",
        agent_id="generic-cli",
        poll_interval_seconds=0,
    )
    return replace(config, **overrides)


def test_execute_replay_drains_add_before_search_and_preserves_protocol(
    dataset_dir: Path, tmp_path: Path
):
    module = _module()
    calls = []

    def invoke(memory_id, request):
        calls.append((memory_id, request))
        if request.action == "ingest":
            assert request.inputs["raw_request"] == {
                "user_id": "replay-run-1",
                "conversation_id": "conv-1",
                "messages": [],
            }
            return _output(
                module,
                state="accepted",
                operation={"session_id": "session-1", "task_id": request.task_id},
            )
        if request.action == "status":
            assert request.inputs["operation"]["session_id"] == "session-1"
            return _output(module, operation=request.inputs["operation"])
        return _output(
            module,
            output={"count": 1, "memories": [], "evidence_text": ""},
        )

    summary = module.execute_replay(_config(module, dataset_dir, tmp_path), invoke)
    requests = [request for _, request in calls]
    first_recall = next(i for i, request in enumerate(requests) if request.action == "recall")
    assert all(request.action in {"ingest", "status"} for request in requests[:first_recall])
    assert all(request.inputs["source_protocol"] == "openmem-v1" for request in requests)
    assert requests[0].task_id in requests[0].idempotency_key
    assert requests[0].idempotency_key == requests[1].idempotency_key
    assert summary.dataset_state == "complete"
    assert [event.operation for event in summary.events] == ["add", "search"]


def test_completed_ingest_is_not_polled(dataset_dir: Path, tmp_path: Path):
    module = _module()
    actions = []

    def invoke(memory_id, request):
        actions.append(request.action)
        if request.action == "ingest":
            return _output(module, operation={"session_id": "session-1"})
        return _output(module, output={"count": 0})

    module.execute_replay(_config(module, dataset_dir, tmp_path), invoke)
    assert actions == ["ingest", "recall"]


@pytest.mark.parametrize("mode", ["terminal_failure", "timeout"])
def test_failed_or_timed_out_add_marks_dataset_partially_written(
    dataset_dir: Path, tmp_path: Path, mode: str
):
    module = _module()

    def invoke(memory_id, request):
        if request.action == "ingest":
            return _output(
                module,
                state="accepted",
                operation={"session_id": "session-1", "task_id": request.task_id},
            )
        if request.action == "status":
            if mode == "terminal_failure":
                return _output(module, status="failed", state="failed")
            return _output(module, state="running", operation=request.inputs["operation"])
        return _output(module, output={"count": 0})

    timeout = 0 if mode == "timeout" else 60
    summary = module.execute_replay(
        _config(module, dataset_dir, tmp_path, drain_timeout_seconds=timeout), invoke
    )
    assert summary.dataset_state == "partially_written"
    assert summary.events[0].status == "failed"


@pytest.mark.parametrize("mode", ["failed_output", "exception"])
def test_recall_failures_become_events_and_do_not_expose_exception_payload(
    dataset_dir: Path, tmp_path: Path, mode: str
):
    module = _module()

    def invoke(memory_id, request):
        if request.action == "ingest":
            return _output(module, operation={"session_id": "session-1"})
        if mode == "exception":
            raise RuntimeError("query needle must not leak")
        return _output(module, status="failed", state="failed")

    summary = module.execute_replay(_config(module, dataset_dir, tmp_path), invoke)
    search = summary.events[-1]
    assert search.status == "failed"
    assert "needle" not in (search.error_message or "")


def test_replay_uses_bounded_add_concurrency_and_keeps_phase_barrier(
    dataset_dir: Path, tmp_path: Path
):
    module = _module()
    add_path = dataset_dir / "openmem_add_sample.jsonl"
    add_path.write_text(
        "".join(
            json.dumps({"request": {"user_id": "", "messages": [], "n": n}}) + "\n"
            for n in range(3)
        ),
        encoding="utf-8",
    )
    lock = threading.Lock()
    active = 0
    maximum = 0
    completed = 0

    def invoke(memory_id, request):
        nonlocal active, maximum, completed
        if request.action == "ingest":
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
                completed += 1
            return _output(module, operation={"session_id": request.task_id})
        assert completed == 3
        return _output(module, output={"count": 0})

    config = _config(module, dataset_dir, tmp_path, add_concurrency=2)
    summary = module.execute_replay(config, invoke)
    assert maximum == 2
    assert [event.line_number for event in summary.events[:3]] == [1, 2, 3]


def test_replay_artifacts_exclude_payload_and_secret_sentinels(
    dataset_dir: Path, tmp_path: Path
):
    module = _module()
    _write_row(
        dataset_dir / "openmem_add_sample.jsonl",
        {"user_id": "", "messages": [{"content": "MESSAGE_SENTINEL"}]},
        {"api_key": "API_KEY_SENTINEL"},
    )
    _write_row(
        dataset_dir / "openmem_search_sample.jsonl",
        {"user_id": "", "query": "QUERY_SENTINEL"},
        {"data": {"memory_detail_list": [{"content": "MEMORY_SENTINEL"}]}},
    )

    def invoke(memory_id, request):
        if request.action == "ingest":
            return _output(module, operation={"session_id": "session-1"})
        return _output(
            module,
            output={"count": 1, "memories": [{"content": "MEMORY_SENTINEL"}]},
        )

    config = _config(module, dataset_dir, tmp_path)
    summary = module.execute_replay(config, invoke)
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in config.output_dir.rglob("*")
        if path.is_file()
    )
    for sentinel in (
        "MESSAGE_SENTINEL",
        "QUERY_SENTINEL",
        "MEMORY_SENTINEL",
        "API_KEY_SENTINEL",
    ):
        assert sentinel not in artifact_text
    assert summary.join_coverage["client_requests"] == 2


def test_run_config_exists_before_first_request(dataset_dir: Path, tmp_path: Path):
    module = _module()
    config = _config(module, dataset_dir, tmp_path)
    existed_before_request = []

    def invoke(memory_id, request):
        existed_before_request.append((config.output_dir / "run_config.json").is_file())
        if request.action == "ingest":
            return _output(module, operation={"session_id": "session-1"})
        return _output(module, output={"count": 0})

    summary = module.execute_replay(config, invoke)
    assert all(existed_before_request)
    assert all(event.status == "ok" for event in summary.events)


def test_summary_percentiles_duplicates_and_trace_coverage(tmp_path: Path):
    module = _module()
    base = module.RequestEvent(
        request_id="same",
        operation="add",
        line_number=1,
        ts="2026-01-01T00:00:00+00:00",
        payload_sha256="a" * 64,
        payload_size_bytes=1,
        top_level_fields=("messages",),
        duration_ms=10,
        status="ok",
        state="completed",
        result_count=None,
        response_size_bytes=1,
        error_type=None,
        error_message=None,
    )
    events = (base, replace(base, line_number=2, duration_ms=20))
    summary = module.summarize_replay("run-1", "complete", events, ())
    assert summary.operations["add"]["p50_ms"] == 15
    assert summary.operations["add"]["p95_ms"] == 19.5
    assert summary.join_coverage == {
        "client_requests": 2,
        "matched_internal_traces": 0,
        "missing_internal_traces": 1,
        "duplicate_request_ids": 1,
        "unmatched_internal_traces": 0,
    }
    assert summary.attribution_status == "exploratory"


@pytest.mark.parametrize(
    "overrides",
    [
        {"add_concurrency": 0},
        {"search_concurrency": 0},
        {"add_rate_per_second": -1},
        {"request_timeout_seconds": 0},
        {"poll_interval_seconds": -1},
        {"drain_timeout_seconds": -1},
        {"model_mode": "invalid"},
    ],
)
def test_replay_config_rejects_invalid_controls(
    dataset_dir: Path, tmp_path: Path, overrides: dict
):
    module = _module()
    with pytest.raises(ValueError):
        _config(module, dataset_dir, tmp_path, **overrides)
