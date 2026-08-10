from pathlib import Path

import pytest

from memory_bench_platform.integration import build_run_contract, get_memory_manifest, resolve_run_skill_bundle


def test_resolve_run_skill_bundle_uses_agent_default_memory_skill():
    bundle = resolve_run_skill_bundle("locomo", "openclaw")

    assert bundle.benchmark.id == "locomo"
    assert bundle.agent.id == "openclaw"
    assert bundle.memory is not None
    assert bundle.memory.id == "openviking"


def test_build_run_contract_exposes_three_skill_runtime_contract():
    contract = build_run_contract("locomo", "openclaw")

    assert contract["selection"] == {
        "benchmark_id": "locomo",
        "agent_id": "openclaw",
        "memory_id": "openviking",
        "memory_integration": "backend_direct",
        "memory_plugin_id": None,
    }
    assert contract["execution"]["benchmark_ingest_unit"] == "session"
    assert contract["agent_runtime"]["protocol_mode"] == "stateful_session"
    assert contract["memory_runtime"]["enabled"] is True
    assert contract["memory_runtime"]["ingest_benchmark_unit"] == "session"
    assert contract["memory_runtime"]["accept_signal"] == "accepted"
    assert contract["memory_runtime"]["complete_signal"] == "completed"
    assert contract["memory_runtime"]["runner"] == "scripts/run_operation.py"
    assert contract["memory_runtime"]["supported_actions"] == ["ingest", "flush", "status", "recall"]
    assert contract["judge_runtime"] == {
        "mode": "external",
        "type": "llm",
        "prompt_style": "locomo",
        "api_format": "openai",
        "timeout_seconds": 60,
        "env": {
            "api_key": "LOCOMO_API_KEY",
            "base_url": "LOCOMO_BASE_URL",
            "model": "LOCOMO_METRIC_MODEL",
        },
    }


def test_build_run_contract_resolves_openclaw_openviking_plugin():
    contract = build_run_contract(
        "locomo",
        "openclaw",
        "openviking",
        "agent_plugin",
    )

    assert contract["selection"]["memory_integration"] == "agent_plugin"
    assert contract["selection"]["memory_plugin_id"] == "openclaw-openviking"
    assert contract["memory_plugin_runtime"]["enabled"] is True
    assert contract["memory_plugin_runtime"]["runner"] == "scripts/run_lifecycle.py"
    assert contract["memory_plugin_runtime"]["actions"] == [
        "validate",
        "prepare",
        "set_phase",
        "commit",
        "wait_ready",
        "flush",
        "wait_settle",
        "finalize",
    ]
    assert contract["memory_plugin_runtime"]["capabilities"]["explicit_flush"] is True
    assert contract["memory_plugin_runtime"]["capabilities"]["extraction_trigger"] == "agent_native_compact"
    assert contract["memory_plugin_runtime"]["phases"]["ingest"] == {
        "capture": True,
        "recall": False,
        "automatic_commit": False,
    }


def test_openviking_memory_manifest_declares_unified_runner():
    manifest = get_memory_manifest("openviking")

    assert manifest.entry.runner == "scripts/run_operation.py"
    assert manifest.runtime["actions"] == ["ingest", "flush", "status", "recall"]


def test_resolve_run_skill_bundle_rejects_incompatible_memory_unit(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "benchmarks" / "demo").mkdir(parents=True)
    (skills / "agents" / "demo-agent").mkdir(parents=True)
    (skills / "memories" / "demo-memory").mkdir(parents=True)

    (skills / "benchmarks" / "demo" / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: benchmark",
                "id: demo",
                "version: 0.1.0",
                "version_policy:",
                "  default_selection: latest_official_release_tag",
                "  resolution_order:",
                "    - latest_official_release_tag",
                "  allowed_overrides:",
                "    - user_specified_official_version",
                "  disallowed_defaults:",
                "    - dirty_worktree",
                "  targets:",
                "    - name: demo-benchmark",
                "      scope: benchmark_tooling",
                "      version_source: upstream_release_tag",
                "      upstream: https://example.com/demo-benchmark",
                "  record_runtime_version: true",
                "entry:",
                "  case_builder: scripts/build.py",
                "execution:",
                "  requires_stateful_agent: true",
                "  ingest_unit: session",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (skills / "agents" / "demo-agent" / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: agent",
                "id: demo-agent",
                "version: 0.1.0",
                "version_policy:",
                "  default_selection: latest_official_release_tag",
                "  resolution_order:",
                "    - latest_official_release_tag",
                "  allowed_overrides:",
                "    - user_specified_official_version",
                "  disallowed_defaults:",
                "    - dirty_worktree",
                "  targets:",
                "    - name: demo-agent",
                "      scope: system_under_test",
                "      version_source: runtime_observed_only",
                "  record_runtime_version: true",
                "entry:",
                "  runner: scripts/run.py",
                "runtime:",
                "  default_memory_skill: demo-memory",
                "io:",
                "  protocol_mode: stateful_session",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (skills / "memories" / "demo-memory" / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: memory",
                "id: demo-memory",
                "version: 0.1.0",
                "version_policy:",
                "  default_selection: latest_official_release_tag",
                "  resolution_order:",
                "    - latest_official_release_tag",
                "  allowed_overrides:",
                "    - user_specified_official_version",
                "  disallowed_defaults:",
                "    - dirty_worktree",
                "  targets:",
                "    - name: demo-memory",
                "      scope: memory_backend",
                "      version_source: upstream_release_tag",
                "      upstream: https://example.com/demo-memory",
                "  record_runtime_version: true",
                "runtime:",
                "  benchmark_unit: turn",
                "ingest:",
                "  benchmark_unit: turn",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ingest_unit='session'"):
        resolve_run_skill_bundle("demo", "demo-agent", skills_root=skills)
