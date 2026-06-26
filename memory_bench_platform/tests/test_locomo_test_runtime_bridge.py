import json
from pathlib import Path

from memory_bench_platform.locomo_test_runtime_bridge import bootstrap_locomo_openclaw_runtime


def test_bootstrap_locomo_openclaw_runtime_writes_openviking_identity_config(tmp_path):
    base_state_dir = tmp_path / "base_state"
    (base_state_dir / "agents" / "main" / "agent").mkdir(parents=True)
    (base_state_dir / "extensions" / "openviking").mkdir(parents=True)
    (base_state_dir / "openviking.env").write_text("", encoding="utf-8")
    (base_state_dir / "agents" / "main" / "agent" / "auth-profiles.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "agents" / "main" / "agent" / "auth-state.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "agents" / "main" / "agent" / "models.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "extensions" / "openviking" / "openclaw.plugin.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "openclaw.json").write_text(
        json.dumps({"gateway": {"auth": {"token": "gw-token"}}, "plugins": {"allow": []}}, ensure_ascii=False),
        encoding="utf-8",
    )
    base_ov_conf = tmp_path / "ov.conf"
    base_ov_conf.write_text(
        json.dumps(
            {
                "server": {"port": 1933, "root_api_key": "root-key"},
                "vlm": {"api_key": "judge-key", "api_base": "http://judge", "model": "judge-model"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_cfg_src = tmp_path / "mini.toml"
    runtime_cfg_src.write_text(
        "[general]\nname = \"mini-test\"\nuser = \"eval-1\"\nagent_id = \"locomo-eval\"\n",
        encoding="utf-8",
    )

    state_dir = tmp_path / "state"
    home_dir = tmp_path / "home"
    config_path = state_dir / "openclaw.json"
    env_path = state_dir / "openviking.env"
    runtime_cfg_dst = tmp_path / "mini-runtime.toml"

    bootstrap_locomo_openclaw_runtime(
        base_state_dir=base_state_dir,
        base_ov_conf=base_ov_conf,
        state_dir=state_dir,
        home_dir=home_dir,
        config_path=config_path,
        env_path=env_path,
        gateway_port=28789,
        run_id="mini-run",
        runtime_config_src=runtime_cfg_src,
        runtime_config_dst=runtime_cfg_dst,
        output_dir="/tmp/out",
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    plugin_cfg = data["plugins"]["entries"]["openviking"]["config"]
    assert plugin_cfg["accountId"] == "acct-mini-run"
    assert plugin_cfg["userId"] == "eval-1"
    assert plugin_cfg["agentId"] == "acct-mini-run"
