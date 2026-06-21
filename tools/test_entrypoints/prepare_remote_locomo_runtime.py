#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import re
import subprocess
import textwrap
from pathlib import Path


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _encode_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _remove_redundant_reindex_injection(phase_text: str) -> str:
    """Keep only one reindex_memory_root shim and one wait_for_search_visibility signature."""

    pattern = re.compile(
        r"\ndef reindex_memory_root\([^\n]*\n(?:    .*?\n)+?\n(?=def wait_for_search_visibility\()",
        re.MULTILINE,
    )
    return pattern.sub("\n", phase_text)


def _remove_redundant_plugin_config_cleanup(phase_text: str) -> str:
    repeated = re.compile(
        r"(\n    changed\.update\(\n        \{\n            key: None\n            for key in legacy_keys\n            if key in current\n        \}\n    \)\n){2,}",
        re.MULTILINE,
    )
    single = (
        "\n    changed.update(\n"
        "        {\n"
        "            key: None\n"
        "            for key in legacy_keys\n"
        "            if key in current\n"
        "        }\n"
        "    )\n"
    )
    return repeated.sub(single, phase_text)


def _remove_redundant_post_ingest_meta(phase_text: str) -> str:
    repeated = re.compile(r'(\n        "post_ingest_reindex": reindex_result,){2,}', re.MULTILINE)
    return repeated.sub('\n        "post_ingest_reindex": reindex_result,', phase_text)


def _enable_benchmark_plugin_diagnostics(shell_text: str) -> str:
    if 'cfg["emitStandardDiagnostics"] = True\n' in shell_text:
        return shell_text
    anchor = 'cfg["accountId"] = account_id\n'
    replacement = (
        'cfg["accountId"] = account_id\n'
        'cfg["emitStandardDiagnostics"] = True\n'
        'cfg["logFindRequests"] = True\n'
    )
    return shell_text.replace(anchor, replacement)


def _enable_dedicated_gateway_port(shell_text: str) -> str:
    if 'OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-28789}"' not in shell_text:
        shell_text = shell_text.replace(
            'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/root/.openclaw/openclaw.json}"\n',
            'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/root/.openclaw/openclaw.json}"\n'
            'OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-28789}"\n',
        )

    if "set_openclaw_gateway_port()" not in shell_text:
        shell_text = shell_text.replace(
            "sync_openclaw_auth_profiles() {\n",
            """set_openclaw_gateway_port() {
  python3 - "${OPENCLAW_CONFIG_PATH}" "${OPENCLAW_GATEWAY_PORT}" <<'__OVTEST_GATEWAY_PORT_PY__'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
gateway_port = int(sys.argv[2])
data = json.loads(config_path.read_text(encoding="utf-8"))
gateway = data.setdefault("gateway", {})
gateway["port"] = gateway_port
control_ui = gateway.setdefault("controlUi", {})
control_ui["allowedOrigins"] = [
    f"http://localhost:{{gateway_port}}",
    f"http://127.0.0.1:{{gateway_port}}",
]
config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
print(json.dumps({{"gateway_port": gateway_port}}, ensure_ascii=False))
PY
}

sync_openclaw_auth_profiles() {
""",
        )

    shell_text = shell_text.replace(
        '    if curl -fsS http://127.0.0.1:18789/health >/tmp/"${RUN_ID}"_gw_health.json 2>/dev/null; then\n',
        '    if curl -fsS "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}/health" >/tmp/"${RUN_ID}"_gw_health.json 2>/dev/null; then\n',
    )
    shell_text = shell_text.replace(
        'config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")\n',
        'config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")\n',
    )
    shell_text = re.sub(
        r'config_path\.write_text\(json\.dumps\(data, ensure_ascii=False, indent=2\) \+ "\s*"\s*, encoding="utf-8"\)',
        'config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")',
        shell_text,
        flags=re.MULTILINE,
    )
    base_url_flag = '    --base-url "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}"\n'
    if base_url_flag not in shell_text:
        shell_text = shell_text.replace(
            '    --output-dir "${OUTPUT_DIR}"\n',
            '    --output-dir "${OUTPUT_DIR}"\n' + base_url_flag,
        )
    if "  set_openclaw_gateway_port\n  sync_openclaw_auth_profiles\n" not in shell_text:
        shell_text = shell_text.replace(
            "  sync_openclaw_auth_profiles\n",
            "  set_openclaw_gateway_port\n"
            "  sync_openclaw_auth_profiles\n",
        )
    return shell_text


def _ensure_openviking_signature_compat(source_text: str, kind: str) -> str:
    if kind == "session_extract_context_provider":
        marker = "latest_archive_session_time: str = \"\","
        if marker in source_text:
            return source_text
        return source_text.replace(
            "        latest_archive_overview: str = \"\",\n"
            "        isolation_handler: MemoryIsolationHandler = None,\n",
            "        latest_archive_overview: str = \"\",\n"
            "        latest_archive_session_time: str = \"\",\n"
            "        isolation_handler: MemoryIsolationHandler = None,\n",
        )
    if kind == "extract_agent_memories":
        if (
            "    async def extract_agent_memories(\n"
            "        self,\n"
            "        messages: List[Message],\n"
            "        ctx: Optional[RequestContext] = None,\n"
            "        strict_extract_errors: bool = False,\n"
            "        latest_archive_overview: str = \"\",\n"
            "        latest_archive_session_time: str = \"\",\n"
            "    ) -> List[Context]:\n"
        ) in source_text:
            return source_text
        return source_text.replace(
            "    async def extract_agent_memories(\n"
            "        self,\n"
            "        messages: List[Message],\n"
            "        ctx: Optional[RequestContext] = None,\n"
            "        strict_extract_errors: bool = False,\n"
            "    ) -> List[Context]:\n",
            "    async def extract_agent_memories(\n"
            "        self,\n"
            "        messages: List[Message],\n"
            "        ctx: Optional[RequestContext] = None,\n"
            "        strict_extract_errors: bool = False,\n"
            "        latest_archive_overview: str = \"\",\n"
            "        latest_archive_session_time: str = \"\",\n"
            "    ) -> List[Context]:\n",
        )
    raise ValueError(f"unsupported compatibility patch kind: {kind}")


def _ensure_openclaw_openviking_plugin_compat(source_text: str) -> str:
    updated = re.sub(
        r'export function createSessionAgentResolver\(configAgentId: string\) \{\n'
        r'\s*const configAgentPrefix = configAgentId\.trim\(\) === "default" \? "" : configAgentId\.trim\(\);\n',
        'export function createSessionAgentResolver(configAgentId?: string | null) {\n'
        '  const normalizedConfigAgentId = typeof configAgentId === "string" ? configAgentId.trim() : "";\n'
        '  const configAgentPrefix = normalizedConfigAgentId === "default" ? "" : normalizedConfigAgentId;\n',
        source_text,
    )
    updated = updated.replace(
        "        cfg.agent_prefix,\n"
        "        cfg.timeoutMs,\n",
        '        cfg.agent_prefix ?? "",\n'
        "        cfg.timeoutMs,\n",
    )
    updated = updated.replace(
        "    const sessionAgentResolver = createSessionAgentResolver(cfg.agent_prefix);\n",
        '    const sessionAgentResolver = createSessionAgentResolver(cfg.agent_prefix ?? "");\n',
    )
    return updated


def _ensure_openclaw_auto_recall_query_extract(source_text: str) -> str:
    marker = 'const questionMarker = "Question:";'
    if marker in source_text:
        return source_text
    old = """export function prepareRecallQuery(rawText: string): PreparedRecallQuery {
  const sanitized = sanitizeUserTextForCapture(rawText).trim();
  const originalChars = sanitized.length;

  if (!sanitized) {
    return {
      query: "",
      truncated: false,
      originalChars: 0,
      finalChars: 0,
    };
  }

  const query =
    sanitized.length > RECALL_QUERY_MAX_CHARS
      ? sanitized.slice(0, RECALL_QUERY_MAX_CHARS).trim()
      : sanitized;

  return {
    query,
    truncated: sanitized.length > RECALL_QUERY_MAX_CHARS,
    originalChars,
    finalChars: query.length,
  };
}
"""
    new = """export function prepareRecallQuery(rawText: string): PreparedRecallQuery {
  const sanitized = sanitizeUserTextForCapture(rawText).trim();
  const originalChars = sanitized.length;

  if (!sanitized) {
    return {
      query: "",
      truncated: false,
      originalChars: 0,
      finalChars: 0,
    };
  }

  let normalized = sanitized;
  const questionMarker = "Question:";
  const markerIndex = sanitized.lastIndexOf(questionMarker);
  if (markerIndex >= 0) {
    const extracted = sanitized.slice(markerIndex + questionMarker.length).trim();
    if (extracted) {
      normalized = extracted;
    }
  }

  const query =
    normalized.length > RECALL_QUERY_MAX_CHARS
      ? normalized.slice(0, RECALL_QUERY_MAX_CHARS).trim()
      : normalized;

  return {
    query,
    truncated: normalized.length > RECALL_QUERY_MAX_CHARS,
    originalChars,
    finalChars: query.length,
  };
}
"""
    return source_text.replace(old, new)


def _inject_openclaw_provider_config(config_data: dict, model_name: str, auth_profiles: dict | None) -> None:
    provider_name = str(model_name or "").split("/", 1)[0].strip()
    if not provider_name:
        return
    models = config_data.setdefault("models", {})
    providers = models.setdefault("providers", {})
    provider_cfg = providers.get(provider_name)
    existing_api_key = (
        str(provider_cfg.get("apiKey") or "").strip()
        if isinstance(provider_cfg, dict)
        else ""
    )
    if existing_api_key:
        if provider_name != "minimax" or provider_cfg.get("models"):
            return

    auth_profiles = auth_profiles or {}
    candidate_keys = [
        f"{provider_name}:default",
        f"{provider_name}:cn",
        f"{provider_name}-cn:default",
        f"{provider_name}-portal:default",
    ]
    selected = None
    for profile_id in candidate_keys:
        profile = auth_profiles.get(profile_id)
        if isinstance(profile, dict) and str(profile.get("key") or profile.get("access") or "").strip():
            selected = profile
            break
    if selected is None and existing_api_key:
        selected = {"key": existing_api_key}
    if selected is None:
        return

    api_key = str(selected.get("key") or selected.get("access") or "").strip()
    template = {
        "apiKey": api_key,
    }
    if provider_name == "minimax":
        template.update(
            {
                "baseUrl": "https://api.minimaxi.com/v1",
                "api": "openai-completions",
                "models": [
                    {
                        "id": "MiniMax-M3",
                        "name": "MiniMax M3",
                        "reasoning": True,
                        "input": ["text"],
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                        "contextWindow": 196608,
                        "maxTokens": 8192,
                    }
                ],
            }
        )
    merged = {**template, **(provider_cfg or {})}
    if provider_name == "minimax" and not merged.get("models"):
        merged["models"] = template["models"]
    providers[provider_name] = merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch remote LoCoMo/OpenClaw runtime for benchmark runs.")
    parser.add_argument("--ssh-host", default="jcp@123.60.114.206")
    parser.add_argument("--ssh-port", default="10008")
    parser.add_argument("--remote-container", default="jcp-dev")
    parser.add_argument(
        "--benchmark-dir",
        default="/home/jcp/agent/code/OpenViking/benchmark/locomo/openclaw",
    )
    parser.add_argument("--locomo-model", default=None)
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    agents_path = script_dir / "remote_overrides" / "locomo_eval_AGENTS.md"
    agents_b64 = _encode_file(agents_path)

    remote_python = textwrap.dedent(
        """
        import base64
        from pathlib import Path
        import re

        benchmark_dir = Path(__BENCHMARK_DIR__)

        def _remove_redundant_reindex_injection(phase_text: str) -> str:
            pattern = re.compile(
                r"\\ndef reindex_memory_root\\([^\\n]*\\n(?:    .*?\\n)+?\\n(?=def wait_for_search_visibility\\()",
                re.MULTILINE,
            )
            return pattern.sub("\\n", phase_text)

        def _remove_redundant_plugin_config_cleanup(phase_text: str) -> str:
            repeated = re.compile(
                r"(\\n    changed\\.update\\(\\n        \\{{\\n            key: None\\n            for key in legacy_keys\\n            if key in current\\n        \\}}\\n    \\)\\n){{2,}}",
                re.MULTILINE,
            )
            single = (
                "\\n    changed.update(\\n"
                "        {{\\n"
                "            key: None\\n"
                "            for key in legacy_keys\\n"
                "            if key in current\\n"
                "        }}\\n"
                "    )\\n"
            )
            return repeated.sub(single, phase_text)

        def _remove_redundant_post_ingest_meta(phase_text: str) -> str:
            repeated = re.compile(r'(\\n        \"post_ingest_reindex\": reindex_result,){{2,}}', re.MULTILINE)
            return repeated.sub('\\n        \"post_ingest_reindex\": reindex_result,', phase_text)

        def _enable_benchmark_plugin_diagnostics(shell_text: str) -> str:
            if 'cfg["emitStandardDiagnostics"] = True\\n' in shell_text:
                return shell_text
            anchor = 'cfg["accountId"] = account_id\\n'
            replacement = (
                'cfg["accountId"] = account_id\\n'
                'cfg["emitStandardDiagnostics"] = True\\n'
                'cfg["logFindRequests"] = True\\n'
            )
            return shell_text.replace(anchor, replacement)

        def _enable_dedicated_gateway_port(shell_text: str) -> str:
            if 'OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-28789}"' not in shell_text:
                shell_text = shell_text.replace(
                    'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/root/.openclaw/openclaw.json}"\\n',
                    'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/root/.openclaw/openclaw.json}"\\n'
                    'OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-28789}"\\n',
                )

            if "set_openclaw_gateway_port()" not in shell_text:
                gateway_port_block = '''set_openclaw_gateway_port() {
  python3 - "${OPENCLAW_CONFIG_PATH}" "${OPENCLAW_GATEWAY_PORT}" <<'__OVTEST_GATEWAY_PORT_PY__'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
gateway_port = int(sys.argv[2])
data = json.loads(config_path.read_text(encoding="utf-8"))
gateway = data.setdefault("gateway", {})
gateway["port"] = gateway_port
control_ui = gateway.setdefault("controlUi", {})
control_ui["allowedOrigins"] = [
    f"http://localhost:{gateway_port}",
    f"http://127.0.0.1:{gateway_port}",
]
config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
print(json.dumps({"gateway_port": gateway_port}, ensure_ascii=False))
__OVTEST_GATEWAY_PORT_PY__
}

sync_openclaw_auth_profiles() {
'''
                shell_text = shell_text.replace(
                    "sync_openclaw_auth_profiles() {\\n",
                    gateway_port_block,
                )

            shell_text = shell_text.replace(
                '    if curl -fsS http://127.0.0.1:18789/health >/tmp/"${RUN_ID}"_gw_health.json 2>/dev/null; then\\n',
                '    if curl -fsS "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}/health" >/tmp/"${RUN_ID}"_gw_health.json 2>/dev/null; then\\n',
            )
            shell_text = shell_text.replace(
                'config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\\\\n", encoding="utf-8")\\n',
                'config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")\\n',
            )
            shell_text = re.sub(
                r'config_path\.write_text\(json\.dumps\(data, ensure_ascii=False, indent=2\) \+ "\s*"\s*, encoding="utf-8"\)',
                'config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")',
                shell_text,
                flags=re.MULTILINE,
            )
            base_url_flag = '    --base-url "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}"\\n'
            if base_url_flag not in shell_text:
                shell_text = shell_text.replace(
                    '    --output-dir "${OUTPUT_DIR}"\\n',
                    '    --output-dir "${OUTPUT_DIR}"\\n' + base_url_flag,
                )
            if "  set_openclaw_gateway_port\\n  sync_openclaw_auth_profiles\\n" not in shell_text:
                shell_text = shell_text.replace(
                    "  sync_openclaw_auth_profiles\\n",
                    "  set_openclaw_gateway_port\\n"
                    "  sync_openclaw_auth_profiles\\n",
                )
            return shell_text

        def _ensure_openviking_signature_compat(source_text: str, kind: str) -> str:
            if kind == "session_extract_context_provider":
                marker = "latest_archive_session_time: str = \\"\\","
                if marker in source_text:
                    return source_text
                return source_text.replace(
                    "        latest_archive_overview: str = \\"\\",\\n"
                    "        isolation_handler: MemoryIsolationHandler = None,\\n",
                    "        latest_archive_overview: str = \\"\\",\\n"
                    "        latest_archive_session_time: str = \\"\\",\\n"
                    "        isolation_handler: MemoryIsolationHandler = None,\\n",
                )
            if kind == "extract_agent_memories":
                if (
                    "    async def extract_agent_memories(\\n"
                    "        self,\\n"
                    "        messages: List[Message],\\n"
                    "        ctx: Optional[RequestContext] = None,\\n"
                    "        strict_extract_errors: bool = False,\\n"
                    "        latest_archive_overview: str = \\"\\",\\n"
                    "        latest_archive_session_time: str = \\"\\",\\n"
                    "    ) -> List[Context]:\\n"
                ) in source_text:
                    return source_text
                return source_text.replace(
                    "    async def extract_agent_memories(\\n"
                    "        self,\\n"
                    "        messages: List[Message],\\n"
                    "        ctx: Optional[RequestContext] = None,\\n"
                    "        strict_extract_errors: bool = False,\\n"
                    "    ) -> List[Context]:\\n",
                    "    async def extract_agent_memories(\\n"
                    "        self,\\n"
                    "        messages: List[Message],\\n"
                    "        ctx: Optional[RequestContext] = None,\\n"
                    "        strict_extract_errors: bool = False,\\n"
                    "        latest_archive_overview: str = \\"\\",\\n"
                    "        latest_archive_session_time: str = \\"\\",\\n"
                    "    ) -> List[Context]:\\n",
                )
            raise ValueError(f"unsupported compatibility patch kind: {{kind}}")

        def _ensure_openclaw_openviking_plugin_compat(source_text: str) -> str:
            updated = re.sub(
                r'export function createSessionAgentResolver\\(configAgentId: string\\) \\{{\\n'
                r'\\s*const configAgentPrefix = configAgentId\\.trim\\(\\) === "default" \\? "" : configAgentId\\.trim\\(\\);\\n',
                'export function createSessionAgentResolver(configAgentId?: string | null) {{\\n'
                '  const normalizedConfigAgentId = typeof configAgentId === "string" ? configAgentId.trim() : "";\\n'
                '  const configAgentPrefix = normalizedConfigAgentId === "default" ? "" : normalizedConfigAgentId;\\n',
                source_text,
            )
            updated = updated.replace(
                "        cfg.agent_prefix,\\n"
                "        cfg.timeoutMs,\\n",
                '        cfg.agent_prefix ?? "",\\n'
                "        cfg.timeoutMs,\\n",
            )
            updated = updated.replace(
                "    const sessionAgentResolver = createSessionAgentResolver(cfg.agent_prefix);\\n",
                '    const sessionAgentResolver = createSessionAgentResolver(cfg.agent_prefix ?? "");\\n',
            )
            return updated

        def _ensure_openclaw_auto_recall_query_extract(source_text: str) -> str:
            marker = 'const questionMarker = "Question:";'
            if marker in source_text:
                return source_text
            old = '''export function prepareRecallQuery(rawText: string): PreparedRecallQuery {
  const sanitized = sanitizeUserTextForCapture(rawText).trim();
  const originalChars = sanitized.length;

  if (!sanitized) {
    return {
      query: "",
      truncated: false,
      originalChars: 0,
      finalChars: 0,
    };
  }

  const query =
    sanitized.length > RECALL_QUERY_MAX_CHARS
      ? sanitized.slice(0, RECALL_QUERY_MAX_CHARS).trim()
      : sanitized;

  return {
    query,
    truncated: sanitized.length > RECALL_QUERY_MAX_CHARS,
    originalChars,
    finalChars: query.length,
  };
}
'''
            new = '''export function prepareRecallQuery(rawText: string): PreparedRecallQuery {
  const sanitized = sanitizeUserTextForCapture(rawText).trim();
  const originalChars = sanitized.length;

  if (!sanitized) {
    return {
      query: "",
      truncated: false,
      originalChars: 0,
      finalChars: 0,
    };
  }

  let normalized = sanitized;
  const questionMarker = "Question:";
  const markerIndex = sanitized.lastIndexOf(questionMarker);
  if (markerIndex >= 0) {
    const extracted = sanitized.slice(markerIndex + questionMarker.length).trim();
    if (extracted) {
      normalized = extracted;
    }
  }

  const query =
    normalized.length > RECALL_QUERY_MAX_CHARS
      ? normalized.slice(0, RECALL_QUERY_MAX_CHARS).trim()
      : normalized;

  return {
    query,
    truncated: normalized.length > RECALL_QUERY_MAX_CHARS,
    originalChars,
    finalChars: query.length,
  };
}
'''
            return source_text.replace(old, new)

        def _inject_openclaw_provider_config(config_data: dict, model_name: str, auth_profiles: dict | None) -> None:
            provider_name = str(model_name or "").split("/", 1)[0].strip()
            if not provider_name:
                return
            models = config_data.setdefault("models", {{}})
            providers = models.setdefault("providers", {{}})
            provider_cfg = providers.get(provider_name)
            existing_api_key = (
                str(provider_cfg.get("apiKey") or "").strip()
                if isinstance(provider_cfg, dict)
                else ""
            )
            if existing_api_key:
                if provider_name != "minimax" or provider_cfg.get("models"):
                    return

            auth_profiles = auth_profiles or {{}}
            candidate_keys = [
                f"{{provider_name}}:default",
                f"{{provider_name}}:cn",
                f"{{provider_name}}-cn:default",
                f"{{provider_name}}-portal:default",
            ]
            selected = None
            for profile_id in candidate_keys:
                profile = auth_profiles.get(profile_id)
                if isinstance(profile, dict) and str(profile.get("key") or profile.get("access") or "").strip():
                    selected = profile
                    break
            if selected is None and existing_api_key:
                selected = {{"key": existing_api_key}}
            if selected is None:
                return

            api_key = str(selected.get("key") or selected.get("access") or "").strip()
            template = {{
                "apiKey": api_key,
            }}
            if provider_name == "minimax":
                template.update(
                    {{
                        "baseUrl": "https://api.minimaxi.com/v1",
                        "api": "openai-completions",
                        "models": [
                            {{
                                "id": "MiniMax-M3",
                                "name": "MiniMax M3",
                                "reasoning": True,
                                "input": ["text"],
                                "cost": {{
                                    "input": 0,
                                    "output": 0,
                                    "cacheRead": 0,
                                    "cacheWrite": 0,
                                }},
                                "contextWindow": 196608,
                                "maxTokens": 8192,
                            }}
                        ],
                    }}
                )
            merged = {{**template, **(provider_cfg or {{}})}}
            if provider_name == "minimax" and not merged.get("models"):
                merged["models"] = template["models"]
            providers[provider_name] = merged

        shell_path = benchmark_dir / "run_clean_small_in_container.sh"
        shell_text = shell_path.read_text(encoding="utf-8")
        shell_text = _enable_benchmark_plugin_diagnostics(shell_text)
        shell_text = _enable_dedicated_gateway_port(shell_text)
        shell_text = shell_text.replace('cfg["agent_prefix"] = account_id\\n', '')
        shell_text = shell_text.replace('cfg["isolateUserScopeByAgent"] = isolate_user_scope_by_agent\\n', '')
        shell_text = shell_text.replace('cfg["isolateAgentScopeByUser"] = isolate_agent_scope_by_user\\n', '')
        shell_text = shell_text.replace(
            '    --qa-disable-autocapture\\n',
            '    ${{QA_DISABLE_AUTOCAPTURE:+--qa-disable-autocapture}}\\n',
        )
        shell_path.write_text(shell_text, encoding="utf-8")

        phase_path = benchmark_dir / "phase_a_off.py"
        phase_text = phase_path.read_text(encoding="utf-8")
        phase_text = _remove_redundant_reindex_injection(phase_text)
        phase_text = _remove_redundant_plugin_config_cleanup(phase_text)
        phase_text = _remove_redundant_post_ingest_meta(phase_text)
        phase_text = phase_text.replace(
            '    updates: dict[str, Any] = {{\\n'
            '        "userId": user,\\n'
            '        "isolateUserScopeByAgent": isolate_user_scope_by_agent,\\n'
            '        "isolateAgentScopeByUser": isolate_agent_scope_by_user,\\n'
            '    }}\\n'
            '    if account_id:\\n'
            '        updates["accountId"] = account_id\\n'
            '    if agent_prefix:\\n'
            '        updates["agent_prefix"] = agent_prefix\\n',
            '    legacy_keys = (\\n'
            '        "agent_prefix",\\n'
            '        "isolateUserScopeByAgent",\\n'
            '        "isolateAgentScopeByUser",\\n'
            '    )\\n'
            '    updates: dict[str, Any] = {{\\n'
            '        "userId": user,\\n'
            '    }}\\n'
            '    if account_id:\\n'
            '        updates["accountId"] = account_id\\n',
        )
        phase_text = phase_text.replace(
            '    changed = {{\\n'
            '        key: value\\n'
            '        for key, value in updates.items()\\n'
            '        if current.get(key) != value\\n'
            '    }}\\n',
            '    changed = {{\\n'
            '        key: value\\n'
            '        for key, value in updates.items()\\n'
            '        if current.get(key) != value\\n'
            '    }}\\n'
            '    changed.update(\\n'
            '        {{\\n'
            '            key: None\\n'
            '            for key in legacy_keys\\n'
            '            if key in current\\n'
            '        }}\\n'
            '    )\\n',
        )
        phase_text = phase_text.replace(
            '    for plugin_cfg in containers:\\n'
            '        plugin_cfg.update(updates)\\n',
            '    for plugin_cfg in containers:\\n'
            '        for key, value in updates.items():\\n'
            '            if value is None:\\n'
            '                plugin_cfg.pop(key, None)\\n'
            '            else:\\n'
            '                plugin_cfg[key] = value\\n',
        )
        phase_text = phase_text.replace(
            '    autocapture_snapshot: dict[str, Any] | None = None\\n'
            '    try:\\n'
            '        if args.qa_disable_autocapture:\\n'
            '            autocapture_snapshot = update_openclaw_plugin_config(\\n'
            '                args.openclaw_state_dir,\\n'
            '                {{"autoCapture": False}},\\n'
            '            )\\n'
            '            restart_local_gateway_for_base_url(args.base_url, args.gw_log)\\n',
            '    autocapture_snapshot: dict[str, Any] | None = None\\n'
            '    run_warnings: list[dict[str, Any]] = []\\n'
            '    try:\\n'
            '        if args.qa_disable_autocapture:\\n'
            '            autocapture_snapshot = update_openclaw_plugin_config(\\n'
            '                args.openclaw_state_dir,\\n'
            '                {{"autoCapture": False}},\\n'
            '            )\\n'
            '            try:\\n'
            '                restart_local_gateway_for_base_url(args.base_url, args.gw_log)\\n'
            '            except Exception as exc:\\n'
            '                warning = {{\\n'
            '                    "stage": "qa_disable_autocapture_restart",\\n'
            '                    "error": str(exc),\\n'
            '                }}\\n'
            '                run_warnings.append(warning)\\n'
            '                print(\\n'
            '                    f"[phaseA][warning] gateway restart after disabling autoCapture failed: {{exc}}",\\n'
            '                    file=sys.stderr,\\n'
            '                    flush=True,\\n'
            '                )\\n',
        )
        phase_text = phase_text.replace(
            '    finally:\\n'
            '        if args.qa_disable_autocapture and autocapture_snapshot is not None:\\n'
            '            restore_value = autocapture_snapshot.get("autoCapture", True)\\n'
            '            update_openclaw_plugin_config(\\n'
            '                args.openclaw_state_dir,\\n'
            '                {{"autoCapture": restore_value}},\\n'
            '            )\\n'
            '            restart_local_gateway_for_base_url(args.base_url, args.gw_log)\\n',
            '    finally:\\n'
            '        if args.qa_disable_autocapture and autocapture_snapshot is not None:\\n'
            '            restore_value = autocapture_snapshot.get("autoCapture", True)\\n'
            '            update_openclaw_plugin_config(\\n'
            '                args.openclaw_state_dir,\\n'
            '                {{"autoCapture": restore_value}},\\n'
            '            )\\n'
            '            try:\\n'
            '                restart_local_gateway_for_base_url(args.base_url, args.gw_log)\\n'
            '            except Exception as exc:\\n'
            '                warning = {{\\n'
            '                    "stage": "qa_disable_autocapture_restore_restart",\\n'
            '                    "error": str(exc),\\n'
            '                }}\\n'
            '                run_warnings.append(warning)\\n'
            '                print(\\n'
            '                    f"[phaseA][warning] gateway restart after restoring autoCapture failed: {{exc}}",\\n'
            '                    file=sys.stderr,\\n'
            '                    flush=True,\\n'
            '                )\\n',
        )
        phase_text = phase_text.replace(
            '        "post_ingest_settle": settle_result,\\n'
            '        "gw_log_tail": tail_log(args.gw_log),\\n',
            '        "post_ingest_settle": settle_result,\\n'
            '        "warnings": run_warnings,\\n'
            '        "gw_log_tail": tail_log(args.gw_log),\\n',
        )
        phase_text = phase_text.replace(
            'def wait_for_search_visibility(\\n',
            'def reindex_memory_root(\\n'
            '    *,\\n'
            '    base_url: str,\\n'
            '    api_key: str,\\n'
            '    account_id: str,\\n'
            '    user_id: str,\\n'
            '    timeout: float = 120.0,\\n'
            '    retry_interval: float = 2.0,\\n'
            ') -> dict[str, Any]:\\n'
            '    target_uri = f"viking://user/{{user_id}}/memories"\\n'
            '    headers = {{\\n'
            '        "Content-Type": "application/json",\\n'
            '        "X-API-Key": api_key,\\n'
            '        "X-OpenViking-Account": account_id,\\n'
            '        "X-OpenViking-User": user_id,\\n'
            '    }}\\n'
            '    payload = {{\\n'
            '        "uri": target_uri,\\n'
            '        "mode": "vectors_only",\\n'
            '        "wait": True,\\n'
            '    }}\\n'
            '    deadline = time.monotonic() + max(timeout, 1.0)\\n'
            '    attempts = 0\\n'
            '    last_error = ""\\n'
            '    while time.monotonic() < deadline:\\n'
            '        attempts += 1\\n'
            '        try:\\n'
            '            resp = requests.post(\\n'
            '                base_url.rstrip("/") + "/api/v1/content/reindex",\\n'
            '                headers=headers,\\n'
            '                json=payload,\\n'
            '                timeout=max(30.0, timeout),\\n'
            '            )\\n'
            '            data = resp.json() if resp.content else {{}}\\n'
            '            if resp.ok:\\n'
            '                return {{\\n'
            '                    "ok": True,\\n'
            '                    "attempts": attempts,\\n'
            '                    "target_uri": target_uri,\\n'
            '                    "result": data.get("result", data),\\n'
            '                }}\\n'
            '            last_error = data.get("error", {{}}).get("message") or resp.text or ("HTTP " + str(resp.status_code))\\n'
            '            conflict_type = data.get("error", {{}}).get("details", {{}}).get("conflict_type")\\n'
            '            if resp.status_code == 409 and conflict_type == "path_busy":\\n'
            '                time.sleep(max(retry_interval, 0.1))\\n'
            '                continue\\n'
            '            resp.raise_for_status()\\n'
            '        except Exception as exc:\\n'
            '            last_error = str(exc)\\n'
            '            time.sleep(max(retry_interval, 0.1))\\n'
            '    return {{\\n'
            '        "ok": False,\\n'
            '        "attempts": attempts,\\n'
            '        "target_uri": target_uri,\\n'
            '        "last_error": last_error,\\n'
            '    }}\\n\\n'
            'def wait_for_search_visibility(\\n',
        )
        phase_text = phase_text.replace(
            '    qa_rows: list[dict[str, Any]] = load_existing_qa_rows(paths.csv_path)\\n'
            '    completed_qis = {{int(row.get("qi") or 0) for row in qa_rows}}\\n'
            '    pending_questions = [qa.get("question", "") for qi, qa in qa_items if qi not in completed_qis]\\n'
            '    settle_result: dict[str, Any] | None = None\\n',
            '    qa_rows: list[dict[str, Any]] = load_existing_qa_rows(paths.csv_path)\\n'
            '    completed_qis = set()\\n'
            '    for row in qa_rows:\\n'
            '        completed_qis.add(int(row.get("qi") or 0))\\n'
            '    pending_questions = [qa.get("question", "") for qi, qa in qa_items if qi not in completed_qis]\\n'
            '    reindex_result: dict[str, Any] | None = None\\n'
            '    if not args.skip_ingest and args.ov_api_key:\\n'
            '        print("[phaseA][qa][reindex] rebuilding user memory vectors before QA", file=sys.stderr, flush=True)\\n'
            '        reindex_result = reindex_memory_root(\\n'
            '            base_url=args.openviking_url,\\n'
            '            api_key=args.ov_api_key,\\n'
            '            account_id=str(args.ov_account_id or ""),\\n'
            '            user_id=user,\\n'
            '        )\\n'
            '        print("[phaseA][qa][reindex] result=" + json.dumps(reindex_result, ensure_ascii=False), file=sys.stderr, flush=True)\\n'
            '        resume_state.setdefault("meta", {{}})["post_ingest_reindex"] = reindex_result\\n'
            '        save_resume_state(paths.state_path, resume_state)\\n'
            '        if not reindex_result.get("ok"):\\n'
            '            run_warnings = resume_state.setdefault("meta", {{}}).setdefault("warnings", [])\\n'
            '            run_warnings.append({{"stage": "post_ingest_reindex", "error": reindex_result.get("last_error", "unknown")}})\\n'
            '    settle_result: dict[str, Any] | None = None\\n',
        )
        phase_text = phase_text.replace(
            '        "post_ingest_settle": settle_result,\\n'
            '        "warnings": run_warnings,\\n',
            '        "post_ingest_reindex": reindex_result,\\n'
            '        "post_ingest_settle": settle_result,\\n'
            '        "warnings": run_warnings,\\n',
        )
        phase_path.write_text(phase_text, encoding="utf-8")

        plugin_index_path = Path("/root/.openclaw/extensions/openviking/index.ts")
        if plugin_index_path.exists():
            plugin_index_text = plugin_index_path.read_text(encoding="utf-8")
            plugin_index_text = _ensure_openclaw_openviking_plugin_compat(plugin_index_text)
            plugin_index_path.write_text(plugin_index_text, encoding="utf-8")

        auto_recall_targets = [
            Path("/root/.openclaw/extensions/openviking/auto-recall.ts"),
            Path("/home/jcp/agent/code/OpenViking/examples/openclaw-plugin/auto-recall.ts"),
        ]
        auto_recall_results = []
        for auto_recall_path in auto_recall_targets:
            if not auto_recall_path.exists():
                auto_recall_results.append({{"path": str(auto_recall_path), "status": "missing"}})
                continue
            original = auto_recall_path.read_text(encoding="utf-8")
            updated = _ensure_openclaw_auto_recall_query_extract(original)
            if updated != original:
                auto_recall_path.write_text(updated, encoding="utf-8")
                auto_recall_results.append({{"path": str(auto_recall_path), "status": "patched"}})
            else:
                auto_recall_results.append({{"path": str(auto_recall_path), "status": "unchanged"}})

        compat_targets = [
            (
                Path("/home/jcp/agent/code/OpenViking/openviking/session/memory/session_extract_context_provider.py"),
                "session_extract_context_provider",
            ),
            (
                Path("/home/jcp/agent/code/OpenViking/openviking/session/compressor_v2.py"),
                "extract_agent_memories",
            ),
            (
                Path("/root/.openviking/venv-0.3.24/lib/python3.11/site-packages/openviking/session/memory/session_extract_context_provider.py"),
                "session_extract_context_provider",
            ),
            (
                Path("/root/.openviking/venv-0.3.24/lib64/python3.11/site-packages/openviking/session/memory/session_extract_context_provider.py"),
                "session_extract_context_provider",
            ),
            (
                Path("/root/.openviking/venv-0.3.24/lib/python3.11/site-packages/openviking/session/compressor_v2.py"),
                "extract_agent_memories",
            ),
            (
                Path("/root/.openviking/venv-0.3.24/lib64/python3.11/site-packages/openviking/session/compressor_v2.py"),
                "extract_agent_memories",
            ),
        ]
        compat_results = []
        for compat_path, compat_kind in compat_targets:
            if not compat_path.exists():
                compat_results.append({{"path": str(compat_path), "status": "missing"}})
                continue
            original = compat_path.read_text(encoding="utf-8")
            updated = _ensure_openviking_signature_compat(original, compat_kind)
            if updated != original:
                compat_path.write_text(updated, encoding="utf-8")
                compat_results.append({{"path": str(compat_path), "status": "patched", "kind": compat_kind}})
            else:
                compat_results.append({{"path": str(compat_path), "status": "unchanged", "kind": compat_kind}})

        agents_path = Path("/root/.openclaw/workspace/locomo-eval/AGENTS.md")
        agents_path.parent.mkdir(parents=True, exist_ok=True)
        if agents_path.exists():
            backup = agents_path.with_name("AGENTS.md.bak-20260616-benchmark")
            backup.write_text(agents_path.read_text(encoding="utf-8"), encoding="utf-8")
        agents_path.write_text(base64.b64decode(__AGENTS_B64__).decode("utf-8"), encoding="utf-8")

        config_path = Path("/root/.openclaw/openclaw.json")
        auth_path = Path("/root/.openclaw/agents/main/agent/auth-profiles.json")
        import json
        data = json.loads(config_path.read_text(encoding="utf-8"))
        auth_payload = json.loads(auth_path.read_text(encoding="utf-8")) if auth_path.exists() else {{"profiles": {{}}}}
        for container in [
            data.get("plugins", {{}}).get("entries", {{}}).get("openviking", {{}}).get("config", {{}}),
            data.get("plugins", {{}}).get("openviking", {{}}),
        ]:
            if isinstance(container, dict):
                container.pop("agent_prefix", None)
                container.pop("isolateUserScopeByAgent", None)
                container.pop("isolateAgentScopeByUser", None)
        agents_root = data.setdefault("agents", {{}})
        defaults = agents_root.setdefault("defaults", {{}})
        default_model = __LOCOMO_MODEL__ or (
            defaults.get("model", {{}}).get("primary")
            if isinstance(defaults.get("model"), dict)
            else None
        ) or "volcengine/doubao-seed-2.0-pro"
        if not isinstance(defaults.get("model"), dict):
            defaults["model"] = {{}}
        defaults["model"]["primary"] = default_model
        _inject_openclaw_provider_config(
            data,
            default_model,
            auth_payload.get("profiles", {{}}) if isinstance(auth_payload, dict) else {{}},
        )
        expected_workspace = "/root/.openclaw/workspace/locomo-eval"
        locomo_eval_found = False
        for agent in (data.get("agents", {{}}).get("list") or []):
            if not isinstance(agent, dict):
                continue
            if agent.get("id") != "locomo-eval":
                continue
            locomo_eval_found = True
            agent["model"] = default_model
            agent["workspace"] = expected_workspace
        if not locomo_eval_found:
            raise RuntimeError("locomo-eval agent entry not found in /root/.openclaw/openclaw.json")
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
        verified = json.loads(config_path.read_text(encoding="utf-8"))
        verified_agent = None
        for agent in (verified.get("agents", {{}}).get("list") or []):
            if isinstance(agent, dict) and agent.get("id") == "locomo-eval":
                verified_agent = agent
                break
        if not isinstance(verified_agent, dict):
            raise RuntimeError("locomo-eval agent entry disappeared after writing /root/.openclaw/openclaw.json")
        if verified_agent.get("model") != default_model:
            raise RuntimeError(
                f"locomo-eval model mismatch after prepare: expected {{default_model}}, got {{verified_agent.get('model')}}"
            )
        if verified_agent.get("workspace") != expected_workspace:
            raise RuntimeError(
                f"locomo-eval workspace mismatch after prepare: expected {{expected_workspace}}, got {{verified_agent.get('workspace')}}"
            )
        print(json.dumps({{
            "status": "remote locomo runtime prepared",
            "locomo_eval_model": verified_agent.get("model"),
            "locomo_eval_workspace": verified_agent.get("workspace"),
            "default_model": default_model,
            "openclaw_auto_recall": auto_recall_results,
            "openviking_signature_compat": compat_results,
        }}, ensure_ascii=False))
        """
    ).strip()
    remote_python = remote_python.replace("__BENCHMARK_DIR__", repr(args.benchmark_dir))
    remote_python = remote_python.replace("__LOCOMO_MODEL__", repr(args.locomo_model))
    remote_python = remote_python.replace("__AGENTS_B64__", repr(agents_b64))
    remote_python = remote_python.replace("{{", "{").replace("}}", "}")
    remote_python = "\n".join(
        line[8:] if line.startswith("        ") else line
        for line in remote_python.splitlines()
    )

    remote_cmd = (
        "docker exec -i "
        + args.remote_container
        + " python3 - <<'PY'\n"
        + remote_python
        + "\nPY"
    )
    _run(["ssh", "-p", args.ssh_port, args.ssh_host, remote_cmd])


if __name__ == "__main__":
    main()
