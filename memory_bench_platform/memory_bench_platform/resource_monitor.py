from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import threading
import time
from typing import Sequence


@dataclass
class CsvWriter:
    path: Path
    headers: list[str]

    def create(self) -> None:
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(self.headers)


class ResourceMonitor:
    def __init__(
        self,
        output_dir: Path,
        work_dir: Path,
        disk_mount: str,
        net_interface: str,
        sample_interval_seconds: float = 1.0,
    ):
        self.output_dir = output_dir
        self.work_dir = work_dir
        self.disk_mount = disk_mount
        self.net_interface = net_interface
        self.sample_interval_seconds = sample_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cpu_counters: tuple[float, ...] | None = None

    def setup_writers(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        writers = [
            CsvWriter(self.output_dir / "cpu_status.csv", ["timestamp", "summary_util_user", "summary_util_sys", "summary_util_idle"]),
            CsvWriter(self.output_dir / "mem_status.csv", ["timestamp", "mem_free_mb", "mem_used_mb"]),
            CsvWriter(self.output_dir / "disk_status.csv", ["timestamp", "read_bw_mb", "write_bw_mb", "disk_bw_mb", "disk_free_mb"]),
            CsvWriter(self.output_dir / "net_status.csv", ["timestamp", "recv_pcks_rate", "sent_pcks_rate", "recv_bytes_rate", "sent_bytes_rate"]),
        ]
        for writer in writers:
            writer.create()

    def _read_cpu_counters(self) -> tuple[float, ...]:
        cpu_line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
        parts = cpu_line.split()[1:]
        return tuple(float(item) for item in parts[:7])

    def _compute_cpu_percentages(
        self,
        previous: Sequence[float],
        current: Sequence[float],
    ) -> tuple[float, float, float]:
        deltas = [max(0.0, float(cur) - float(prev)) for prev, cur in zip(previous, current)]
        total = sum(deltas) or 1.0
        user = deltas[0] / total * 100
        system = deltas[2] / total * 100
        idle = deltas[3] / total * 100
        return round(user, 2), round(system, 2), round(idle, 2)

    def _read_cpu_percentages(self) -> tuple[float, float, float]:
        current = self._read_cpu_counters()
        previous = self._last_cpu_counters
        self._last_cpu_counters = current
        if previous is None:
            return 0.0, 0.0, 100.0
        return self._compute_cpu_percentages(previous, current)

    def _read_memory_usage_mb(self) -> tuple[float, float]:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            meminfo[key] = float(value.strip().split()[0])
        total_mb = meminfo.get("MemTotal", 0.0) / 1024.0
        available_mb = meminfo.get("MemAvailable", 0.0) / 1024.0
        used_mb = max(0.0, total_mb - available_mb)
        return round(available_mb, 2), round(used_mb, 2)

    def capture_once(self) -> dict[str, float | str]:
        timestamp = datetime.now().isoformat()
        user, system, idle = self._read_cpu_percentages()
        mem_free_mb, mem_used_mb = self._read_memory_usage_mb()
        cpu_row = [timestamp, user, system, idle]
        with (self.output_dir / "cpu_status.csv").open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(cpu_row)
        with (self.output_dir / "mem_status.csv").open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([timestamp, mem_free_mb, mem_used_mb])
        return {
            "timestamp": timestamp,
            "summary_util_user": user,
            "summary_util_sys": system,
            "summary_util_idle": idle,
            "mem_free_mb": mem_free_mb,
            "mem_used_mb": mem_used_mb,
        }

    def start_background_sampling(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sampling_loop, name="resource-monitor", daemon=True)
        self._thread.start()

    def stop_background_sampling(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.sample_interval_seconds * 4))

    def _sampling_loop(self) -> None:
        while not self._stop_event.is_set():
            self.capture_once()
            if self._stop_event.wait(self.sample_interval_seconds):
                break
