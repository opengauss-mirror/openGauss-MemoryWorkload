import json
from datetime import datetime, timezone

from memory_bench_platform.profiling_archive import ClientEvent, ProfileArchive
from memory_bench_platform.profiling_trace_analysis import analyze_trace


def _archive(tmp_path, rows, *, threshold=0.95):
    root = tmp_path / "profiling"
    root.mkdir()
    (root / "perf_trace_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (root / "stage_map.json").write_text(json.dumps({"schema": "stage-map/1", "stages": {
        "extract": {"category": "extract_llm", "role": "leaf", "operation": "add"},
        "write": {"category": "memory_write", "role": "leaf", "operation": "add"},
        "pipeline": {"category": "pipeline", "role": "wrapper", "operation": "add"},
        "index": {"category": "memory_write", "role": "leaf", "operation": "add", "async": True},
    }}))
    (tmp_path / "run_config.json").write_text(json.dumps({
        "profiling": {"quality_gates": {"min_request_join_rate": threshold}}
    }))
    return ProfileArchive(tmp_path, (), (), ())


def _client(user="alice", request="r1", operation="add", duration=100):
    return ClientEvent("run", "instance", user, request, operation,
                       datetime(2026, 1, 1, tzinfo=timezone.utc),
                       datetime(2026, 1, 1, tzinfo=timezone.utc), "ok", duration)


def _span(stage="extract", role="leaf", duration=40, user="alice", request="r1", **extra):
    return {"kind": "memory_perf", "run_id": "run", "instance_id": "instance",
            "user_id": user, "request_id": request, "operation": "add", "stage": stage,
            "span_role": role, "span_id": stage + "-1", "duration_ms": duration,
            "status": "ok", "ts": "2026-01-01T00:00:00+00:00", **extra}


def test_join_requires_scope_and_operation(tmp_path):
    archive = _archive(tmp_path, [_span()])
    report = analyze_trace(archive, [_client("alice"), _client("bob")])
    assert report.join_coverage == {"matched": 1, "missing": 1, "extra": 0,
                                    "duplicates": 0, "rate": 0.5}
    assert report.validity == "invalid"
    assert report.reason == "low_request_join_rate"


def test_wrapper_leaf_not_double_counted(tmp_path):
    archive = _archive(tmp_path, [_span("pipeline", "wrapper", 100),
                                  _span("extract", "leaf", 40), _span("write", "leaf", 30)])
    report = analyze_trace(archive, [_client()])
    assert report.stage_durations_ms["add"] == {"extract_llm": [40.0], "memory_write": [30.0]}
    assert report.residuals[0]["unattributed_ms"] == 30.0
    assert report.residuals[0]["client_service_gap_ms"] == 0.0


def test_async_task_span_maps_to_drain(tmp_path):
    archive = _archive(tmp_path, [_span("index", "leaf", 20, task_id="task1")])
    report = analyze_trace(archive, [_client(operation="drain")])
    assert report.stage_durations_ms["drain"] == {"memory_write": [20.0]}
    assert report.join_coverage["matched"] == 1


def test_missing_clock_anchor_prevents_perf_bucket_join(tmp_path):
    archive = _archive(tmp_path, [_span()])
    report = analyze_trace(archive, [_client()])
    assert report.time_bucket_eligible == 0
    assert report.join_coverage["matched"] == 1


def test_invalid_and_extra_trace_rows_are_accounted(tmp_path):
    archive = _archive(tmp_path, [_span(), _span(duration=50),
                                  _span(stage="unknown", duration=10),
                                  _span(stage="bad", duration=-1),
                                  _span(stage="write", user="other", request="extra")])
    report = analyze_trace(archive, [_client()])
    assert report.join_coverage["duplicates"] == 1
    assert report.join_coverage["extra"] == 1
    assert report.invalid_rows["negative_duration"] == 1
    assert report.invalid_rows["unknown_stage"] == 1
    assert report.stage_durations_ms["add"]["other_leaf"] == [10.0]
