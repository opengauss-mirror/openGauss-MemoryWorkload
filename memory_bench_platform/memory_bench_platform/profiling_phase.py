"""True measured-phase windows bracketed by collector readiness and stop."""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from .profiling_archive import PhaseEvent
from .profiling_perf import ProfilerSession

T = TypeVar("T")


class PhaseSink:
    def __init__(self, run_dir: Path, run_id: str, session: ProfilerSession):
        self.run_dir, self.run_id, self.session = run_dir, run_id, session
        self.invalid_profile = False
        run_dir.mkdir(parents=True, exist_ok=True)
        targets = [asdict(target) for target in session.targets.values()]
        (run_dir / "target_registry.json").write_text(json.dumps(
            {"schema": "target-registry/1", "targets": targets}, indent=2), encoding="utf-8")
        (run_dir / "phase_events.jsonl").touch(exist_ok=True)

    def run_phase(self, phase_id: str, target_ids: list[str], operation: Callable[[], T], *,
                  warmup: bool = False, applicable: bool = True,
                  request_count: Callable[[], int] | None = None,
                  parent_phase_id: str | None = None) -> T:
        unique_targets = list(dict.fromkeys(target_ids))
        if not applicable:
            instant = datetime.now(timezone.utc)
            tick = time.monotonic_ns()
            self._write(unique_targets, phase_id, instant, instant, tick, tick,
                        "not_applicable", 0, warmup, parent_phase_id)
            return operation()
        handles = self.session.start_phase(phase_id, unique_targets)
        start_utc = datetime.now(timezone.utc)
        start_ns = time.monotonic_ns()
        state = "completed"
        try:
            return operation()
        except BaseException as exc:
            state = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            raise
        finally:
            end_ns = time.monotonic_ns()
            end_utc = datetime.now(timezone.utc)
            count = request_count() if request_count is not None else 0
            try:
                self._write(unique_targets, phase_id, start_utc, end_utc, start_ns,
                            end_ns, state, count, warmup, parent_phase_id)
            finally:
                statuses = self.session.stop_phase(handles)
                if any(item.state in {"failed", "invalid"} for item in statuses):
                    self.invalid_profile = True

    def _write(self, targets: list[str], phase: str, start_utc: datetime, end_utc: datetime,
               start_ns: int, end_ns: int, state: str, count: int, warmup: bool,
               parent_phase_id: str | None) -> None:
        with (self.run_dir / "phase_events.jsonl").open("a", encoding="utf-8") as stream:
            for target in targets:
                event = PhaseEvent.model_validate({"schema": "phase-event/1", "run_id": self.run_id,
                    "phase_id": phase, "target_id": target, "start_utc": start_utc,
                    "parent_phase_id": parent_phase_id,
                    "end_utc": end_utc, "start_monotonic_ns": start_ns,
                    "end_monotonic_ns": end_ns, "state": state,
                    "request_count": count, "warmup": warmup})
                stream.write(event.model_dump_json(by_alias=True) + "\n")
            stream.flush()
