from pathlib import Path

import pytest

from memory_bench_platform.loader import load_all_skills


def test_load_all_skills_reads_benchmark_and_agent_manifests(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "benchmarks" / "locomo").mkdir(parents=True)
    (skills / "agents" / "generic-cli").mkdir(parents=True)
    (skills / "benchmarks" / "locomo" / "manifest.yaml").write_text(
        "kind: benchmark\nid: locomo\nversion: 0.1.0\nversion_policy:\n  default_selection: latest_official_release_tag\n  resolution_order:\n    - user_specified_official_version\n    - latest_official_release_tag\n  allowed_overrides:\n    - user_specified_official_version\n  disallowed_defaults:\n    - dirty_worktree\n  targets:\n    - name: locomo-benchmark\n      scope: benchmark_tooling\n      version_source: upstream_release_tag\n      upstream: https://example.com/locomo\n  record_runtime_version: true\nentry:\n  case_builder: scripts/build_tasks.py\n",
        encoding="utf-8",
    )
    (skills / "agents" / "generic-cli" / "manifest.yaml").write_text(
        "kind: agent\nid: generic-cli\nversion: 0.1.0\nversion_policy:\n  default_selection: latest_official_release_tag\n  resolution_order:\n    - user_specified_official_version\n    - latest_official_release_tag\n  allowed_overrides:\n    - user_specified_official_version\n  disallowed_defaults:\n    - dirty_worktree\n  targets:\n    - name: generic-cli\n      scope: system_under_test\n      version_source: runtime_observed_only\n  record_runtime_version: true\nentry:\n  runner: scripts/run_task.py\n",
        encoding="utf-8",
    )
    loaded = load_all_skills(skills)
    assert loaded["benchmarks"][0].id == "locomo"
    assert loaded["benchmarks"][0].entry.case_builder == "scripts/build_tasks.py"
    assert loaded["benchmarks"][0].version_policy.targets[0].name == "locomo-benchmark"
    assert loaded["agents"][0].id == "generic-cli"
    assert loaded["agents"][0].version_policy.targets[0].scope == "system_under_test"


def test_load_all_skills_requires_explicit_version_policy(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "benchmarks" / "locomo").mkdir(parents=True)
    (skills / "benchmarks" / "locomo" / "manifest.yaml").write_text(
        "kind: benchmark\nid: locomo\nversion: 0.1.0\nentry:\n  case_builder: scripts/build_tasks.py\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="version_policy"):
        load_all_skills(skills)


def test_load_all_skills_requires_upstream_for_release_tag_targets(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "benchmarks" / "locomo").mkdir(parents=True)
    (skills / "benchmarks" / "locomo" / "manifest.yaml").write_text(
        "kind: benchmark\nid: locomo\nversion: 0.1.0\nversion_policy:\n  default_selection: latest_official_release_tag\n  resolution_order:\n    - latest_official_release_tag\n  allowed_overrides:\n    - user_specified_official_version\n  disallowed_defaults:\n    - dirty_worktree\n  targets:\n    - name: locomo-benchmark\n      scope: benchmark_tooling\n      version_source: upstream_release_tag\n  record_runtime_version: true\nentry:\n  case_builder: scripts/build_tasks.py\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="declare upstream explicitly"):
        load_all_skills(skills)
