#!/usr/bin/env python3
"""Diagnose OpenViking local vectordb runtime/persistence split on a remote container."""

from __future__ import annotations

import argparse
import json
import subprocess
import textwrap
from pathlib import Path


def build_remote_script(
    *,
    api_base: str,
    api_key: str,
    account_id: str,
    user_id: str,
    agent_id: str,
    config_path: str,
    vectordb_root: str,
    target_uri: str,
    search_query: str,
) -> str:
    payload = {
        "api_base": api_base,
        "api_key": api_key,
        "account_id": account_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "config_path": config_path,
        "vectordb_root": vectordb_root,
        "target_uri": target_uri,
        "search_query": search_query,
    }
    return textwrap.dedent(
        f"""
        import json
        import os
        import shutil
        import tempfile
        import urllib.request
        from pathlib import Path

        from openviking_cli.utils.config import get_openviking_config
        from openviking.storage.vectordb.store.store_manager import create_store_manager
        from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend
        from openviking.server.identity import RequestContext, Role
        from openviking_cli.session.user_id import UserIdentifier
        from openviking.storage.expr import Eq

        cfg = json.loads({json.dumps(json.dumps(payload, ensure_ascii=False))})
        headers = {{
            "Content-Type": "application/json",
            "X-API-Key": cfg["api_key"],
            "X-OpenViking-Account": cfg["account_id"],
            "X-OpenViking-User": cfg["user_id"],
            "X-OpenViking-Agent": cfg["agent_id"],
        }}

        def http_json(method: str, path: str, body: dict | None = None):
            req = urllib.request.Request(
                cfg["api_base"].rstrip("/") + path,
                data=(json.dumps(body).encode("utf-8") if body is not None else None),
                method=method,
            )
            for k, v in headers.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))

        report: dict = {{}}
        report["config_path"] = cfg["config_path"]
        report["vectordb_root"] = cfg["vectordb_root"]
        report["account_id"] = cfg["account_id"]
        report["target_uri"] = cfg["target_uri"]

        report["observer_vikingdb"] = http_json("GET", "/api/v1/observer/vikingdb")
        report["observer_queue"] = http_json("GET", "/api/v1/observer/queue")
        report["search_find"] = http_json(
            "POST",
            "/api/v1/search/find",
            {{
                "query": cfg["search_query"],
                "target_uri": cfg["target_uri"],
                "limit": 5,
            }},
        )
        report["search_search"] = http_json(
            "POST",
            "/api/v1/search/search",
            {{
                "query": cfg["search_query"],
                "target_uri": cfg["target_uri"],
                "limit": 5,
            }},
        )
        report["consistency"] = http_json(
            "POST",
            "/api/v1/system/consistency",
            {{"uri": cfg["target_uri"]}},
        )

        live_root = Path(cfg["vectordb_root"])
        context_dir = live_root / "context"
        report["live_files"] = {{
            "context_dir_exists": context_dir.exists(),
            "collection_meta_exists": (context_dir / "collection_meta.json").exists(),
            "index_meta_exists": (context_dir / "index" / "default" / "index_meta.json").exists(),
            "versions_dir_exists": (context_dir / "index" / "default" / "versions").exists(),
        }}

        os.environ["OPENVIKING_CONFIG_FILE"] = cfg["config_path"]
        ov_cfg = get_openviking_config()
        backend = VikingVectorIndexBackend(ov_cfg.storage.vectordb)
        fresh_ctx = RequestContext(
            user=UserIdentifier(cfg["account_id"], cfg["user_id"], cfg["agent_id"]),
            role=Role.ROOT,
        )
        import asyncio

        async def probe_fresh():
            rows = await backend.filter(
                filter=Eq("account_id", cfg["account_id"]),
                limit=20,
                output_fields=["id", "uri", "level", "account_id", "context_type"],
                ctx=fresh_ctx,
            )
            return {{
                "collection_exists": await backend.collection_exists(),
                "collection_info": await backend.get_collection_info(),
                "collection_meta": await backend.get_collection_meta(),
                "account_rows": rows,
            }}

        report["fresh_backend"] = asyncio.run(probe_fresh())

        tmp_root = Path(tempfile.mkdtemp(prefix="ov-vdb-diag-"))
        copy_dir = tmp_root / "context"
        shutil.copytree(context_dir, copy_dir)
        candidate_store = create_store_manager("local", str(copy_dir / "store"))
        report["copied_store"] = {{
            "candidate_count": len(candidate_store.get_all_cands_data()),
            "collection_meta_exists": (copy_dir / "collection_meta.json").exists(),
            "index_meta_exists": (copy_dir / "index" / "default" / "index_meta.json").exists(),
        }}
        shutil.rmtree(tmp_root, ignore_errors=True)

        hint = []
        if not report["live_files"]["collection_meta_exists"]:
            hint.append("missing_collection_meta")
        if not report["fresh_backend"]["collection_exists"]:
            hint.append("fresh_backend_cannot_see_collection")
        if report["copied_store"]["candidate_count"] == 0:
            hint.append("candidate_store_empty")
        if report["consistency"].get("result", {{}}).get("missing_record_count", 0) > 0:
            hint.append("consistency_missing_records")
        report["root_cause_hint"] = hint

        print(json.dumps(report, ensure_ascii=False, indent=2))
        """
    )


def run_remote_python(*, host: str, ssh_port: int, container: str, code: str) -> str:
    cmd = [
        "ssh",
        "-p",
        str(ssh_port),
        host,
        f"docker exec -i {container} /root/.openviking/venv-0.3.24/bin/python -",
    ]
    proc = subprocess.run(
        cmd,
        input=code,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"remote python failed ({proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc.stdout


def parse_json_payload(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            payload, end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if text[idx + end :].strip():
            continue
        if isinstance(payload, dict):
            return payload
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="jcp@123.60.114.206")
    parser.add_argument("--ssh-port", type=int, default=10008)
    parser.add_argument("--container", default="jcp-dev")
    parser.add_argument("--api-base", default="http://127.0.0.1:1933")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--user-id", default="eval-1")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--config-path", default="/root/.openviking/ov-0.3.24.conf")
    parser.add_argument("--vectordb-root", default="/root/.openviking/data/vectordb")
    parser.add_argument("--target-uri", default="viking://user/eval-1/memories")
    parser.add_argument("--search-query", default="LGBTQ support group Caroline")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    remote_code = build_remote_script(
        api_base=args.api_base,
        api_key=args.api_key,
        account_id=args.account_id,
        user_id=args.user_id,
        agent_id=args.agent_id,
        config_path=args.config_path,
        vectordb_root=args.vectordb_root,
        target_uri=args.target_uri,
        search_query=args.search_query,
    )
    output = run_remote_python(
        host=args.host,
        ssh_port=args.ssh_port,
        container=args.container,
        code=remote_code,
    )
    parsed = parse_json_payload(output)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        if parsed is not None:
            target.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            target.write_text(output, encoding="utf-8")
    if parsed is not None:
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
