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
    assert plugin_cfg["agent_prefix"] == "acct-mini-run"
    assert plugin_cfg["autoRecall"] is False
    assert plugin_cfg["autoCapture"] is True
    assert plugin_cfg["bypassSessionPatterns"] == ["qa-*"]
    assert data["agents"]["defaults"]["timeoutSeconds"] == 600
    assert "agentId" not in plugin_cfg
    manifest = json.loads((state_dir / "extensions" / "openviking" / "openclaw.plugin.json").read_text(encoding="utf-8"))
    assert manifest["configSchema"]["properties"]["agent_prefix"] == {"type": "string"}


def test_bootstrap_locomo_openclaw_runtime_honors_openclaw_timeout_override(tmp_path):
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
        "\n".join(
            [
                '[general]',
                'name = "mini-test"',
                'user = "eval-1"',
                'agent_id = "locomo-eval"',
                '',
                '[openclaw]',
                'timeout_seconds = 900',
            ]
        ) + "\n",
        encoding="utf-8",
    )

    state_dir = tmp_path / "state"
    home_dir = tmp_path / "home"
    config_path = state_dir / "openclaw.json"

    bootstrap_locomo_openclaw_runtime(
        base_state_dir=base_state_dir,
        base_ov_conf=base_ov_conf,
        state_dir=state_dir,
        home_dir=home_dir,
        config_path=config_path,
        env_path=state_dir / "openviking.env",
        gateway_port=28789,
        run_id="mini-run",
        runtime_config_src=runtime_cfg_src,
        runtime_config_dst=tmp_path / "mini-runtime.toml",
        output_dir="/tmp/out",
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["agents"]["defaults"]["timeoutSeconds"] == 900


def test_bootstrap_locomo_openclaw_runtime_writes_unified_llm_env_toml(tmp_path):
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
    (tmp_path / "env.toml").write_text(
        "\n".join(
            [
                "[llm.chat]",
                'base_url = "https://codex.jemmy.icu/v1"',
                'api_key = "chat-key"',
                'model = "gpt-5.4-mini"',
                "",
                "[llm.embedding]",
                'base_url = "http://127.0.0.1:18080/v1"',
                'api_key = "dummy"',
                'model = "Qwen/Qwen3-Embedding-0.6B"',
                "dimension = 1024",
                "",
                "[judge]",
                'api_key = "judge-key-override"',
                'base_url = "https://codex.jemmy.icu/v1"',
                'model = "gpt-5.4-mini"',
                'api_format = "openai"',
                "parallel = 5",
            ]
        )
        + "\n",
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

    env_toml = (tmp_path / "env.toml").read_text(encoding="utf-8")
    assert '[llm.chat]' in env_toml
    assert 'base_url = "https://codex.jemmy.icu/v1"' in env_toml
    assert '[llm.embedding]' in env_toml
    assert 'base_url = "http://127.0.0.1:18080/v1"' in env_toml
    assert 'model = "Qwen/Qwen3-Embedding-0.6B"' in env_toml


def test_bootstrap_locomo_openclaw_runtime_pins_locomo_eval_model(tmp_path):
    base_state_dir = tmp_path / "base_state"
    (base_state_dir / "agents" / "main" / "agent").mkdir(parents=True)
    (base_state_dir / "extensions" / "openviking").mkdir(parents=True)
    (base_state_dir / "openviking.env").write_text("", encoding="utf-8")
    (base_state_dir / "agents" / "main" / "agent" / "auth-profiles.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "agents" / "main" / "agent" / "auth-state.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "agents" / "main" / "agent" / "models.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "extensions" / "openviking" / "openclaw.plugin.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "gateway": {"auth": {"token": "gw-token"}},
                "plugins": {"allow": []},
                "agents": {
                    "defaults": {"model": {"primary": "minimax/MiniMax-M3"}},
                    "list": [
                        {"id": "main", "model": "minimax/MiniMax-M3"},
                        {"id": "locomo-eval", "model": "minimax/MiniMax-M3"},
                    ],
                },
            },
            ensure_ascii=False,
        ),
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
    (tmp_path / "env.toml").write_text(
        "\n".join(
            [
                "[llm.chat]",
                'base_url = "https://codex.jemmy.icu/v1"',
                'api_key = "chat-key"',
                'model = "gpt-5.4-mini"',
            ]
        )
        + "\n",
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

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["agents"]["defaults"]["model"]["primary"] == "openai/gpt-5.4-mini"
    models = {item["id"]: item["model"] for item in payload["agents"]["list"]}
    assert models["locomo-eval"] == "openai/gpt-5.4-mini"


def test_bootstrap_locomo_openclaw_runtime_normalizes_openai_model_and_provider_config(tmp_path):
    base_state_dir = tmp_path / "base_state"
    (base_state_dir / "agents" / "main" / "agent").mkdir(parents=True)
    (base_state_dir / "extensions" / "openviking").mkdir(parents=True)
    (base_state_dir / "openviking.env").write_text("", encoding="utf-8")
    (base_state_dir / "agents" / "main" / "agent" / "auth-profiles.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "agents" / "main" / "agent" / "auth-state.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "agents" / "main" / "agent" / "models.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "extensions" / "openviking" / "openclaw.plugin.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "gateway": {"auth": {"token": "gw-token"}},
                "plugins": {"allow": []},
                "agents": {
                    "defaults": {"model": {"primary": "minimax/MiniMax-M3"}},
                    "list": [
                        {"id": "main", "model": "minimax/MiniMax-M3"},
                        {"id": "locomo-eval", "model": "minimax/MiniMax-M3"},
                    ],
                },
            },
            ensure_ascii=False,
        ),
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
    (tmp_path / "env.toml").write_text(
        "\n".join(
            [
                "[llm.chat]",
                'base_url = "https://codex.jemmy.icu/v1"',
                'api_key = "chat-key"',
                'model = "gpt-5.4-mini"',
            ]
        )
        + "\n",
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

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["agents"]["defaults"]["model"]["primary"] == "openai/gpt-5.4-mini"
    models = {item["id"]: item["model"] for item in payload["agents"]["list"]}
    assert models["locomo-eval"] == "openai/gpt-5.4-mini"
    provider = payload["models"]["providers"]["openai"]
    assert provider["apiKey"] == "chat-key"
    assert provider["baseUrl"] == "https://codex.jemmy.icu/v1"
    assert provider["api"] == "openai-responses"
    assert provider["models"][0]["id"] == "gpt-5.4-mini"
    assert provider["models"][0]["api"] == "openai-responses"


def test_bootstrap_locomo_openclaw_runtime_rewrites_openai_default_auth_profile(tmp_path):
    base_state_dir = tmp_path / "base_state"
    agent_dir = base_state_dir / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True)
    (base_state_dir / "extensions" / "openviking").mkdir(parents=True)
    (base_state_dir / "openviking.env").write_text("", encoding="utf-8")
    (agent_dir / "auth-profiles.json").write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "openai:default": {
                        "type": "api_key",
                        "provider": "openai",
                        "key": "stale-key",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (agent_dir / "auth-state.json").write_text("{}", encoding="utf-8")
    (agent_dir / "models.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "extensions" / "openviking" / "openclaw.plugin.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "gateway": {"auth": {"token": "gw-token"}},
                "plugins": {"allow": []},
                "agents": {
                    "defaults": {"model": {"primary": "minimax/MiniMax-M3"}},
                    "list": [{"id": "locomo-eval", "model": "minimax/MiniMax-M3"}],
                },
            },
            ensure_ascii=False,
        ),
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
    (tmp_path / "env.toml").write_text(
        "\n".join(
            [
                "[llm.chat]",
                'base_url = "https://codex.jemmy.icu/v1"',
                'api_key = "fresh-chat-key"',
                'model = "gpt-5.4-mini"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    state_dir = tmp_path / "state"
    bootstrap_locomo_openclaw_runtime(
        base_state_dir=base_state_dir,
        base_ov_conf=base_ov_conf,
        state_dir=state_dir,
        home_dir=tmp_path / "home",
        config_path=state_dir / "openclaw.json",
        env_path=state_dir / "openviking.env",
        gateway_port=28789,
        run_id="mini-run",
        runtime_config_src=runtime_cfg_src,
        runtime_config_dst=tmp_path / "mini-runtime.toml",
        output_dir="/tmp/out",
    )

    runtime_profiles = json.loads(
        (state_dir / "agents" / "locomo-eval" / "agent" / "auth-profiles.json").read_text(encoding="utf-8")
    )
    assert runtime_profiles["profiles"]["openai:default"]["key"] == "fresh-chat-key"


def test_bootstrap_locomo_openclaw_runtime_rewrites_locomo_eval_paths_and_agents_md(tmp_path):
    base_state_dir = tmp_path / "base_state"
    agent_dir = base_state_dir / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True)
    (base_state_dir / "extensions" / "openviking").mkdir(parents=True)
    (base_state_dir / "openviking.env").write_text("", encoding="utf-8")
    (agent_dir / "auth-profiles.json").write_text("{}", encoding="utf-8")
    (agent_dir / "auth-state.json").write_text("{}", encoding="utf-8")
    (agent_dir / "models.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "extensions" / "openviking" / "openclaw.plugin.json").write_text("{}", encoding="utf-8")
    (base_state_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "gateway": {"auth": {"token": "gw-token"}},
                "plugins": {"allow": []},
                "agents": {
                    "defaults": {"workspace": "/root/.openclaw/workspace"},
                    "list": [
                        {
                            "id": "locomo-eval",
                            "model": "minimax/MiniMax-M3",
                            "workspace": "/root/.openclaw/workspace/locomo-eval",
                            "agentDir": "/root/.openclaw/agents/locomo-eval/agent",
                        }
                    ],
                },
            },
            ensure_ascii=False,
        ),
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
    bootstrap_locomo_openclaw_runtime(
        base_state_dir=base_state_dir,
        base_ov_conf=base_ov_conf,
        state_dir=state_dir,
        home_dir=home_dir,
        config_path=state_dir / "openclaw.json",
        env_path=state_dir / "openviking.env",
        gateway_port=28789,
        run_id="mini-run",
        runtime_config_src=runtime_cfg_src,
        runtime_config_dst=tmp_path / "mini-runtime.toml",
        output_dir="/tmp/out",
    )

    payload = json.loads((state_dir / "openclaw.json").read_text(encoding="utf-8"))
    locomo_agent = next(item for item in payload["agents"]["list"] if item["id"] == "locomo-eval")
    assert locomo_agent["workspace"] == str(state_dir / "workspace" / "locomo-eval")
    assert locomo_agent["agentDir"] == str(state_dir / "agents" / "locomo-eval" / "agent")
    agents_md = (state_dir / "workspace" / "locomo-eval" / "AGENTS.md").read_text(encoding="utf-8")
    assert "benchmark QA agent" in agents_md
