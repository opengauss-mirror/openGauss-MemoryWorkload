from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_LOCOMO_EVAL_AGENTS_MD = """# Benchmark Agent Rules

You are a benchmark QA agent for LoCoMo and related memory evaluations.

## Primary Task

- Answer the user question directly from the recalled memory snippets already available in the current context.
- Treat recalled memory snippets as the primary evidence source.
- Treat the normalized summary or leading bullet points at the top of each memory as authoritative before raw chat-log details.
- If the answer is not supported by recalled memory, say so briefly.

## Hard Rules

- Do not read local workspace files such as `AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`, or `memory/YYYY-MM-DD.md`.
- Do not use filesystem or shell style tools such as `exec` for benchmark QA.
- Do not call `session_status` or other diagnostic tools unless the user explicitly asks for debugging details.
- Do not invent extra search steps when recalled memory is already present in context.
- Prefer the most specific supported fact over generic paraphrase.
- For list or set questions, include only explicitly supported items that match the asked category.
- Do not say information is unavailable when the recalled memories explicitly contain the answer.
- If the memory says an event happened in the week before the current date, answer with that relative date instead of saying it is missing.
- Prefer a short direct answer over explanations.

## Output Style

- For fact questions: answer in one short sentence.
- For list questions: answer with a short comma-separated list.
- Do not mention internal tools, memory files, or workspace instructions.
"""


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


def _normalize_openclaw_model_ref(model_name: str) -> str:
    normalized = str(model_name or "").strip()
    if not normalized:
        return "openai/gpt-5.4-mini"
    if "/" in normalized:
        return normalized
    return f"openai/{normalized}"


def _inject_openclaw_provider_config(
    config_data: dict,
    *,
    model_name: str,
    base_url: str,
    api_key: str,
) -> None:
    provider_name = str(model_name or "").split("/", 1)[0].strip()
    if not provider_name:
        return
    models = config_data.setdefault("models", {})
    providers = models.setdefault("providers", {})
    provider_cfg = providers.get(provider_name)
    merged = dict(provider_cfg) if isinstance(provider_cfg, dict) else {}
    if api_key and not str(merged.get("apiKey") or "").strip():
        merged["apiKey"] = api_key
    if provider_name == "openai":
        if base_url and not str(merged.get("baseUrl") or "").strip():
            merged["baseUrl"] = base_url
        merged.setdefault("api", "openai-responses")
        if not merged.get("models"):
            model_id = str(model_name.split("/", 1)[1] if "/" in model_name else model_name).strip()
            merged["models"] = [
                {
                    "id": model_id,
                    "name": model_id,
                    "api": "openai-responses",
                    "reasoning": False,
                    "input": ["text"],
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                    "contextWindow": 128000,
                    "maxTokens": 4096,
                }
            ]
    if merged:
        providers[provider_name] = merged


def _rewrite_auth_profile_api_key(path: Path, *, provider: str, api_key: str) -> None:
    if not api_key or not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    profiles = payload.setdefault("profiles", {})
    profile_id = f"{provider}:default"
    current = profiles.get(profile_id)
    next_profile = dict(current) if isinstance(current, dict) else {}
    next_profile.update(
        {
            "type": "api_key",
            "provider": provider,
            "key": api_key,
        }
    )
    profiles[profile_id] = next_profile
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_locomo_eval_agents_md() -> str:
    override_path = (
        Path(__file__).resolve().parents[2]
        / "tools"
        / "test_entrypoints"
        / "remote_overrides"
        / "locomo_eval_AGENTS.md"
    )
    if override_path.exists():
        return override_path.read_text(encoding="utf-8")
    return DEFAULT_LOCOMO_EVAL_AGENTS_MD


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
    openclaw_cfg = runtime_cfg.get("openclaw", {}) if isinstance(runtime_cfg, dict) else {}
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
    llm_cfg = existing_env.get("llm", {}) if isinstance(existing_env, dict) else {}
    llm_chat = llm_cfg.get("chat", {}) if isinstance(llm_cfg, dict) else {}
    llm_embedding = llm_cfg.get("embedding", {}) if isinstance(llm_cfg, dict) else {}
    locomo_model = _normalize_openclaw_model_ref(str(llm_chat.get("model", "gpt-5.4-mini")))

    gateway = base_config.setdefault("gateway", {})
    gateway["port"] = gateway_port
    agents_cfg = base_config.setdefault("agents", {})
    defaults_cfg = agents_cfg.setdefault("defaults", {})
    defaults_cfg.setdefault("model", {})["primary"] = locomo_model
    timeout_seconds = openclaw_cfg.get("timeout_seconds")
    if timeout_seconds is None:
        timeout_seconds = os.environ.get("LOCOMO_OPENCLAW_TIMEOUT_SECONDS", "600") or 600
    defaults_cfg["timeoutSeconds"] = int(timeout_seconds)
    defaults_cfg["workspace"] = str(state_dir / "workspace")
    runtime_workspace_dir = state_dir / "workspace" / runtime_agent
    runtime_workspace_dir.mkdir(parents=True, exist_ok=True)
    for agent in agents_cfg.get("list", []):
        if isinstance(agent, dict) and agent.get("id") == runtime_agent:
            agent["model"] = locomo_model
            agent["workspace"] = str(runtime_workspace_dir)
            agent["agentDir"] = str(state_dir / "agents" / runtime_agent / "agent")
    _inject_openclaw_provider_config(
        base_config,
        model_name=locomo_model,
        base_url=str(llm_chat.get("base_url", "https://codex.jemmy.icu/v1")),
        api_key=str(llm_chat.get("api_key", "")),
    )
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
            "isolateUserScopeByAgent": True,
            "isolateAgentScopeByUser": True,
            "autoRecall": False,
            "autoCapture": True,
            "bypassSessionPatterns": ["qa-*"],
            "emitStandardDiagnostics": True,
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
    openai_api_key = str(llm_chat.get("api_key", ""))
    _rewrite_auth_profile_api_key(
        state_dir / "agents" / "main" / "agent" / "auth-profiles.json",
        provider="openai",
        api_key=openai_api_key,
    )
    _rewrite_auth_profile_api_key(
        state_dir / "agents" / runtime_agent / "agent" / "auth-profiles.json",
        provider="openai",
        api_key=openai_api_key,
    )

    extensions_src = base_state_dir / "extensions" / "openviking"
    extensions_dst = state_dir / "extensions" / "openviking"
    if extensions_src.exists():
        extensions_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(extensions_src, extensions_dst)
        plugin_manifest = extensions_dst / "openclaw.plugin.json"
        if plugin_manifest.exists():
            try:
                manifest_data = json.loads(plugin_manifest.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest_data = {}
            plugin_manifest.write_text(
                json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    (runtime_workspace_dir / "AGENTS.md").write_text(_load_locomo_eval_agents_md(), encoding="utf-8")

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
    env_text = _ensure_export(env_text, "OPENVIKING_ISOLATE_USER_SCOPE_BY_AGENT", "true")
    env_text = _ensure_export(env_text, "OPENVIKING_ISOLATE_AGENT_SCOPE_BY_USER", "true")
    env_path.write_text(env_text, encoding="utf-8")

    judge_cfg = existing_env.get("judge", {}) if isinstance(existing_env, dict) else {}
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
