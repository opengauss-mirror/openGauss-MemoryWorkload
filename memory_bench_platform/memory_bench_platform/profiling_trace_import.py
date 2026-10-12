"""Import only current-run numeric memory spans from a source log."""
from __future__ import annotations

import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO


@dataclass
class TraceCursor:
    inode: int | None
    offset: int
    stream: BinaryIO | None


@dataclass(frozen=True)
class TraceRunScope:
    run_id: str
    instance_ids: set[str]
    user_ids: set[str]
    request_ids: set[str]
    start_utc: datetime
    end_utc: datetime
    max_raw_bytes: int = 1073741824


@dataclass(frozen=True)
class TraceImportStatus:
    imported: int
    counts: dict[str, int]


def capture_trace_cursor(source: Path) -> TraceCursor:
    try:
        stream = source.open("rb")
    except FileNotFoundError:
        return TraceCursor(None, 0, None)
    stream.seek(0, 2)
    return TraceCursor(os.fstat(stream.fileno()).st_ino, stream.tell(), stream)


def _stage_map(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("invalid stage map") from exc
    if not isinstance(data, dict) or data.get("schema") != "stage-map/1" or not isinstance(data.get("stages"), dict):
        raise ValueError("unsupported stage map")
    clean = {}
    for name, stage in data["stages"].items():
        if (not isinstance(name, str) or not isinstance(stage, dict)
                or stage.get("role") not in {"leaf", "wrapper"}
                or stage.get("operation") not in {"add", "search", "drain"}
                or not isinstance(stage.get("category"), str)):
            raise ValueError("invalid stage map")
        clean[name] = {key: stage[key] for key in ("category", "role", "operation")}
        if stage.get("async") is True:
            clean[name]["async"] = True
    return {"schema": "stage-map/1", "stages": clean}


def _lines(source: Path, cursor: TraceCursor, limit: int):
    consumed = 0
    streams = []
    current = source.stat() if source.is_file() else None
    truncated = (current is not None and current.st_ino == cursor.inode
                 and current.st_size < cursor.offset)
    if cursor.stream is not None and not truncated:
        streams.append(cursor.stream)
    if current is not None and (cursor.inode is None or current.st_ino != cursor.inode or truncated):
        streams.append(source.open("rb"))
    if truncated and cursor.stream is not None:
        cursor.stream.close()
    try:
        for stream in streams:
            for line in stream:
                consumed += len(line)
                if consumed > limit:
                    raise ValueError("trace source exceeds max_raw_bytes")
                yield line
    finally:
        for stream in streams:
            stream.close()


def import_trace(source: Path, cursor: TraceCursor, run: TraceRunScope, stage_map: Path,
                 output_dir: Path) -> TraceImportStatus:
    if run.start_utc.tzinfo is None or run.end_utc.tzinfo is None or run.end_utc < run.start_utc:
        raise ValueError("invalid trace run window")
    try:
        clean_map = _stage_map(stage_map)
    except BaseException:
        if cursor.stream is not None:
            cursor.stream.close()
        raise
    root = output_dir / "profiling"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    (root / "stage_map.json").write_text(json.dumps(clean_map, sort_keys=True), encoding="utf-8")
    counts = Counter()
    seen = set()
    imported = 0
    output = root / "perf_trace_events.jsonl"
    with output.open("w", encoding="utf-8") as destination:
        for raw in _lines(source, cursor, run.max_raw_bytes):
            try:
                line = raw.decode("utf-8").strip()
                if line.startswith("[MEM_PERF]"):
                    line = line[len("[MEM_PERF]"):].strip()
                row = json.loads(line)
            except (UnicodeError, ValueError):
                counts["malformed_json"] += 1
                continue
            if not isinstance(row, dict) or row.get("kind") != "memory_perf":
                counts["wrong_kind"] += 1
                continue
            required_ids = ("request_id", "instance_id", "run_id") + (("user_id",) if run.user_ids else ())
            if any(not row.get(field) for field in required_ids):
                counts["missing_id"] += 1
                continue
            try:
                ts = datetime.fromisoformat(row["ts"])
            except (KeyError, TypeError, ValueError):
                counts["invalid_timestamp"] += 1
                continue
            if ts.tzinfo is None:
                counts["invalid_timestamp"] += 1
                continue
            if (row["run_id"] != run.run_id or row["instance_id"] not in run.instance_ids
                    or (run.user_ids and row.get("user_id") not in run.user_ids)
                    or (run.request_ids and row["request_id"] not in run.request_ids)
                    or not run.start_utc <= ts <= run.end_utc):
                counts["out_of_scope"] += 1
                continue
            try:
                duration = float(row["duration_ms"])
            except (KeyError, TypeError, ValueError):
                counts["invalid_duration"] += 1
                continue
            if not math.isfinite(duration) or duration < 0:
                counts["negative_duration" if duration < 0 else "invalid_duration"] += 1
                continue
            if row.get("operation") not in {"add", "search", "drain"} or row.get("span_role") not in {"leaf", "wrapper"} or not isinstance(row.get("stage"), str) or not isinstance(row.get("status"), str):
                counts["invalid_span"] += 1
                continue
            span_id = row.get("span_id")
            span_key = (row["run_id"], row["instance_id"], row.get("user_id"), row["request_id"], span_id)
            if span_id and span_key in seen:
                counts["duplicate_span_id"] += 1
                continue
            if span_id:
                seen.add(span_key)
            if row["stage"] not in clean_map["stages"]:
                counts["unknown_stage"] += 1
            clean = {key: row[key] for key in ("kind", "ts", "run_id", "instance_id",
                     "request_id", "operation", "stage", "span_role", "status")}
            clean["user_id"] = row.get("user_id") if run.user_ids else None
            clean["duration_ms"] = duration
            for field in ("span_id", "parent_span_id", "task_id", "trace_id", "start_utc", "end_utc"):
                if isinstance(row.get(field), str):
                    clean[field] = row[field]
            destination.write(json.dumps(clean, ensure_ascii=False) + "\n")
            imported += 1
    output.chmod(0o600)
    return TraceImportStatus(imported, dict(counts))
