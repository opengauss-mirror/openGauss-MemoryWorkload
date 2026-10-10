import json
import csv

import pytest

from memory_bench_platform.profiling_analysis import analyze_profiling_archive


def _fixture(tmp_path, *, collector="completed", running="1.0", topdown=None):
    (tmp_path / "profiling").mkdir()
    (tmp_path / "run_config.json").write_text(json.dumps({
        "schema": "multi-user-run/1", "run_id": "run1", "started_at": "2026-01-01T00:00:00+00:00",
        "users": {"alice": {"user_id": "run1:alice", "instance": "i", "workload": "w"}},
        "profiling": {"quality_gates": {"min_request_join_rate": 0.95, "min_counter_running_fraction": 0.90}}
    }))
    (tmp_path / "request_events.jsonl").write_text(json.dumps({
        "user_id": "run1:alice", "instance_id": "i", "request_id": "r1", "operation": "add",
        "started_ms": 100, "ended_ms": 200, "status": "ok"
    }) + "\n")
    (tmp_path / "target_registry.json").write_text(json.dumps({"schema": "target-registry/1",
        "targets": [{"target_id": "memory-i", "physical_instance_id": "i"}]}))
    (tmp_path / "phase_events.jsonl").write_text(json.dumps({
        "schema": "phase-event/1", "run_id": "run1", "target_id": "memory-i", "phase_id": "ingest",
        "start_utc": "2026-01-01T00:00:00+00:00", "end_utc": "2026-01-01T00:00:01+00:00",
        "start_monotonic_ns": 10, "end_monotonic_ns": 1000000010,
        "state": "completed", "request_count": 1, "warmup": False}) + "\n")
    (tmp_path / "profiling" / "collector_status.json").write_text(json.dumps({
        "schema": "collector-status/1", "items": [{"target_id": "memory-i", "phase_id": "ingest",
        "state": collector, "reason": "target_restarted" if collector == "invalid" else None}]
    }))
    (tmp_path / "profiling" / "perf_counters.csv").write_text(
        "target_id,phase_id,event,value,running_fraction,cpu_scope,event_group\n"
        f"memory-i,ingest,cycles,1000,{running},0-3,basic\n"
        f"memory-i,ingest,instructions,500,{running},0-3,basic\n")
    raw = tmp_path / "profiling" / "raw" / "memory-i" / "ingest"
    raw.mkdir(parents=True)
    (raw / "perf-stat.txt").write_text("1000;;cycles;1000;100.00\n")
    if topdown is not None:
        (tmp_path / "profiling" / "topdown_summary.csv").write_text(
            "target_id,phase_id,cpu_scope,event_group,frontend_bound,bad_speculation,backend_bound,retiring\n"
            f"memory-i,ingest,0-3,basic,{','.join(str(x) for x in topdown)}\n")
    return tmp_path


def test_reanalysis_rebuilds_same_report(tmp_path):
    run = _fixture(tmp_path)
    first = analyze_profiling_archive(run)
    assert first.summary["phases"]["memory-i:ingest"]["qps"] == 1.0
    assert first.summary["phases"]["memory-i:ingest"]["users"]["run1:alice"]["count"] == 1
    before = (run / "reports" / "profiling_analysis.json").read_bytes()
    analyze_profiling_archive(run)
    assert (run / "reports" / "profiling_analysis.json").read_bytes() == before
    assert (run / "reports" / "join_coverage.json").exists()
    assert (run / "reports" / "stage_summary.csv").exists()
    assert first.summary["overall"]["ingest"]["qps"] == 1.0
    assert "Overall" in (run / "reports" / "profiling_analysis.html").read_text()


def test_counter_running_below_090_invalid(tmp_path):
    report = analyze_profiling_archive(_fixture(tmp_path, running="0.50"))
    assert report.summary["hardware"]["memory-i:ingest"]["state"] == "invalid"
    assert report.summary["hardware"]["memory-i:ingest"]["reason"] == "low_running_fraction"


def test_topdown_sum_outside_95_to_105_invalid(tmp_path):
    report = analyze_profiling_archive(_fixture(tmp_path, topdown=(20, 20, 20, 20)))
    assert report.summary["hardware"]["memory-i:ingest"]["topdown_valid"] is False
    assert report.summary["hardware"]["memory-i:ingest"]["topdown_reason"] == "sum_out_of_range"


def test_restart_invalidates_target_phase(tmp_path):
    report = analyze_profiling_archive(_fixture(tmp_path, collector="invalid"))
    assert report.summary["hardware"]["memory-i:ingest"]["state"] == "invalid"
    assert report.summary["hardware"]["memory-i:ingest"]["reason"] == "target_restarted"


def test_skipped_hardware_is_unavailable_not_zero(tmp_path):
    run = _fixture(tmp_path, collector="skipped")
    (run / "profiling" / "perf_counters.csv").unlink()
    report = analyze_profiling_archive(run)
    hardware = report.summary["hardware"]["memory-i:ingest"]
    assert hardware["state"] == "skipped"
    assert "cycles" not in hardware


def test_missing_collector_status_is_invalid_not_skipped(tmp_path):
    run = _fixture(tmp_path)
    (run / "profiling" / "collector_status.json").write_text(json.dumps({
        "schema": "collector-status/1", "items": []}))
    report = analyze_profiling_archive(run)
    assert report.summary["hardware"]["memory-i:ingest"] == {
        "state": "invalid", "reason": "missing_collector_status"}


def test_report_excludes_trace_payload_and_private_endpoint(tmp_path):
    run = _fixture(tmp_path)
    (run / "run_config.json").write_text((run / "run_config.json").read_text().replace(
        '"profiling":', '"private_endpoint": "https://secret.example", "profiling":'))
    (run / "profiling" / "stage_map.json").write_text(json.dumps({
        "schema": "stage-map/1", "stages": {"extract": {"category": "extract_llm", "role": "leaf", "operation": "add"}}}))
    (run / "profiling" / "perf_trace_events.jsonl").write_text(json.dumps({
        "kind": "memory_perf", "run_id": "run1", "instance_id": "i", "user_id": "run1:alice",
        "request_id": "r1", "operation": "add", "stage": "extract", "span_role": "leaf",
        "span_id": "s1", "duration_ms": 80, "status": "ok", "prompt": "PRIVATE_BODY"
    }) + "\n")
    analyze_profiling_archive(run)
    public = "".join(p.read_text() for p in (run / "reports").iterdir())
    assert "PRIVATE_BODY" not in public and "secret.example" not in public
    assert "CPU bottleneck" not in public


def test_tampered_existing_profile_summary_is_rejected(tmp_path):
    run = _fixture(tmp_path)
    analyze_profiling_archive(run)
    summary_path = run / "reports" / "profiling_analysis.json"
    summary_path.write_text('{"schema": "profiling-analysis/1", "phases": {}}')
    with pytest.raises(ValueError, match="disagree"):
        analyze_profiling_archive(run)


def test_phase_latency_percentiles_use_client_durations(tmp_path):
    run = _fixture(tmp_path)
    report = analyze_profiling_archive(run)
    phase = report.summary["phases"]["memory-i:ingest"]
    assert (phase["p50_ms"], phase["p95_ms"], phase["p99_ms"]) == (100.0, 100.0, 100.0)


def test_production_replay_counts_unscoped_events_in_phase_window(tmp_path):
    run = _fixture(tmp_path, collector="skipped")
    (run / "run_config.json").unlink()
    (run / "run.json").write_text(json.dumps({"run_id": "run1"}))
    replay = run / "external_artifacts" / "replay"
    replay.mkdir(parents=True)
    (replay / "run_config.json").write_text(json.dumps({
        "schema": "production-replay-config/1", "run_id": "run1"}))
    (replay / "request_events.jsonl").write_text(json.dumps({
        "request_id": "r1", "operation": "add", "ts": "2026-01-01T00:00:00.200000+00:00",
        "duration_ms": 100, "status": "ok", "state": "completed"}) + "\n")
    report = analyze_profiling_archive(run)
    assert report.summary["phases"]["memory-i:ingest"]["qps"] == 1.0


def test_unscoped_event_does_not_count_for_other_target(tmp_path):
    run = _fixture(tmp_path)
    (run / "run_config.json").unlink()
    (run / "run.json").write_text(json.dumps({"run_id": "run1"}))
    (run / "target_registry.json").write_text(json.dumps({"schema": "target-registry/1",
        "targets": [{"target_id": "memory-i", "physical_instance_id": "i", "role": "memory"},
                    {"target_id": "memory-j", "physical_instance_id": "j", "role": "memory"}]}))
    replay = run / "external_artifacts" / "replay"
    replay.mkdir(parents=True)
    (replay / "run_config.json").write_text(json.dumps({
        "schema": "production-replay-config/1", "run_id": "run1"}))
    (replay / "request_events.jsonl").write_text(json.dumps({
        "request_id": "r1", "operation": "add", "ts": "2026-01-01T00:00:00.200000+00:00",
        "duration_ms": 100, "status": "ok", "state": "completed"}) + "\n")
    report = analyze_profiling_archive(run)
    assert report.summary["phases"]["memory-i:ingest"]["count"] == 0


def test_ordinary_run_uses_archived_quality_gate(tmp_path):
    from memory_bench_platform.profiling_analysis import _gate
    (tmp_path / "run.json").write_text(json.dumps({
        "config": {"profiling": {"quality_gates": {"min_counter_running_fraction": 0.75}}}}))
    assert _gate(tmp_path, "min_counter_running_fraction", 0.90) == 0.75


def test_topdown_requires_same_event_group(tmp_path):
    run = _fixture(tmp_path, topdown=(25, 25, 25, 25))
    path = run / "profiling" / "topdown_summary.csv"
    path.write_text(path.read_text().replace("0-3,basic", "0-3,other"))
    report = analyze_profiling_archive(run)
    hardware = report.summary["hardware"]["memory-i:ingest"]
    assert hardware["topdown_valid"] is False
    assert hardware["topdown_reason"] == "incomparable_event_scope"


def test_stage_summary_includes_tail_latency(tmp_path):
    run = _fixture(tmp_path)
    (run / "profiling" / "stage_map.json").write_text(json.dumps({
        "schema": "stage-map/1", "stages": {"extract": {"category": "extract_llm", "role": "leaf", "operation": "add"}}}))
    (run / "profiling" / "perf_trace_events.jsonl").write_text(json.dumps({
        "kind": "memory_perf", "run_id": "run1", "instance_id": "i", "user_id": "run1:alice",
        "request_id": "r1", "operation": "add", "stage": "extract", "span_role": "leaf",
        "span_id": "s1", "duration_ms": 80, "status": "ok"
    }) + "\n")
    analyze_profiling_archive(run)
    with (run / "reports" / "stage_summary.csv").open(newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["p95_ms"] == "80.0"
