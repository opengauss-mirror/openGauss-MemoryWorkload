from pathlib import Path

import yaml
from skills.benchmarks.longmemeval.scripts.build_tasks import build_cases
from skills.benchmarks.longmemeval.scripts.validate import validate


def test_locomo_manifest_marks_multi_turn_stateful_execution():
    manifest = yaml.safe_load(
        Path("skills/benchmarks/locomo/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["execution"]["mode"] == "multi_turn"
    assert manifest["execution"]["requires_stateful_agent"] is True
    assert manifest["version_policy"]["default_selection"] == "latest_official_release_tag"


def test_longmemeval_manifest_declares_case_builder():
    manifest = yaml.safe_load(
        Path("skills/benchmarks/longmemeval/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["entry"]["case_builder"] == "scripts/build_tasks.py"
    assert manifest["version_policy"]["default_selection"] == "latest_official_release_tag"


def test_longmemeval_builder_uses_case_source_shape():
    payload = build_cases()
    assert "cases" in payload
    assert "steps" in payload
    assert "execution_spec" in payload


def test_longmemeval_validator_reports_missing_source_when_no_path():
    payload = validate(None)
    assert payload["status"] == "missing_source"


def test_ovtest_memory_manifest_declares_case_builder():
    manifest = yaml.safe_load(
        Path("skills/benchmarks/ovtest-memory/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["entry"]["case_builder"] == "scripts/build_tasks.py"
    assert manifest["version_policy"]["default_selection"] == "latest_official_release_tag"


def test_ovtest_health_manifest_declares_case_builder():
    manifest = yaml.safe_load(
        Path("skills/benchmarks/ovtest-health/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["entry"]["case_builder"] == "scripts/build_tasks.py"
    assert manifest["version_policy"]["default_selection"] == "latest_official_release_tag"


def test_ovtest_admin_memory_manifest_declares_case_builder():
    manifest = yaml.safe_load(
        Path("skills/benchmarks/ovtest-admin-memory/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["entry"]["case_builder"] == "scripts/build_tasks.py"
    assert manifest["version_policy"]["default_selection"] == "latest_official_release_tag"
