from pathlib import Path

import yaml


def test_openclaw_manifest_declares_service_runtime():
    manifest = yaml.safe_load(
        Path("skills/agents/openclaw/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["runtime"]["mode"] == "service"
    assert manifest["io"]["protocol_mode"] == "stateful_session"
    assert manifest["version_policy"]["default_selection"] == "latest_official_release_tag"
    assert manifest["version_policy"]["resolution_order"][1] == "latest_official_release_tag"
    assert manifest["version_policy"]["targets"][0]["name"] == "openclaw"
    assert manifest["version_policy"]["targets"][1]["name"] == "openviking"
    assert manifest["version_policy"]["targets"][0]["upstream"] == "https://github.com/coding-guy/openclaw"
    assert manifest["version_policy"]["targets"][1]["upstream"] == "https://github.com/xforce-io/openviking"


def test_generic_cli_manifest_declares_process_runtime():
    manifest = yaml.safe_load(
        Path("skills/agents/generic-cli/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["runtime"]["mode"] == "process"
    assert manifest["io"]["protocol_mode"] == "stateless_cli"
    assert manifest["version_policy"]["default_selection"] == "latest_official_release_tag"
    assert manifest["version_policy"]["targets"][0]["name"] == "generic-cli"


def test_hermes_manifest_declares_process_runtime():
    manifest = yaml.safe_load(
        Path("skills/agents/hermes/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["runtime"]["mode"] == "process"
    assert manifest["io"]["protocol_mode"] == "stateless_cli"
    assert manifest["version_policy"]["default_selection"] == "latest_official_release_tag"
    assert manifest["version_policy"]["targets"][0]["name"] == "hermes"
    assert manifest["version_policy"]["targets"][0]["upstream"] == "https://github.com/Integuru-AI/hermes-agent"
