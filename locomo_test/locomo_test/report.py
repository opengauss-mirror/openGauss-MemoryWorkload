from __future__ import annotations

from pathlib import Path

from memory_bench_platform.locomo_test_report_bridge import (
    render_locomo_test_html_report,
    write_locomo_test_html_report,
)


def render_html_report(output_dir: Path) -> str:
    return render_locomo_test_html_report(Path(output_dir))


def write_html_report(output_dir: Path) -> Path:
    return write_locomo_test_html_report(Path(output_dir))
