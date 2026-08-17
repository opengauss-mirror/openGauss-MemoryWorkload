from __future__ import annotations

import tarfile

from skills.benchmarks.locomo.tooling.test_entrypoints.build_remote_runtime_bundle import (
    build_bundle,
)


def test_remote_runtime_bundle_contains_installed_runtime_layout(tmp_path):
    bundle = tmp_path / "runtime.tar.gz"
    build_bundle(bundle)

    with tarfile.open(bundle, "r:gz") as archive:
        names = set(archive.getnames())

    assert "locomo_test/locomo_test/cli.py" in names
    assert "locomo_test/configs/openviking-small-stable.toml" in names
    assert "memory_bench_platform/memory_bench_platform/cli.py" in names
    assert "memory_bench_platform/skills/benchmarks/locomo/manifest.yaml" in names
    assert "memory_bench_platform/schemas/benchmark-manifest.schema.json" in names
