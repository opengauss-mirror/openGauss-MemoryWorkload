import json
import subprocess
import sys
import time

import pytest

from memory_bench_platform.profiling_config import ProfilingConfig
from memory_bench_platform.profiling_perf import ProfilerSession
from memory_bench_platform.profiling_targets import CapabilityResult, TargetIdentity


def _config(**changes):
    return ProfilingConfig.model_validate({"schema": "profiling/1", "targets": ["memory"], **changes})


def _target():
    return TargetIdentity("memory-i", "memory", "i", "external", cgroup_path="/sys/fs/cgroup/test", cpu_scope="0-3")


class FakeProcess:
    pid = 1234
    def __init__(self, stderr="1000;;cycles;1000;100.00\n500;;instructions;1000;100.00\n"):
        self.stderr = stderr
        self.signaled = False

    def communicate(self, timeout=None):
        return "", self.stderr

    def poll(self):
        return None if not self.signaled else 0


def test_ready_precedes_phase_window(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_perf
    monkeypatch.setattr(profiling_perf.os, "killpg", lambda pid, sig: None)
    processes = []
    def factory(argv, **kwargs):
        processes.append((argv, kwargs, FakeProcess()))
        return processes[-1][2]
    session = ProfilerSession(_config(), [_target()], tmp_path, command_factory=factory,
                              probe=lambda target, config: CapabilityResult("ready", None, "basic", "perf 1", "0-3"))
    handles = session.start_phase("ingest", ["memory-i"])
    assert handles[0].state == "running"
    assert processes[0][1]["start_new_session"] is True
    assert json.loads((tmp_path / "profiling" / "collector_status.json").read_text())["items"][0]["state"] == "running"


def test_required_probe_failure_blocks_start(tmp_path):
    session = ProfilerSession(_config(mode="required"), [_target()], tmp_path,
        command_factory=lambda *a, **k: pytest.fail("must not start"),
        probe=lambda target, config: CapabilityResult("skipped", "perf_unavailable"))
    with pytest.raises(RuntimeError, match="perf_unavailable"):
        session.start_phase("ingest", ["memory-i"])


def test_required_immediate_perf_exit_blocks_work(tmp_path):
    class ExitedProcess(FakeProcess):
        def poll(self):
            return 1
    session = ProfilerSession(_config(mode="required"), [_target()], tmp_path,
        command_factory=lambda *a, **k: ExitedProcess("permission denied"),
        probe=lambda target, config: CapabilityResult("ready", None, "basic"))
    with pytest.raises(RuntimeError, match="collector_start_failed"):
        session.start_phase("ingest", ["memory-i"])
    status = json.loads((tmp_path / "profiling" / "collector_status.json").read_text())
    assert status["items"][0]["state"] == "failed"


def test_target_restart_during_phase_invalidates_counters(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_perf
    monkeypatch.setattr(profiling_perf.os, "killpg", lambda pid, sig: None)
    class Docker:
        restart = 0
        def inspect(self, container):
            return {"Id": container, "RestartCount": self.restart,
                    "State": {"StartedAt": "start"}}
    docker = Docker()
    target = TargetIdentity("memory-i", "memory", "i", "compose",
        container_id="cid", cgroup_path="/sys/fs/cgroup/test", restart_count=0,
        start_time="start")
    session = ProfilerSession(_config(), [target], tmp_path,
        command_factory=lambda *a, **k: FakeProcess(), docker=docker,
        probe=lambda target, config: CapabilityResult("ready", None, "basic"))
    handles = session.start_phase("ingest", ["memory-i"])
    docker.restart = 1
    status = session.stop_phase(handles)
    assert status[0].state == "invalid" and status[0].reason == "target_drift"


def test_best_effort_topdown_downgrades_by_name(tmp_path):
    session = ProfilerSession(_config(hardware={"stat": "topdown_l1"}), [_target()], tmp_path,
        command_factory=lambda *a, **k: FakeProcess(),
        probe=lambda target, config: CapabilityResult("skipped", "topdown_unsupported"))
    handles = session.start_phase("ingest", ["memory-i"])
    assert handles[0].state == "skipped" and handles[0].reason == "topdown_unsupported"


def test_counter_running_below_090_invalid(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_perf
    monkeypatch.setattr(profiling_perf.os, "killpg", lambda pid, sig: None)
    session = ProfilerSession(_config(), [_target()], tmp_path,
        command_factory=lambda *a, **k: FakeProcess("1000;;cycles;1000;50.00\n"),
        probe=lambda target, config: CapabilityResult("ready", None, "basic"))
    status = session.stop_phase(session.start_phase("ingest", ["memory-i"]))
    assert status[0].state == "invalid" and status[0].reason == "low_running_fraction"


def test_raw_size_cap_invalidates_capture(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_perf
    monkeypatch.setattr(profiling_perf.os, "killpg", lambda pid, sig: None)
    session = ProfilerSession(_config(hardware={"max_raw_bytes": 12}), [_target()], tmp_path,
        command_factory=lambda *a, **k: FakeProcess("x" * 20),
        probe=lambda target, config: CapabilityResult("ready", None, "basic"))
    status = session.stop_phase(session.start_phase("ingest", ["memory-i"]))
    assert status[0].state == "invalid" and status[0].reason == "raw_size_limit"


def test_stop_is_idempotent_after_interrupt(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_perf
    monkeypatch.setattr(profiling_perf.os, "killpg", lambda pid, sig: None)
    session = ProfilerSession(_config(), [_target()], tmp_path,
        command_factory=lambda *a, **k: FakeProcess(),
        probe=lambda target, config: CapabilityResult("ready", None, "basic"))
    handles = session.start_phase("ingest", ["memory-i"])
    first = session.stop_phase(handles)
    assert session.stop_phase(handles) == first


def test_timeout_kills_process_group_descendants(tmp_path):
    marker = tmp_path / "orphan.txt"
    child = "import pathlib,sys,time; time.sleep(.4); pathlib.Path(sys.argv[1]).write_text('orphan')"
    parent = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]); "
              "print('spawned', flush=True); time.sleep(30)")
    def factory(argv, **kwargs):
        return subprocess.Popen([sys.executable, "-c", parent, str(marker), child], **kwargs)
    session = ProfilerSession(_config(), [_target()], tmp_path, command_factory=factory,
        probe=lambda target, config: CapabilityResult("ready", None, "basic"))
    handles = session.start_phase("ingest", ["memory-i"])
    time.sleep(.1)
    session.stop_phase(handles)
    time.sleep(.5)
    assert not marker.exists()


def test_topdown_l1_writes_four_valid_shares(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_perf
    monkeypatch.setattr(profiling_perf.os, "killpg", lambda pid, sig: None)
    raw = "".join(f"25;;{event};1000;100.00\n" for event in (
        "topdown-fe-bound", "topdown-bad-spec", "topdown-be-bound", "topdown-retiring"))
    session = ProfilerSession(_config(hardware={"stat": "topdown_l1"}), [_target()], tmp_path,
        command_factory=lambda *a, **k: FakeProcess(raw),
        probe=lambda target, config: CapabilityResult("ready", None, "topdown_l1"))
    status = session.stop_phase(session.start_phase("ingest", ["memory-i"]))
    assert status[0].state == "completed"
    text = (tmp_path / "profiling" / "topdown_summary.csv").read_text()
    assert "frontend_bound,bad_speculation,backend_bound,retiring" in text
    assert ",25.0,25.0,25.0,25.0" in text


def test_selected_phase_starts_perf_record_and_stops_it(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_perf
    monkeypatch.setattr(profiling_perf.os, "killpg", lambda pid, sig: None)
    commands = []
    def factory(argv, **kwargs):
        commands.append(argv)
        return FakeProcess()
    session = ProfilerSession(_config(hardware={"record": "selected_phases", "record_phases": ["ingest"]}),
        [_target()], tmp_path, command_factory=factory,
        probe=lambda target, config: CapabilityResult("ready", None, "basic"))
    session.stop_phase(session.start_phase("ingest", ["memory-i"]))
    assert [argv[1] for argv in commands] == ["stat", "record"]
    assert "-o" in commands[1]


def test_perf_record_limit_stops_capture_during_phase(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_perf
    signals = []
    monkeypatch.setattr(profiling_perf.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    def factory(argv, **kwargs):
        if argv[1] == "record":
            path = tmp_path / "profiling" / "raw" / "memory-i" / "ingest" / "perf.data"
            path.write_bytes(b"x" * 20)
        return FakeProcess()
    session = ProfilerSession(_config(hardware={"record": "selected_phases",
        "record_phases": ["ingest"], "max_raw_bytes": 10}), [_target()], tmp_path,
        command_factory=factory,
        probe=lambda target, config: CapabilityResult("ready", None, "basic"))
    handles = session.start_phase("ingest", ["memory-i"])
    time.sleep(.12)
    assert handles[0].record_overflow is True
    assert signals
    assert session.stop_phase(handles)[0].reason == "raw_size_limit"
