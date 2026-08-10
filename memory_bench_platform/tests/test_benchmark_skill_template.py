from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

from memory_bench_platform.benchmark_scenario import BenchmarkScenario
from memory_bench_platform.manifests import BenchmarkManifest


TEMPLATE = Path("templates/benchmark-skill")


def test_template_builder_matches_golden_and_contains_no_runtime_actions():
    script = TEMPLATE / "scripts/build_scenario.py"
    spec = importlib.util.spec_from_file_location("benchmark_template_builder", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    actual = module.build(TEMPLATE / "tests/golden/source_sample.json")
    expected = json.loads(
        (TEMPLATE / "tests/golden/expected_scenario.json").read_text(encoding="utf-8")
    )
    assert actual == expected
    BenchmarkScenario.model_validate(actual)

    serialized = json.dumps(actual).lower()
    for runtime_action in ("set_phase", "commit", "wait_ready", "openclaw", "openviking"):
        assert runtime_action not in serialized


def test_template_manifest_is_a_valid_copyable_manifest():
    payload = yaml.safe_load((TEMPLATE / "manifest.yaml").read_text(encoding="utf-8"))
    manifest = BenchmarkManifest.model_validate(payload)
    assert manifest.entry.scenario_builder == "scripts/build_scenario.py"
