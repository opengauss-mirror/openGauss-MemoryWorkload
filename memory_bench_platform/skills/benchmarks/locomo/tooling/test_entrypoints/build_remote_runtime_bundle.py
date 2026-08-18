from __future__ import annotations

import argparse
import shutil
import tarfile
import tempfile
from pathlib import Path

import memory_bench_platform
import schemas
import skills


def _locomo_package_root() -> Path:
    try:
        import locomo_test
    except ImportError:
        locomo_test = None
    module_file = getattr(locomo_test, "__file__", None)
    if module_file:
        return Path(module_file).resolve().parent
    source_package = (
        Path(__file__).resolve().parents[6] / "locomo_test" / "locomo_test"
    )
    if source_package.joinpath("__init__.py").is_file():
        return source_package
    raise RuntimeError(
        "locomo-test-kit is required for LoCoMo external runners; "
        "install memory-bench-platform[locomo]"
    )


def _locomo_configs_root() -> Path:
    package_root = _locomo_package_root()
    packaged_configs = package_root / "configs"
    if packaged_configs.is_dir():
        return packaged_configs
    source_configs = package_root.parent / "configs"
    if source_configs.is_dir():
        return source_configs
    raise RuntimeError("locomo-test configuration files are unavailable")


def build_bundle(output_path: Path) -> None:
    """Build the source layout expected by the remote LoCoMo runner."""
    locomo_package_root = _locomo_package_root()
    with tempfile.TemporaryDirectory(prefix="locomo-runtime-bundle-") as tmp:
        staging = Path(tmp)
        locomo_root = staging / "locomo_test"
        platform_root = staging / "memory_bench_platform"
        locomo_root.mkdir()
        platform_root.mkdir()

        shutil.copytree(locomo_package_root, locomo_root / "locomo_test")
        shutil.copytree(
            _locomo_configs_root(),
            locomo_root / "configs",
        )
        shutil.copytree(
            Path(memory_bench_platform.__file__).resolve().parent,
            platform_root / "memory_bench_platform",
        )
        shutil.copytree(Path(skills.__file__).resolve().parent, platform_root / "skills")
        shutil.copytree(Path(schemas.__file__).resolve().parent, platform_root / "schemas")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output_path, "w:gz") as archive:
            archive.add(locomo_root, arcname="locomo_test")
            archive.add(platform_root, arcname="memory_bench_platform")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    build_bundle(Path(args.output))


if __name__ == "__main__":
    main()
