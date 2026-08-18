from memory_bench_platform.adapters.locomo.artifacts import load_locomo_test_artifacts
from memory_bench_platform.adapters.locomo.diagnostics import diagnose_locomo_test_output
from memory_bench_platform.adapters.locomo.metrics_bridge import (
    summarize_locomo_qa_results,
)
from memory_bench_platform.adapters.locomo.report_bridge import write_locomo_test_html_report
from memory_bench_platform.adapters.locomo.runtime import bootstrap_locomo_openclaw_runtime
from memory_bench_platform.adapters.locomo.timing import build_locomo_test_timing_report


def test_locomo_adapter_namespace_exports_current_bridge_entrypoints():
    assert callable(load_locomo_test_artifacts)
    assert callable(diagnose_locomo_test_output)
    assert callable(summarize_locomo_qa_results)
    assert callable(write_locomo_test_html_report)
    assert callable(bootstrap_locomo_openclaw_runtime)
    assert callable(build_locomo_test_timing_report)
