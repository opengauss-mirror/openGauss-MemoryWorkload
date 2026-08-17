#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def backup_and_switch(config_path: Path, agent_id: str, model: str) -> Path:
    payload = _load_json(config_path)
    agents = payload.setdefault("agents", {})
    agent_list = agents.setdefault("list", [])
    target = None
    for item in agent_list:
        if item.get("id") == agent_id:
            target = item
            break
    if target is None:
        raise ValueError(f"agent not found: {agent_id}")

    backup_path = config_path.with_name(f"{config_path.name}.model_bak_{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(config_path, backup_path)
    target["model"] = model
    _save_json(config_path, payload)
    return backup_path


def restore_backup(config_path: Path, backup_path: Path) -> None:
    shutil.copy2(backup_path, config_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Swap remote locomo-eval model in openclaw.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p_switch = sub.add_parser("switch")
    p_switch.add_argument("config_path")
    p_switch.add_argument("--agent-id", default="locomo-eval")
    p_switch.add_argument("--model", required=True)

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("config_path")
    p_restore.add_argument("--backup-path", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "switch":
        backup = backup_and_switch(Path(args.config_path), args.agent_id, args.model)
        print(str(backup))
        return 0
    if args.command == "restore":
        restore_backup(Path(args.config_path), Path(args.backup_path))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
