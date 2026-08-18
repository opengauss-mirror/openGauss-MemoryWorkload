"""Helpers for reading OpenClaw/OpenViking runtime configuration snapshots."""

from __future__ import annotations

from typing import Any


def parse_openclaw_config(data: dict[str, Any]) -> dict[str, Any]:
    gateway = data.get("gateway", {}) if isinstance(data, dict) else {}
    auth = gateway.get("auth", {}) if isinstance(gateway, dict) else {}
    return {
        "gateway_port": gateway.get("port"),
        "gateway_token": auth.get("token", ""),
        "state_dir": data.get("stateDir") or "/root/.openclaw",
    }


def parse_openviking_config(data: dict[str, Any]) -> dict[str, Any]:
    server = data.get("server", {}) if isinstance(data, dict) else {}
    vlm = data.get("vlm", {}) if isinstance(data, dict) else {}
    return {
        "port": server.get("port"),
        "root_api_key": server.get("root_api_key", ""),
        "judge_base_url": vlm.get("api_base", ""),
        "judge_model": vlm.get("model", ""),
    }
