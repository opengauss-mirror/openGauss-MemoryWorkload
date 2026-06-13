from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CsvWriter:
    path: Path
    headers: list[str]

    def create(self) -> None:
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(self.headers)


class ResourceMonitor:
    def __init__(self, output_dir: Path, work_dir: Path, disk_mount: str, net_interface: str):
        self.output_dir = output_dir
        self.work_dir = work_dir
        self.disk_mount = disk_mount
        self.net_interface = net_interface

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
