from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory_bench_platform.cli import build_parser, main
from memory_bench_platform.loader import load_all_skills, load_trace_skill
from memory_bench_platform.manifests import TraceManifest
from memory_bench_platform.planner import RunPlan
from memory_bench_platform.protocol import TraceChannelConfig, TraceRuntimeConfig
from memory_bench_platform.trace_runtime.platform import (
    build_trace_runtime_config,
    validate_trace_protocols,
)


def test_trace_manifests_are_loaded():
    loaded = load_all_skills(Path(__file__).resolve().parents[1] / "skills")
    assert {item.id for item in loaded["traces"]} >= {"openai-chat", "openai-embedding"}
    manifest = load_trace_skill(Path(__file__).resolve().parents[1] / "skills", "openai-chat")
    assert isinstance(manifest, TraceManifest)
    assert "openai-chat-completions" in manifest.protocols


def test_trace_cli_arguments_are_normalized():
    args = build_parser().parse_args(
        [
            "run",
            "--benchmark",
            "locomo",
            "--agent",
            "generic-cli",
            "--trace-mode",
            "replay-zero-delay",
            "--trace-profile",
            "openai-compatible@1",
            "--trace-bundle",
            "/tmp/bundle",
            "--trace-copies",
            "8",
            "--trace-channel",
            "agent_chat=openai-chat-completions:fingerprint:session",
            "--trace-endpoint",
            "agent_chat=http://127.0.0.1:18087/v1",
            "--trace-redact-json-pointer",
            "agent_chat=/metadata/tenant_credential",
        ]
    )
    assert args.trace_mode == "replay-zero-delay"
    assert args.trace_copies == 8
    assert args.trace_channel == ["agent_chat=openai-chat-completions:fingerprint:session"]
    assert args.trace_endpoint == ["agent_chat=http://127.0.0.1:18087/v1"]
    config = build_trace_runtime_config(args, {})
    assert config is not None
    assert config.channels["agent_chat"].redact_json_pointers == [
        "/metadata/tenant_credential"
    ]


def test_list_skills_includes_trace_category(capsys):
    main(["list-skills"])
    payload = json.loads(capsys.readouterr().out)
    assert "traces" in payload
    assert "openai-chat" in payload["traces"]


def test_validate_trace_skill(capsys):
    main(["validate", "--trace", "openai-embedding"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["trace"] == {
        "status": "ok",
        "trace": "openai-embedding",
        "protocols": ["openai-embeddings"],
    }


def test_plan_run_includes_trace_contract(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        "memory_bench_platform.cli._plan_from_args",
        lambda args: RunPlan(
            run_id="trace-plan",
            benchmark_id=args.benchmark,
            agent_id=args.agent,
            benchmark_version=None,
            agent_version=None,
            memory_backend=None,
            memory_integration="backend_direct",
            hardware_profile=None,
            data_path=None,
        ),
    )
    main(
        [
            "plan-run",
            "--benchmark",
            "locomo",
            "--agent",
            "openclaw",
            "--trace-mode",
            "capture",
            "--trace-output",
            str(tmp_path / "bundle"),
            "--trace-channel",
            "memory_extract=openai-chat-completions:fingerprint:fingerprint",
            "--trace-upstream",
            "memory_extract=http://provider.example/v1",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_contract"]["trace_runtime"]["mode"] == "capture"


def test_trace_manifest_requires_supported_protocol():
    with pytest.raises(ValueError):
        TraceManifest(
            id="bad",
            version="1",
            protocols=["unsupported"],
            entry={"server": "scripts/serve.py"},
        )


def test_installed_trace_skills_reject_future_protocols():
    config = TraceRuntimeConfig(
        mode="replay-zero-delay",
        profile_id="openai-compatible@1",
        bundle_path="/tmp/bundle",
        channels={
            "agent_chat": TraceChannelConfig(
                protocol="openai-responses",
                match_mode="ordered",
                order_scope="session",
            )
        },
    )
    with pytest.raises(ValueError, match="openai-responses"):
        validate_trace_protocols(
            config,
            {"openai-chat-completions", "openai-embeddings"},
        )
