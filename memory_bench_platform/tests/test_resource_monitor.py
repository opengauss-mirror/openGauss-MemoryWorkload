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


def test_resource_monitor_cpu_sampling_uses_delta_between_snapshots(tmp_path: Path):
    monitor = ResourceMonitor(output_dir=tmp_path, work_dir=tmp_path, disk_mount="root", net_interface="lo")
    monitor.setup_writers()
    counters = iter(
        [
            (100.0, 0.0, 50.0, 850.0, 0.0, 0.0, 0.0),
            (120.0, 0.0, 70.0, 910.0, 0.0, 0.0, 0.0),
            (160.0, 0.0, 90.0, 930.0, 0.0, 0.0, 0.0),
        ]
    )
    monitor._read_cpu_counters = lambda: next(counters)  # type: ignore[method-assign]

    first = monitor.capture_once()
    second = monitor.capture_once()
    third = monitor.capture_once()

    assert float(first["summary_util_user"]) == 0.0
    assert float(first["summary_util_idle"]) == 100.0
    assert float(second["summary_util_user"]) > 0.0
    assert float(second["summary_util_idle"]) < 100.0
    assert float(third["summary_util_user"]) != float(second["summary_util_user"])


def test_resource_monitor_disk_and_network_sampling_use_deltas(tmp_path: Path):
    monitor = ResourceMonitor(output_dir=tmp_path, work_dir=tmp_path, disk_mount="root", net_interface="lo")
    monitor.setup_writers()
    disk_counters = iter(
        [
            (1000.0, 2000.0),
            (2000.0, 2600.0),
            (2600.0, 3600.0),
        ]
    )
    net_counters = iter(
        [
            (100.0, 50.0, 10000.0, 5000.0),
            (140.0, 60.0, 16000.0, 7000.0),
            (180.0, 90.0, 25000.0, 12000.0),
        ]
    )
    time_points = iter([10.0, 11.0, 13.0])
    monitor._read_disk_counters = lambda: next(disk_counters)  # type: ignore[method-assign]
    monitor._read_network_counters = lambda: next(net_counters)  # type: ignore[method-assign]

    original_monotonic = time.monotonic
    time.monotonic = lambda: next(time_points)  # type: ignore[assignment]
    try:
        first = monitor.capture_once()
        second = monitor.capture_once()
        third = monitor.capture_once()
    finally:
        time.monotonic = original_monotonic  # type: ignore[assignment]

    assert float(first["disk_bw_mb"]) == 0.0
    assert float(first["recv_bytes_rate"]) == 0.0
    assert float(second["disk_bw_mb"]) > 0.0
    assert float(second["recv_bytes_rate"]) > 0.0
    assert float(third["disk_bw_mb"]) != float(second["disk_bw_mb"])
    assert float(third["sent_pcks_rate"]) != float(second["sent_pcks_rate"])
