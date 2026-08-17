"""Helpers for collecting benchmark run artifacts into a stable directory."""

from __future__ import annotations

from pathlib import Path
import shutil


def collect_artifacts(output_dir: Path, artifact_paths: list[Path]) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    collected: list[Path] = []
    for artifact in artifact_paths:
        if not artifact.exists():
            continue
        target = output_dir / artifact.name
        if artifact.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(artifact, target)
        else:
            shutil.copy2(artifact, target)
        collected.append(target)
    return collected
