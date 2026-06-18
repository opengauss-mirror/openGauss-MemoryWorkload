import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "tools" / "test_entrypoints" / "prepare_remote_locomo_runtime.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_remote_locomo_runtime", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_inject_openclaw_provider_config_backfills_minimax_models_when_provider_exists():
    config = {
        "models": {
            "providers": {
                "minimax": {
                    "apiKey": "existing-key",
                    "baseUrl": "https://api.minimaxi.com/v1",
                    "api": "openai-completions",
                    "models": [],
                }
            }
        }
    }

    MODULE._inject_openclaw_provider_config(config, "minimax/MiniMax-M3", auth_profiles=None)

    provider = config["models"]["providers"]["minimax"]
    assert provider["apiKey"] == "existing-key"
    assert provider["models"]
    assert provider["models"][0]["id"] == "MiniMax-M3"


def test_enable_benchmark_plugin_diagnostics_is_idempotent():
    shell_text = (
        'cfg["userId"] = user_id\n'
        'cfg["accountId"] = account_id\n'
        'cfg.pop("agent_prefix", None)\n'
    )

    updated = MODULE._enable_benchmark_plugin_diagnostics(shell_text)
    updated_twice = MODULE._enable_benchmark_plugin_diagnostics(updated)

    assert 'cfg["emitStandardDiagnostics"] = True\n' in updated
    assert 'cfg["logFindRequests"] = True\n' in updated
    assert updated == updated_twice
