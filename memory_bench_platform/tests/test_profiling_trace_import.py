import json
from datetime import datetime, timezone

import pytest

from memory_bench_platform.profiling_trace_import import (
    TraceRunScope, capture_trace_cursor, import_trace,
)


def _scope():
    return TraceRunScope("run", {"instance"}, {"alice"}, {"r1"},
        datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc))


def _map(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"schema": "stage-map/1", "stages": {
        "extract": {"category": "extract_llm", "role": "leaf", "operation": "add"}}}))
    return path


def _row(**kwargs):
    return {"kind": "memory_perf", "ts": "2026-01-01T12:00:00+00:00",
        "run_id": "run", "instance_id": "instance", "user_id": "alice", "request_id": "r1",
        "operation": "add", "stage": "extract", "span_role": "leaf", "span_id": "s1",
        "duration_ms": 10, "status": "ok", **kwargs}


def test_mem_perf_prefix_and_plain_jsonl(tmp_path):
    source = tmp_path / "source.log"
    source.write_text("")
    cursor = capture_trace_cursor(source)
    source.write_text("[MEM_PERF] " + json.dumps(_row()) + "\n")
    result = import_trace(source, cursor, _scope(), _map(tmp_path), tmp_path / "out")
    assert result.imported == 1
    row = json.loads((tmp_path / "out" / "profiling" / "perf_trace_events.jsonl").read_text())
    assert row["stage"] == "extract" and row["duration_ms"] == 10


def test_rotation_uses_saved_identity_and_offset(tmp_path):
    source = tmp_path / "source.log"
    source.write_text("old-history\n")
    cursor = capture_trace_cursor(source)
    with source.open("a") as stream:
        stream.write(json.dumps(_row()) + "\n")
    source.rename(tmp_path / "source.log.1")
    source.write_text(json.dumps(_row(span_id="s2")) + "\n")
    result = import_trace(source, cursor, _scope(), _map(tmp_path), tmp_path / "out")
    assert result.imported == 2


def test_out_of_run_and_missing_ids_counted(tmp_path):
    source = tmp_path / "source.log"
    source.write_text("")
    cursor = capture_trace_cursor(source)
    source.write_text(json.dumps(_row(user_id="other")) + "\n" +
                      json.dumps(_row(request_id=None, span_id="s2")) + "\n")
    result = import_trace(source, cursor, _scope(), _map(tmp_path), tmp_path / "out")
    assert result.imported == 0
    assert result.counts["out_of_scope"] == 1 and result.counts["missing_id"] == 1


def test_duplicate_span_and_negative_duration_counted(tmp_path):
    source = tmp_path / "source.log"
    source.write_text("")
    cursor = capture_trace_cursor(source)
    source.write_text("\n".join(json.dumps(row) for row in [
        _row(), _row(), _row(span_id="s2", duration_ms=-1)]) + "\n")
    result = import_trace(source, cursor, _scope(), _map(tmp_path), tmp_path / "out")
    assert result.imported == 1
    assert result.counts["duplicate_span_id"] == 1
    assert result.counts["negative_duration"] == 1


def test_prompt_and_response_never_copied(tmp_path):
    source = tmp_path / "source.log"
    source.write_text("")
    cursor = capture_trace_cursor(source)
    source.write_text(json.dumps({**_row(), "prompt": "SECRET_PROMPT", "content": "SECRET_REPLY"}) + "\n")
    import_trace(source, cursor, _scope(), _map(tmp_path), tmp_path / "out")
    assert "SECRET_" not in (tmp_path / "out" / "profiling" / "perf_trace_events.jsonl").read_text()


def test_incompatible_stage_map_fails(tmp_path):
    source = tmp_path / "source.log"
    source.write_text("")
    cursor = capture_trace_cursor(source)
    path = _map(tmp_path)
    path.write_text('{"schema":"stage-map/2","stages":{}}')
    with pytest.raises(ValueError, match="stage map"):
        import_trace(source, cursor, _scope(), path, tmp_path / "out")


def test_async_task_id_preserved(tmp_path):
    source = tmp_path / "source.log"
    source.write_text("")
    cursor = capture_trace_cursor(source)
    source.write_text(json.dumps(_row(task_id="task-1")) + "\n")
    import_trace(source, cursor, _scope(), _map(tmp_path), tmp_path / "out")
    row = json.loads((tmp_path / "out" / "profiling" / "perf_trace_events.jsonl").read_text())
    assert row["task_id"] == "task-1"
