import json

import pytest

from memory_bench_platform.profiling_phase import PhaseSink
from memory_bench_platform.profiling_archive import load_profile_archive
from memory_bench_platform.profiling_targets import TargetIdentity


class FakeSession:
    def __init__(self):
        self.targets = {"memory-i": TargetIdentity("memory-i", "memory", "i", "external")}
        self.calls = []

    def start_phase(self, phase, targets):
        self.calls.append(("start", phase, targets))
        return [object()]

    def stop_phase(self, handles):
        self.calls.append(("stop", handles))
        return []


def test_collector_ready_precedes_phase_window(tmp_path):
    session = FakeSession()
    sink = PhaseSink(tmp_path, "run", session)
    result = sink.run_phase("ingest", ["memory-i"], lambda: session.calls.append(("traffic",)))
    assert result is None
    assert [call[0] for call in session.calls] == ["start", "traffic", "stop"]
    event = json.loads((tmp_path / "phase_events.jsonl").read_text())
    assert event["state"] == "completed" and event["start_monotonic_ns"] < event["end_monotonic_ns"]


def test_window_closes_before_collector_stop(tmp_path):
    session = FakeSession()
    sink = PhaseSink(tmp_path, "run", session)
    class CheckingSession(FakeSession):
        def stop_phase(self, handles):
            assert (tmp_path / "phase_events.jsonl").read_text().strip()
            return super().stop_phase(handles)
    session = CheckingSession()
    sink = PhaseSink(tmp_path, "run", session)
    sink.run_phase("recall", ["memory-i"], lambda: None)


def test_empty_phase_not_applicable(tmp_path):
    session = FakeSession()
    sink = PhaseSink(tmp_path, "run", session)
    sink.run_phase("drain", ["memory-i"], lambda: None, applicable=False)
    assert session.calls == []
    event = json.loads((tmp_path / "phase_events.jsonl").read_text())
    assert event["state"] == "not_applicable" and event["request_count"] == 0


def test_shared_target_started_once(tmp_path):
    session = FakeSession()
    sink = PhaseSink(tmp_path, "run", session)
    sink.run_phase("ingest", ["memory-i", "memory-i"], lambda: None)
    assert session.calls[0][2] == ["memory-i"]
    assert len((tmp_path / "phase_events.jsonl").read_text().splitlines()) == 1


def test_exception_persists_partial_phase(tmp_path):
    session = FakeSession()
    sink = PhaseSink(tmp_path, "run", session)
    with pytest.raises(KeyboardInterrupt):
        sink.run_phase("ingest", ["memory-i"], lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    event = json.loads((tmp_path / "phase_events.jsonl").read_text())
    assert event["state"] == "interrupted"
    assert session.calls[-1][0] == "stop"


def test_target_registry_has_one_physical_instance(tmp_path):
    session = FakeSession()
    PhaseSink(tmp_path, "run", session)
    registry = json.loads((tmp_path / "target_registry.json").read_text())
    assert [row["physical_instance_id"] for row in registry["targets"]] == ["i"]


def test_overlapping_parent_child_are_explicit(tmp_path):
    session = FakeSession()
    sink = PhaseSink(tmp_path, "run", session)
    sink.run_phase("recall", ["memory-i"],
        lambda: sink.run_phase("qa", ["memory-i"], lambda: None, parent_phase_id="recall"))
    (tmp_path / "profiling").mkdir()
    (tmp_path / "profiling" / "collector_status.json").write_text(json.dumps({
        "schema": "collector-status/1", "items": []}))
    archive = load_profile_archive(tmp_path)
    assert len(archive.phases) == 2
    assert next(p for p in archive.phases if p.phase_id == "qa").parent_phase_id == "recall"


def test_failed_stop_marks_profile_invalid_after_operation(tmp_path):
    from types import SimpleNamespace
    session = FakeSession()
    session.config = SimpleNamespace(mode="required")
    session.stop_phase = lambda handles: [SimpleNamespace(state="invalid", reason="low_running_fraction")]
    sink = PhaseSink(tmp_path, "run", session)
    sink.run_phase("ingest", ["memory-i"], lambda: None)
    assert sink.invalid_profile is True
