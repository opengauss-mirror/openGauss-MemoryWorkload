from pathlib import Path

import pytest
from pydantic import ValidationError

from memory_bench_platform.protocol import TraceChannelConfig, TraceRuntimeConfig


def test_capture_requires_output_path(tmp_path: Path):
    with pytest.raises(ValidationError, match="output_path"):
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            channels={
                "memory_extract": TraceChannelConfig(
                    protocol="openai-chat-completions",
                    upstream_base_url="http://provider.example/v1",
                    match_mode="fingerprint",
                    order_scope="fingerprint",
                )
            },
        )


def test_replay_requires_bundle_path():
    with pytest.raises(ValidationError, match="bundle_path"):
        TraceRuntimeConfig(
            mode="replay-zero-delay",
            profile_id="openai-compatible@1",
            channels={},
        )


def test_capture_channel_requires_upstream(tmp_path: Path):
    with pytest.raises(ValidationError, match="upstream_base_url"):
        TraceRuntimeConfig(
            mode="capture",
            profile_id="openai-compatible@1",
            output_path=str(tmp_path / "bundle"),
            channels={
                "embedding": TraceChannelConfig(
                    protocol="openai-embeddings",
                    match_mode="compat",
                    order_scope="fingerprint",
                )
            },
        )


def test_trace_copies_must_be_positive(tmp_path: Path):
    with pytest.raises(ValidationError):
        TraceRuntimeConfig(
            mode="replay-zero-delay",
            profile_id="openai-compatible@1",
            bundle_path=str(tmp_path / "bundle"),
            copies=0,
            channels={},
        )


def test_trace_delay_scale_must_be_finite(tmp_path: Path):
    with pytest.raises(ValidationError, match="finite"):
        TraceRuntimeConfig(
            mode="replay-with-delay",
            profile_id="openai-compatible@1",
            bundle_path=str(tmp_path / "bundle"),
            delay_scale=float("inf"),
            channels={},
        )
