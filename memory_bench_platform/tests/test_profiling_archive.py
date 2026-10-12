import json
from pathlib import Path

import pytest

from memory_bench_platform.profiling_archive import load_client_events, load_profile_archive


def _write_profile(root: Path, phases: list[dict]) -> None:
    (root / "profiling").mkdir()
    (root / "target_registry.json").write_text(json.dumps({
        "schema": "target-registry/1", "targets": [{"target_id": "memory-i", "physical_instance_id": "i"}]
    }))
    (root / "profiling" / "collector_status.json").write_text(json.dumps({
        "schema": "collector-status/1", "items": []
    }))
    (root / "phase_events.jsonl").write_text("".join(json.dumps(p) + "\n" for p in phases))


def _phase(start: int, end: int, *, target: str = "memory-i", phase: str = "ingest") -> dict:
    return {"schema": "phase-event/1", "run_id": "run1", "phase_id": phase,
            "target_id": target, "start_utc": "2026-01-01T00:00:00+00:00",
            "end_utc": "2026-01-01T00:00:01+00:00",
            "start_monotonic_ns": start, "end_monotonic_ns": end,
            "state": "completed", "request_count": 1, "warmup": False}


def test_old_archives_return_none(tmp_path):
    assert load_profile_archive(tmp_path) is None


def test_multi_user_without_run_json_loads_scoped_events(tmp_path):
    (tmp_path / "run_config.json").write_text(json.dumps({
        "schema": "multi-user-run/1", "run_id": "run1", "started_at": "2026-01-01T00:00:00+00:00",
        "users": {"alice": {"user_id": "run1:alice", "instance": "i", "workload": "w"}}
    }))
    (tmp_path / "request_events.jsonl").write_text(json.dumps({
        "user_id": "run1:alice", "instance_id": "i", "request_id": "req1",
        "operation": "add", "started_ms": 100, "ended_ms": 200, "status": "ok"
    }) + "\n")
    events = load_client_events(tmp_path)
    assert len(events) == 1
    assert (events[0].run_id, events[0].instance_id, events[0].user_id,
            events[0].request_id, events[0].operation, events[0].status) == (
                "run1", "i", "run1:alice", "req1", "add", "ok")
    assert events[0].started_utc.isoformat() == "2026-01-01T00:00:00.100000+00:00"


def test_ordinary_event_preserves_missing_identity(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"run_id": "run1"}))
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "case_results.json").write_text(json.dumps([
        {"case_id": "q1", "passed": True},
        {"case_id": "q2", "request_id": "read2", "operation": "search", "passed": False},
    ]))
    events = load_client_events(tmp_path)
    assert events[0].request_id is None and events[0].operation is None
    assert events[1].request_id == "read2" and events[1].status == "failed"


def test_production_replay_events_keep_actual_request_windows(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"run_id": "run1"}))
    (tmp_path / "target_registry.json").write_text(json.dumps({
        "schema": "target-registry/1", "targets": [{"target_id": "memory-i", "role": "memory",
                                                    "physical_instance_id": "i"}]}))
    replay = tmp_path / "external_artifacts" / "replay"
    replay.mkdir(parents=True)
    (replay / "run_config.json").write_text(json.dumps({
        "schema": "production-replay-config/1", "run_id": "run1"}))
    (replay / "request_events.jsonl").write_text(json.dumps({
        "request_id": "add-1", "operation": "add", "ts": "2026-01-01T00:00:01+00:00",
        "duration_ms": 250, "status": "ok", "state": "completed"}) + "\n")
    events = load_client_events(tmp_path)
    assert len(events) == 1
    assert events[0].request_id == "add-1"
    assert events[0].instance_id == "i"
    assert events[0].started_utc.isoformat() == "2026-01-01T00:00:00.750000+00:00"


def test_native_workflow_events_keep_stage_window(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"run_id": "run1"}))
    (tmp_path / "profiling_client_events.jsonl").write_text(json.dumps({
        "run_id": "run1", "request_id": "case-1", "operation": "search",
        "started_utc": "2026-01-01T00:00:00+00:00",
        "ended_utc": "2026-01-01T00:00:00.100000+00:00", "status": "ok"}) + "\n")
    events = load_client_events(tmp_path)
    assert [(item.request_id, item.operation, item.duration_ms) for item in events] == [
        ("case-1", "search", 100.0)]


def test_marked_archive_missing_phase_file_fails(tmp_path):
    (tmp_path / "profiling").mkdir()
    with pytest.raises(ValueError, match="phase_events"):
        load_profile_archive(tmp_path)


def test_phase_half_open_windows_and_duplicate_target_phase_fail(tmp_path):
    _write_profile(tmp_path, [_phase(10, 20), _phase(20, 30, phase="recall")])
    archive = load_profile_archive(tmp_path)
    assert [(p.phase_id, p.start_monotonic_ns, p.end_monotonic_ns) for p in archive.phases] == [
        ("ingest", 10, 20), ("recall", 20, 30)]
    (tmp_path / "phase_events.jsonl").write_text("".join(json.dumps(p) + "\n" for p in [
        _phase(10, 20), _phase(19, 30, phase="recall")]))
    with pytest.raises(ValueError, match="overlap"):
        load_profile_archive(tmp_path)
    (tmp_path / "phase_events.jsonl").write_text("".join(json.dumps(p) + "\n" for p in [
        _phase(10, 20), _phase(20, 30)]))
    with pytest.raises(ValueError, match="duplicate"):
        load_profile_archive(tmp_path)


def test_invalid_phase_schema_and_negative_window_fail(tmp_path):
    _write_profile(tmp_path, [_phase(10, 20)])
    phase = _phase(10, 20)
    phase["schema"] = "phase-event/2"
    (tmp_path / "phase_events.jsonl").write_text(json.dumps(phase) + "\n")
    with pytest.raises(ValueError, match="invalid profiling archive"):
        load_profile_archive(tmp_path)
    phase = _phase(20, 10)
    (tmp_path / "phase_events.jsonl").write_text(json.dumps(phase) + "\n")
    with pytest.raises(ValueError, match="invalid profiling archive"):
        load_profile_archive(tmp_path)


def test_skipped_collector_does_not_require_raw_counter_file(tmp_path):
    _write_profile(tmp_path, [_phase(10, 20)])
    (tmp_path / "profiling" / "collector_status.json").write_text(json.dumps({
        "schema": "collector-status/1", "items": [{"target_id": "memory-i", "phase_id": "ingest",
                                                   "state": "skipped", "reason": "unsupported_platform"}]
    }))
    archive = load_profile_archive(tmp_path)
    assert archive.collectors[0].state == "skipped"


def test_multi_user_duplicate_event_id_fails(tmp_path):
    (tmp_path / "run_config.json").write_text(json.dumps({
        "schema": "multi-user-run/1", "run_id": "run1", "started_at": "2026-01-01T00:00:00+00:00",
        "users": {"alice": {"user_id": "run1:alice", "instance": "i", "workload": "w"}}
    }))
    event = {"user_id": "run1:alice", "instance_id": "i", "request_id": "req1",
             "operation": "add", "started_ms": 100, "ended_ms": 200, "status": "ok"}
    (tmp_path / "request_events.jsonl").write_text((json.dumps(event) + "\n") * 2)
    with pytest.raises(ValueError, match="duplicate client event"):
        load_client_events(tmp_path)


def test_phase_run_id_must_match_archive_run_id(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"run_id": "run1"}))
    phase = _phase(10, 20)
    phase["run_id"] = "other-run"
    _write_profile(tmp_path, [phase])
    with pytest.raises(ValueError, match="run_id"):
        load_profile_archive(tmp_path)
