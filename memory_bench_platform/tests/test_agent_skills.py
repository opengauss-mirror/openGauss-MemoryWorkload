from pathlib import Path

import yaml


def test_openclaw_manifest_declares_service_runtime():
    manifest = yaml.safe_load(
        Path("skills/agents/openclaw/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["runtime"]["mode"] == "service"
    assert manifest["io"]["protocol_mode"] == "stateful_session"


def test_generic_cli_manifest_declares_process_runtime():
    manifest = yaml.safe_load(
        Path("skills/agents/generic-cli/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["runtime"]["mode"] == "process"
    assert manifest["io"]["protocol_mode"] == "stateless_cli"


def test_hermes_manifest_declares_process_runtime():
    manifest = yaml.safe_load(
        Path("skills/agents/hermes/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["runtime"]["mode"] == "process"
    assert manifest["io"]["protocol_mode"] == "stateless_cli"
