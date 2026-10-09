"""Import only requests started inside a dedicated memory-system proxy run window."""
import json
import os
import uuid
from pathlib import Path

from . import token_usage as tokens


def configured(ctx):
    return tokens.enabled() and tokens.memory_source(ctx) == "model_proxy"


def snapshot(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    if (manifest.get("schema_version") != "token-proxy/1"
            or manifest.get("component") != "memory" or not manifest.get("memory_backend")
            or not manifest.get("proxy_id")):
        raise ValueError("Invalid memory-system usage proxy manifest")
    p = root / "requests.jsonl"
    with p.open("rb") as f:
        stat = os.fstat(f.fileno())
        raw = f.read(stat.st_size)
    # A writer can be between append and newline. Use only complete records.
    end = raw.rfind(b"\n") + 1
    rows = []
    for line in raw[:end].splitlines():
        row = json.loads(line)
        if (row.get("schema_version") != "token-request/1" or row.get("proxy_id") != manifest["proxy_id"]
                or row.get("event") not in {"started", "finished"} or not row.get("request_id")
                or row.get("model_kind") not in {"llm", "embedding"}):
            raise ValueError("Invalid memory-system usage audit record")
        rows.append(row)
    active = set()
    for row in rows:
        if row["event"] == "started":
            active.add(row["request_id"])
        else:
            active.discard(row["request_id"])
    return {"root": str(root), "proxy_id": manifest["proxy_id"], "memory_backend": manifest["memory_backend"], "inode": stat.st_ino,
            "device": stat.st_dev, "offset": end, "rows": rows, "active": sorted(active)}


def begin(ctx):
    if not configured(ctx):
        return None
    with tokens.scope(ctx):
        def read():
            if os.environ.get("MEMORY_BENCH_TOKEN_PROXY_EXCLUSIVE") != "1":
                raise ValueError("Proxy collection requires an exclusive memory instance")
            root = os.environ.get("MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR")
            if not root:
                raise ValueError("MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR is required")
            start = snapshot(root)
            if start["memory_backend"] != ctx.get("memory_backend"):
                raise ValueError("Usage proxy backend does not match this run")
            # One proxy audit belongs to exactly one run. A persistent exclusive
            # claim prevents overlapping runs from charging the same requests.
            # After a crash, keep the claim and evidence; use a new proxy/audit.
            owner = {"run_id": ctx["run_id"], "run_dir": str(Path(ctx["run_dir"]).resolve()),
                     "claim_id": uuid.uuid4().hex}
            with (Path(root) / "run-owner.json").open("x", encoding="utf-8") as f:
                json.dump(owner, f)
            start = snapshot(root)
            if start["memory_backend"] != ctx.get("memory_backend"):
                raise ValueError("Usage proxy backend changed during initialization")
            if start["active"]:
                raise ValueError("Dedicated memory-system proxy must be idle at run start")
            start.pop("rows")
            start["owner"] = owner
            tokens._atomic(Path(ctx["run_dir"]) / "records/token-usage/model-proxy-start.json", json.dumps(start))
            return start
        return tokens.best_effort(read)


def finish(ctx, start):
    if not configured(ctx):
        return
    with tokens.scope(ctx):
        def collect():
            if not start:
                tokens.warning("token_proxy_window_unavailable")
                return
            owner = json.loads((Path(start["root"]) / "run-owner.json").read_text())
            if (owner != start.get("owner") or owner["run_id"] != ctx["run_id"]
                    or owner["run_dir"] != str(Path(ctx["run_dir"]).resolve())
                    or start["memory_backend"] != ctx.get("memory_backend")):
                raise ValueError("memory-system proxy belongs to another run")
            end = snapshot(start["root"])
            if any(end[k] != start[k] for k in ("proxy_id", "memory_backend", "inode", "device")) or end["offset"] < start["offset"]:
                raise ValueError("memory-system proxy restarted or audit replaced/truncated")
            p = Path(start["root"]) / "requests.jsonl"
            with p.open("rb") as f:
                f.seek(start["offset"])
                raw = f.read(end["offset"] - start["offset"])
            rows = [json.loads(line) for line in raw.splitlines()]
            # Keep the exact numeric-only evidence for offline reconciliation.
            tokens._atomic(Path(ctx["run_dir"]) / "records/token-usage/model-proxy-window.jsonl", raw.decode())
            starts, finishes = {}, {}
            for row in rows:
                target = starts if row["event"] == "started" else finishes
                rid = row["request_id"]
                if rid in target:
                    raise ValueError("Duplicate event in memory-system proxy audit")
                target[rid] = row
            if set(finishes) - set(starts):
                raise ValueError("Foreign request completion in memory-system run window")
            for rid, initial in starts.items():
                final = finishes.get(rid)
                if final and any(initial[k] != final[k] for k in ("model_kind", "endpoint", "model", "proxy_id")):
                    raise ValueError("memory-system proxy request identity changed")
                if not final:
                    tokens.warning("token_proxy_request_unfinished", request_id=rid)
                tokens.record(final.get("usage") if final else None,
                              component="memory", phase="unknown", operation="unknown",
                              source="model_proxy", model_kind=initial["model_kind"], model=initial["model"],
                              granularity="request", record_id=f"model-proxy:{start['proxy_id']}:{rid}",
                              revision=1 if final else 0,
                              outcome=final.get("outcome") if final else "started",
                              http_status=final.get("http_status") if final else None,
                              error_type=final.get("error_type") if final else "unfinished_at_window_end",
                              proxy_id=start["proxy_id"], provider_attempt_id=rid,
                              evidence_ref="records/token-usage/model-proxy-window.jsonl#" + rid)
            tokens._atomic(Path(ctx["run_dir"]) / "records/token-usage/model-proxy-window.json",
                           json.dumps({"proxy_id": start["proxy_id"], "start_offset": start["offset"],
                                       "end_offset": end["offset"], "requests": len(starts),
                                       "unfinished": len(set(starts) - set(finishes)),
                                       "scope": "exclusive memory routes; phase attribution unknown"}, indent=2))
        tokens.best_effort(collect)
