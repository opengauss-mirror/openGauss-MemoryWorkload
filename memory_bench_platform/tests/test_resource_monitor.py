from pathlib import Path
import csv
import time

from memory_bench_platform.resource_monitor import ResourceMonitor


def test_resource_monitor_prepares_clusterbench_style_csv_files(tmp_path: Path):
    monitor = ResourceMonitor(output_dir=tmp_path, work_dir=tmp_path, disk_mount="root", net_interface="lo")
    monitor.setup_writers()
    assert (tmp_path / "cpu_status.csv").exists()
    assert (tmp_path / "mem_status.csv").exists()
    assert (tmp_path / "disk_status.csv").exists()
    assert (tmp_path / "net_status.csv").exists()
    snapshot = monitor.capture_once()
    assert 0.0 <= float(snapshot["summary_util_idle"]) <= 100.0
    assert "mem_used_mb" in snapshot
    assert "timestamp" in snapshot


def test_resource_monitor_background_sampling_writes_multiple_rows(tmp_path: Path):
    monitor = ResourceMonitor(
        output_dir=tmp_path,
        work_dir=tmp_path,
        disk_mount="root",
        net_interface="lo",
        sample_interval_seconds=0.01,
    )
    monitor.setup_writers()

    monitor.start_background_sampling()
    time.sleep(0.05)
    monitor.stop_background_sampling()

    rows = list(csv.DictReader((tmp_path / "cpu_status.csv").open(encoding="utf-8")))
    assert len(rows) >= 2
