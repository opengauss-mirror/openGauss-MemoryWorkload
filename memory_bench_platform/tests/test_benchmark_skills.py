from pathlib import Path

import yaml


def test_locomo_manifest_marks_multi_turn_stateful_execution():
    manifest = yaml.safe_load(
        Path("skills/benchmarks/locomo/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["execution"]["mode"] == "multi_turn"
    assert manifest["execution"]["requires_stateful_agent"] is True


def test_longmemeval_manifest_declares_task_builder():
    manifest = yaml.safe_load(
        Path("skills/benchmarks/longmemeval/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["entry"]["task_builder"] == "scripts/build_tasks.py"
