from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


def _copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _ensure_export(env_text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}=", re.MULTILINE)
    if pattern.search(env_text):
        return env_text
    suffix = "" if env_text.endswith("\n") or not env_text else "\n"
    return f'{env_text}{suffix}export {key}="{value}"\n'


def bootstrap_locomo_openclaw_runtime(
    *,
    base_state_dir: Path,
    base_ov_conf: Path,
    state_dir: Path,
    home_dir: Path,
    config_path: Path,
    env_path: Path,
    gateway_port: int,
    run_id: str,
    runtime_config_src: Path,
    runtime_config_dst: Path,
    output_dir: str,
) -> None:
    runtime_cfg = tomllib.loads(runtime_config_src.read_text(encoding="utf-8"))
    existing_env_toml = runtime_config_src.parent / "env.toml"
    existing_env = (
        tomllib.loads(existing_env_toml.read_text(encoding="utf-8"))
        if existing_env_toml.exists()
        else {}
    )
    general_cfg = runtime_cfg.get("general", {}) if isinstance(runtime_cfg, dict) else {}
    runtime_user = str(general_cfg.get("user", "eval-1"))
    runtime_agent = str(general_cfg.get("agent_id", "locomo-eval"))
    account_id = f"acct-{run_id}"

    if home_dir.exists():
        shutil.rmtree(home_dir)
    if state_dir.exists():
        shutil.rmtree(state_dir)

    home_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "agents" / "main" / "agent").mkdir(parents=True, exist_ok=True)
    (state_dir / "agents" / runtime_agent / "agent").mkdir(parents=True, exist_ok=True)
    link_path = home_dir / ".openclaw"
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(state_dir, target_is_directory=True)

    base_config = json.loads((base_state_dir / "openclaw.json").read_text(encoding="utf-8"))
    ov_conf = json.loads(base_ov_conf.read_text(encoding="utf-8"))

    gateway = base_config.setdefault("gateway", {})
    gateway["port"] = gateway_port
    plugins = base_config.setdefault("plugins", {})
    allow = list(plugins.get("allow") or [])
    if "openviking" not in allow:
        allow.append("openviking")
    plugins["allow"] = allow
    entries = plugins.setdefault("entries", {})
    entries["openviking"] = {
        "enabled": True,
        "config": {
            "mode": "remote",
            "baseUrl": f'http://127.0.0.1:{ov_conf["server"]["port"]}',
            "apiKey": str(ov_conf.get("server", {}).get("root_api_key", "")),
            "accountId": account_id,
            "userId": runtime_user,
            "agentId": account_id,
            "autoRecall": True,
            "autoCapture": True,
            "logFindRequests": True,
        },
    }
    plugins.setdefault("slots", {})["contextEngine"] = "openviking"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(base_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for rel_src, rel_dst in [
        ("agents/main/agent/auth-profiles.json", "agents/main/agent/auth-profiles.json"),
        ("agents/main/agent/auth-state.json", "agents/main/agent/auth-state.json"),
        ("agents/main/agent/models.json", "agents/main/agent/models.json"),
        ("openviking.env", "openviking.env"),
    ]:
        _copy_if_exists(base_state_dir / rel_src, state_dir / rel_dst)

    for leaf in ("auth-profiles.json", "auth-state.json", "models.json"):
        _copy_if_exists(
            base_state_dir / "agents" / "main" / "agent" / leaf,
            state_dir / "agents" / runtime_agent / "agent" / leaf,
        )

    extensions_src = base_state_dir / "extensions" / "openviking"
    extensions_dst = state_dir / "extensions" / "openviking"
    if extensions_src.exists():
        extensions_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(extensions_src, extensions_dst)

    if not env_path.exists():
        env_path.write_text("", encoding="utf-8")
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env_text = _ensure_export(env_text, "OPENVIKING_BASE_URL", f'http://127.0.0.1:{ov_conf["server"]["port"]}')
    env_text = _ensure_export(env_text, "OPENVIKING_API_KEY", str(ov_conf.get("server", {}).get("root_api_key", "")))
    env_text = _ensure_export(
        env_text,
        "OPENVIKING_ROOT_API_KEY",
        str(ov_conf.get("server", {}).get("root_api_key", "")),
    )
    env_text = _ensure_export(env_text, "OPENVIKING_ACCOUNT_ID", account_id)
    env_text = _ensure_export(env_text, "OPENVIKING_USER_ID", runtime_user)
    env_text = _ensure_export(env_text, "OPENVIKING_AGENT_ID", runtime_agent)
    env_path.write_text(env_text, encoding="utf-8")

    judge_cfg = existing_env.get("judge", {}) if isinstance(existing_env, dict) else {}
    llm_cfg = existing_env.get("llm", {}) if isinstance(existing_env, dict) else {}
    llm_chat = llm_cfg.get("chat", {}) if isinstance(llm_cfg, dict) else {}
    llm_embedding = llm_cfg.get("embedding", {}) if isinstance(llm_cfg, dict) else {}
    env_toml = (
        f"[gateway]\n"
        f"port = {gateway_port}\n"
        f'token = "{base_config["gateway"]["auth"]["token"]}"\n'
        f'state_dir = "{state_dir}"\n\n'
        "[openviking]\n"
        f'port = {ov_conf["server"]["port"]}\n\n'
        "[judge]\n"
        f'api_key = "{judge_cfg.get("api_key", ov_conf["vlm"]["api_key"])}"\n'
        f'base_url = "{judge_cfg.get("base_url", "https://codex.jemmy.icu/v1")}"\n'
        f'model = "{judge_cfg.get("model", "gpt-5.4-mini")}"\n'
        f'api_format = "{judge_cfg.get("api_format", "openai")}"\n'
        f'parallel = {judge_cfg.get("parallel", 5)}\n'
        "\n[llm.chat]\n"
        f'base_url = "{llm_chat.get("base_url", "https://codex.jemmy.icu/v1")}"\n'
        f'api_key = "{llm_chat.get("api_key", judge_cfg.get("api_key", ov_conf["vlm"]["api_key"]))}"\n'
        f'model = "{llm_chat.get("model", judge_cfg.get("model", "gpt-5.4-mini"))}"\n'
        "\n[llm.embedding]\n"
        f'base_url = "{llm_embedding.get("base_url", "http://127.0.0.1:18080/v1")}"\n'
        f'api_key = "{llm_embedding.get("api_key", "dummy")}"\n'
        f'model = "{llm_embedding.get("model", "Qwen/Qwen3-Embedding-0.6B")}"\n'
        f'dimension = {int(llm_embedding.get("dimension", 1024))}\n'
    )
    (runtime_config_src.parent / "env.toml").write_text(env_toml, encoding="utf-8")

    text = runtime_config_src.read_text(encoding="utf-8")
    text = re.sub(r'^name = ".*"$', f'name = "{run_id}"', text, count=1, flags=re.MULTILINE)
    if re.search(r"^output_dir = ", text, flags=re.MULTILINE):
        text = re.sub(r'^output_dir = ".*"$', f'output_dir = "{output_dir}"', text, flags=re.MULTILINE)
    else:
        text = text.replace("[general]\n", f'[general]\noutput_dir = "{output_dir}"\n', 1)
    runtime_config_dst.write_text(text, encoding="utf-8")
