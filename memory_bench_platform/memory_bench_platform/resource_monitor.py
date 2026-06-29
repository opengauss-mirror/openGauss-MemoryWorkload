from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
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
        self._last_disk_counters: tuple[float, float] | None = None
        self._last_net_counters: tuple[float, float, float, float] | None = None
        self._last_io_timestamp: float | None = None

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

    def _read_disk_counters(self) -> tuple[float, float]:
        read_bytes = 0.0
        write_bytes = 0.0
        device_prefixes = ("sd", "vd", "xvd", "nvme", "md")
        for line in Path("/proc/diskstats").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 14:
                continue
            name = parts[2]
            if not name.startswith(device_prefixes):
                continue
            if name.startswith("nvme") and "p" in name:
                continue
            if (name.startswith("sd") or name.startswith("vd") or name.startswith("xvd")) and any(ch.isdigit() for ch in name[2:]):
                continue
            read_sectors = float(parts[5])
            write_sectors = float(parts[9])
            read_bytes += read_sectors * 512.0
            write_bytes += write_sectors * 512.0
        return read_bytes, write_bytes

    def _read_disk_snapshot(self, elapsed_seconds: float | None) -> tuple[float, float, float, float]:
        read_bytes, write_bytes = self._read_disk_counters()
        previous = self._last_disk_counters
        self._last_disk_counters = (read_bytes, write_bytes)
        read_bw_mb = 0.0
        write_bw_mb = 0.0
        if previous is not None and elapsed_seconds and elapsed_seconds > 0:
            read_bw_mb = max(0.0, read_bytes - previous[0]) / elapsed_seconds / (1024.0 * 1024.0)
            write_bw_mb = max(0.0, write_bytes - previous[1]) / elapsed_seconds / (1024.0 * 1024.0)
        disk_free_mb = shutil.disk_usage(self.work_dir).free / (1024.0 * 1024.0)
        return round(read_bw_mb, 4), round(write_bw_mb, 4), round(read_bw_mb + write_bw_mb, 4), round(disk_free_mb, 2)

    def _read_network_counters(self) -> tuple[float, float, float, float]:
        for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
            if ":" not in line:
                continue
            iface, payload = line.split(":", 1)
            if iface.strip() != self.net_interface:
                continue
            parts = payload.split()
            if len(parts) < 16:
                break
            recv_bytes = float(parts[0])
            recv_packets = float(parts[1])
            sent_bytes = float(parts[8])
            sent_packets = float(parts[9])
            return recv_packets, sent_packets, recv_bytes, sent_bytes
        return 0.0, 0.0, 0.0, 0.0

    def _read_network_snapshot(self, elapsed_seconds: float | None) -> tuple[float, float, float, float]:
        recv_packets, sent_packets, recv_bytes, sent_bytes = self._read_network_counters()
        previous = self._last_net_counters
        self._last_net_counters = (recv_packets, sent_packets, recv_bytes, sent_bytes)
        recv_pcks_rate = 0.0
        sent_pcks_rate = 0.0
        recv_bytes_rate = 0.0
        sent_bytes_rate = 0.0
        if previous is not None and elapsed_seconds and elapsed_seconds > 0:
            recv_pcks_rate = max(0.0, recv_packets - previous[0]) / elapsed_seconds
            sent_pcks_rate = max(0.0, sent_packets - previous[1]) / elapsed_seconds
            recv_bytes_rate = max(0.0, recv_bytes - previous[2]) / elapsed_seconds
            sent_bytes_rate = max(0.0, sent_bytes - previous[3]) / elapsed_seconds
        return (
            round(recv_pcks_rate, 4),
            round(sent_pcks_rate, 4),
            round(recv_bytes_rate, 4),
            round(sent_bytes_rate, 4),
        )

    def capture_once(self) -> dict[str, float | str]:
        timestamp = datetime.now().isoformat()
        now = time.monotonic()
        elapsed_seconds = None if self._last_io_timestamp is None else max(0.0, now - self._last_io_timestamp)
        self._last_io_timestamp = now
        user, system, idle = self._read_cpu_percentages()
        mem_free_mb, mem_used_mb = self._read_memory_usage_mb()
        read_bw_mb, write_bw_mb, disk_bw_mb, disk_free_mb = self._read_disk_snapshot(elapsed_seconds)
        recv_pcks_rate, sent_pcks_rate, recv_bytes_rate, sent_bytes_rate = self._read_network_snapshot(elapsed_seconds)
        cpu_row = [timestamp, user, system, idle]
        with (self.output_dir / "cpu_status.csv").open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(cpu_row)
        with (self.output_dir / "mem_status.csv").open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([timestamp, mem_free_mb, mem_used_mb])
        with (self.output_dir / "disk_status.csv").open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([timestamp, read_bw_mb, write_bw_mb, disk_bw_mb, disk_free_mb])
        with (self.output_dir / "net_status.csv").open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([timestamp, recv_pcks_rate, sent_pcks_rate, recv_bytes_rate, sent_bytes_rate])
        return {
            "timestamp": timestamp,
            "summary_util_user": user,
            "summary_util_sys": system,
            "summary_util_idle": idle,
            "mem_free_mb": mem_free_mb,
            "mem_used_mb": mem_used_mb,
            "read_bw_mb": read_bw_mb,
            "write_bw_mb": write_bw_mb,
            "disk_bw_mb": disk_bw_mb,
            "disk_free_mb": disk_free_mb,
            "recv_pcks_rate": recv_pcks_rate,
            "sent_pcks_rate": sent_pcks_rate,
            "recv_bytes_rate": recv_bytes_rate,
            "sent_bytes_rate": sent_bytes_rate,
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
