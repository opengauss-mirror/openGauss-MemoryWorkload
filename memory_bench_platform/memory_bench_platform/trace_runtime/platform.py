from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from memory_bench_platform.protocol import (
    TraceChannelConfig,
    TraceRuntimeConfig,
    TraceRuntimeSummary,
)

from .bundle import load_bundle
from .runtime import TraceBindings


DEFAULT_MATCHING = {
    "openai-chat-completions": ("fingerprint", "fingerprint"),
    "openai-responses": ("ordered", "session"),
    "openai-embeddings": ("strict", "fingerprint"),
    "memory-http": ("ordered", "session"),
    "openclaw-session": ("ordered", "session"),
}


def _parse_named_values(values: list[str], option: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"invalid {option} value: {item!r}; expected channel=value")
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            raise ValueError(f"invalid {option} value: {item!r}; expected channel=value")
        if name in parsed:
            raise ValueError(f"duplicate {option} channel: {name}")
        parsed[name] = value
    return parsed


def _parse_named_lists(values: list[str], option: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"invalid {option} value: {item!r}; expected channel=value")
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            raise ValueError(f"invalid {option} value: {item!r}; expected channel=value")
        parsed.setdefault(name, []).append(value)
    return parsed


def _channel_from_spec(spec: str) -> TraceChannelConfig:
    parts = spec.split(":")
    protocol = parts[0]
    if protocol not in DEFAULT_MATCHING:
        raise ValueError(f"unsupported trace protocol: {protocol}")
    default_match, default_scope = DEFAULT_MATCHING[protocol]
    if len(parts) > 3:
        raise ValueError(f"invalid --trace-channel spec: {spec!r}")
    return TraceChannelConfig(
        protocol=protocol,
        match_mode=parts[1] if len(parts) >= 2 and parts[1] else default_match,
        order_scope=parts[2] if len(parts) >= 3 and parts[2] else default_scope,
    )


def build_trace_runtime_config(
    args: Any,
    run_contract: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> TraceRuntimeConfig | None:
    mode = str(getattr(args, "trace_mode", "off") or "off")
    if mode == "off":
        return None
    deployment = str(getattr(args, "trace_deployment", "managed") or "managed")
    channel_specs = _parse_named_values(
        list(getattr(args, "trace_channel", []) or []), "--trace-channel"
    )
    endpoints = _parse_named_values(
        list(getattr(args, "trace_endpoint", []) or []), "--trace-endpoint"
    )
    upstreams = _parse_named_values(
        list(getattr(args, "trace_upstream", []) or []), "--trace-upstream"
    )
    redact_pointers = _parse_named_lists(
        list(getattr(args, "trace_redact_json_pointer", []) or []),
        "--trace-redact-json-pointer",
    )
    channels = {name: _channel_from_spec(spec) for name, spec in channel_specs.items()}

    bundle_path = getattr(args, "trace_bundle", None)
    if not channels and bundle_path and mode.startswith("replay-"):
        bundle = load_bundle(Path(bundle_path))
        channels = {
            name: TraceChannelConfig(
                protocol=channel.protocol,
                match_mode=channel.match_mode,
                order_scope=channel.order_scope,
            )
            for name, channel in bundle.manifest.channels.items()
        }
    if not channels and mode == "capture":
        dependencies = run_contract.get("model_dependencies", {})
        if isinstance(dependencies, Mapping):
            for name, dependency in dependencies.items():
                if not isinstance(dependency, Mapping):
                    continue
                protocol = str(dependency.get("protocol") or "")
                if protocol not in DEFAULT_MATCHING:
                    continue
                default_match, default_scope = DEFAULT_MATCHING[protocol]
                channels[str(name)] = TraceChannelConfig(
                    protocol=protocol,
                    match_mode=default_match,
                    order_scope=default_scope,
                )
    if not channels:
        raise ValueError("trace runtime requires at least one channel")

    environment = environ or os.environ
    dependencies = run_contract.get("model_dependencies", {})
    for name, channel in list(channels.items()):
        dependency = dependencies.get(name, {}) if isinstance(dependencies, Mapping) else {}
        base_url_env = str(dependency.get("base_url_env") or "") if isinstance(dependency, Mapping) else ""
        upstream = upstreams.get(name) or (str(environment.get(base_url_env) or "") if base_url_env else "")
        endpoint = endpoints.get(name)
        channels[name] = TraceChannelConfig.model_validate(
            {
                **channel.model_dump(),
                "upstream_base_url": upstream or None,
                "listen_url": endpoint or None,
                "redact_json_pointers": redact_pointers.get(name, []),
            }
        )
    unknown_endpoints = sorted(set(endpoints) - set(channels))
    unknown_upstreams = sorted(set(upstreams) - set(channels))
    unknown_redact_channels = sorted(set(redact_pointers) - set(channels))
    if unknown_endpoints:
        raise ValueError("trace endpoints reference unknown channels: " + ", ".join(unknown_endpoints))
    if unknown_upstreams:
        raise ValueError("trace upstreams reference unknown channels: " + ", ".join(unknown_upstreams))
    if unknown_redact_channels:
        raise ValueError(
            "trace redaction rules reference unknown channels: "
            + ", ".join(unknown_redact_channels)
        )
    if deployment == "external":
        missing = [name for name, channel in channels.items() if not channel.listen_url]
        if missing:
            raise ValueError("external trace channels require --trace-endpoint: " + ", ".join(missing))

    return TraceRuntimeConfig(
        mode=mode,
        deployment=deployment,
        profile_id=str(getattr(args, "trace_profile", "openai-compatible@1")),
        bundle_path=bundle_path,
        output_path=getattr(args, "trace_output", None),
        copies=int(getattr(args, "trace_copies", 1)),
        delay_scale=float(getattr(args, "trace_delay_scale", 1.0)),
        channels=channels,
    )


def validate_trace_protocols(
    config: TraceRuntimeConfig,
    supported_protocols: set[str],
) -> None:
    unsupported = sorted(
        {
            channel.protocol
            for channel in config.channels.values()
            if channel.protocol not in supported_protocols
        }
    )
    if unsupported:
        raise ValueError(
            "trace protocols are not provided by installed Trace Skills: "
            + ", ".join(unsupported)
        )


def build_trace_environment(
    run_contract: Mapping[str, Any],
    bindings: TraceBindings,
    *,
    run_id: str,
) -> dict[str, str]:
    environment = dict(bindings.environment)
    environment["TRACE_RUN_ID"] = run_id
    dependencies = run_contract.get("model_dependencies", {})
    for name, endpoint in bindings.endpoints.items():
        dependency = dependencies.get(name, {}) if isinstance(dependencies, Mapping) else {}
        if isinstance(dependency, Mapping):
            env_name = str(dependency.get("base_url_env") or "")
            if env_name:
                protocol = str(dependency.get("protocol") or "")
                append_v1 = bool(dependency.get("append_v1", True))
                if (
                    protocol.startswith("openai-")
                    and append_v1
                    and not endpoint.rstrip("/").endswith("/v1")
                ):
                    endpoint = endpoint.rstrip("/") + "/v1"
                environment[env_name] = endpoint
    return environment


def sanitized_trace_config(config: TraceRuntimeConfig) -> dict[str, Any]:
    payload = config.model_dump(mode="json")
    for channel in payload["channels"].values():
        for field in ("upstream_base_url", "listen_url"):
            value = channel.get(field)
            if value:
                parts = urlsplit(value)
                host = parts.hostname or ""
                if parts.port:
                    host = f"{host}:{parts.port}"
                channel[field] = urlunsplit((parts.scheme, host, parts.path, "", ""))
    return payload


def trace_metrics(summary: TraceRuntimeSummary, run_id: str) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for channel, snapshot in summary.channels.items():
        for field in (
            "loaded",
            "matched",
            "mismatched",
            "remaining",
            "errors",
            "active",
            "queued",
            "peak_active",
        ):
            metrics.append(
                {
                    "metric_id": f"{run_id}-trace-{channel}-{field.replace('_', '-')}",
                    "run_id": run_id,
                    "case_id": None,
                    "step_id": None,
                    "scope": "run",
                    "name": f"trace.requests.{field}",
                    "value": getattr(snapshot, field),
                    "unit": "count",
                    "dimension": {
                        "channel": channel,
                        "model_dependency_mode": summary.mode,
                    },
                }
            )
    return metrics


def trace_verification(summary: TraceRuntimeSummary) -> dict[str, Any]:
    reasons: list[str] = []
    for channel, snapshot in summary.channels.items():
        if snapshot.mismatched:
            reasons.append(f"{channel}:mismatched={snapshot.mismatched}")
        if snapshot.remaining:
            reasons.append(f"{channel}:remaining={snapshot.remaining}")
        if snapshot.errors:
            reasons.append(f"{channel}:errors={snapshot.errors}")
        if snapshot.active:
            reasons.append(f"{channel}:active={snapshot.active}")
    return {"valid": summary.valid, "reasons": reasons}
