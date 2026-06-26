from pathlib import Path

from locomo_test.config import load_config


def test_load_config_reads_unified_llm_sections_and_falls_back_judge(tmp_path):
    env_path = tmp_path / "env.toml"
    env_path.write_text(
        """
[gateway]
port = 19790
token = "gw-token"
state_dir = "/tmp/openclaw"

[openviking]
port = 2936

[llm.chat]
base_url = "https://codex.jemmy.icu/v1"
api_key = "chat-key"
model = "gpt-5.4-mini"

[llm.embedding]
base_url = "http://127.0.0.1:18080/v1"
api_key = "dummy"
model = "Qwen/Qwen3-Embedding-0.6B"
dimension = 1024
""".strip(),
        encoding="utf-8",
    )
    test_path = tmp_path / "mini.toml"
    test_path.write_text(
        """
[general]
name = "mini"
env_file = "env.toml"
dataset = "small"
memory_mode = "openviking"
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(str(test_path))

    assert cfg.llm.chat.base_url == "https://codex.jemmy.icu/v1"
    assert cfg.llm.chat.api_key == "chat-key"
    assert cfg.llm.chat.model == "gpt-5.4-mini"
    assert cfg.llm.embedding.base_url == "http://127.0.0.1:18080/v1"
    assert cfg.llm.embedding.api_key == "dummy"
    assert cfg.llm.embedding.model == "Qwen/Qwen3-Embedding-0.6B"
    assert cfg.llm.embedding.dimension == 1024
    assert cfg.judge_env.base_url == "https://codex.jemmy.icu/v1"
    assert cfg.judge_env.api_key == "chat-key"
    assert cfg.judge_env.model == "gpt-5.4-mini"


def test_load_config_prefers_explicit_judge_over_llm_chat(tmp_path):
    env_path = tmp_path / "env.toml"
    env_path.write_text(
        """
[gateway]
port = 19790
token = "gw-token"
state_dir = "/tmp/openclaw"

[openviking]
port = 2936

[llm.chat]
base_url = "https://codex.jemmy.icu/v1"
api_key = "chat-key"
model = "gpt-5.4-mini"

[judge]
api_key = "judge-key"
base_url = "https://judge.example/v1"
model = "judge-model"
parallel = 9
""".strip(),
        encoding="utf-8",
    )
    test_path = tmp_path / "mini.toml"
    test_path.write_text(
        """
[general]
name = "mini"
env_file = "env.toml"
dataset = "small"
memory_mode = "openviking"
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(str(test_path))

    assert cfg.judge_env.api_key == "judge-key"
    assert cfg.judge_env.base_url == "https://judge.example/v1"
    assert cfg.judge_env.model == "judge-model"
    assert cfg.judge_env.parallel == 9
