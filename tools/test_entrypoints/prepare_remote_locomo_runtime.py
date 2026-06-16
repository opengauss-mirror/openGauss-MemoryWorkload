#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import subprocess
import textwrap
from pathlib import Path


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _encode_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch remote LoCoMo/OpenClaw runtime for benchmark runs.")
    parser.add_argument("--ssh-host", default="jcp@123.60.114.206")
    parser.add_argument("--ssh-port", default="10008")
    parser.add_argument("--remote-container", default="jcp-dev")
    parser.add_argument(
        "--benchmark-dir",
        default="/home/jcp/agent/code/OpenViking/benchmark/locomo/openclaw",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    agents_path = script_dir / "remote_overrides" / "locomo_eval_AGENTS.md"
    agents_b64 = _encode_file(agents_path)

    remote_python = textwrap.dedent(
        f"""
        import base64
        from pathlib import Path

        benchmark_dir = Path({args.benchmark_dir!r})

        shell_path = benchmark_dir / "run_clean_small_in_container.sh"
        shell_text = shell_path.read_text(encoding="utf-8")
        shell_text = shell_text.replace('cfg["agent_prefix"] = account_id\\n', '')
        shell_text = shell_text.replace('cfg["isolateUserScopeByAgent"] = isolate_user_scope_by_agent\\n', '')
        shell_text = shell_text.replace('cfg["isolateAgentScopeByUser"] = isolate_agent_scope_by_user\\n', '')
        shell_path.write_text(shell_text, encoding="utf-8")

        phase_path = benchmark_dir / "phase_a_off.py"
        phase_text = phase_path.read_text(encoding="utf-8")
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
        phase_path.write_text(phase_text, encoding="utf-8")

        agents_path = Path("/root/.openclaw/workspace/locomo-eval/AGENTS.md")
        agents_path.parent.mkdir(parents=True, exist_ok=True)
        if agents_path.exists():
            backup = agents_path.with_name("AGENTS.md.bak-20260616-benchmark")
            backup.write_text(agents_path.read_text(encoding="utf-8"), encoding="utf-8")
        agents_path.write_text(base64.b64decode({agents_b64!r}).decode("utf-8"), encoding="utf-8")

        config_path = Path("/root/.openclaw/openclaw.json")
        import json
        data = json.loads(config_path.read_text(encoding="utf-8"))
        for container in [
            data.get("plugins", {{}}).get("entries", {{}}).get("openviking", {{}}).get("config", {{}}),
            data.get("plugins", {{}}).get("openviking", {{}}),
        ]:
            if isinstance(container, dict):
                container.pop("agent_prefix", None)
                container.pop("isolateUserScopeByAgent", None)
                container.pop("isolateAgentScopeByUser", None)
        defaults = data.get("agents", {{}}).get("defaults", {{}})
        default_model = (
            defaults.get("model", {{}}).get("primary")
            if isinstance(defaults.get("model"), dict)
            else None
        ) or "volcengine/doubao-seed-2.0-pro"
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
        }}, ensure_ascii=False))
        """
    ).strip()

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
