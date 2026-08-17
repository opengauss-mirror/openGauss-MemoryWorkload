import importlib.util
import json
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "benchmarks"
    / "locomo"
    / "tooling"
    / "test_entrypoints"
    / "swap_remote_locomo_model.py"
)
SPEC = importlib.util.spec_from_file_location("swap_remote_locomo_model", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_backup_and_switch_updates_target_agent_model(tmp_path: Path):
    config = tmp_path / "openclaw.json"
    config.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {"id": "main", "model": "openai/gpt-4.1"},
                        {"id": "locomo-eval", "model": "volcengine/doubao-seed-2.0-pro"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    backup = MODULE.backup_and_switch(config, "locomo-eval", "zai/glm-4.7-flash")
    assert backup.exists()
    payload = json.loads(config.read_text(encoding="utf-8"))
    models = {item["id"]: item["model"] for item in payload["agents"]["list"]}
    assert models["locomo-eval"] == "zai/glm-4.7-flash"
    assert models["main"] == "openai/gpt-4.1"


def test_restore_backup_restores_original_file(tmp_path: Path):
    config = tmp_path / "openclaw.json"
    config.write_text('{"agents":{"list":[{"id":"locomo-eval","model":"a/b"}]}}', encoding="utf-8")
    backup = MODULE.backup_and_switch(config, "locomo-eval", "c/d")
    MODULE.restore_backup(config, backup)
    assert json.loads(config.read_text(encoding="utf-8"))["agents"]["list"][0]["model"] == "a/b"
