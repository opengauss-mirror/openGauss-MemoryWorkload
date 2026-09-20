from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal


Operation = Literal["add", "search"]


@dataclass(frozen=True)
class ReplayDataset:
    add_path: Path
    search_path: Path


@dataclass(frozen=True)
class ReplayRecord:
    operation: Operation
    line_number: int
    request_id: str
    request: dict[str, Any]
    payload_sha256: str
    payload_size_bytes: int
    top_level_fields: tuple[str, ...]
    baseline: dict[str, Any]


def _has_operation_token(name: str, operation: Operation) -> bool:
    stem = name[: -len(".jsonl")]
    return re.search(rf"(?:^|[_-]){operation}(?:$|[_-])", stem, re.IGNORECASE) is not None


def discover_dataset(path: Path) -> ReplayDataset:
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"dataset directory not found: {root}")

    files = [
        item
        for item in root.iterdir()
        if item.is_file() and not item.is_symlink() and item.name.lower().endswith(".jsonl")
    ]
    candidates = {
        operation: sorted(
            (item for item in files if _has_operation_token(item.name, operation)),
            key=lambda item: item.name,
        )
        for operation in ("add", "search")
    }
    for operation, matches in candidates.items():
        if len(matches) != 1:
            names = ", ".join(item.name for item in matches) or "none"
            reason = "missing" if not matches else "ambiguous"
            raise ValueError(f"{reason} {operation} JSONL in {root}: {names}")
    return ReplayDataset(add_path=candidates["add"][0], search_path=candidates["search"][0])


def _canonical_request_bytes(request: dict[str, Any]) -> bytes:
    return json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _request_id(operation: str, line_number: int, digest: str) -> str:
    return f"{operation}-{line_number:06d}-{digest[:12]}"


def _result_count(response: dict[str, Any]) -> int | None:
    for value in response.values():
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            nested = _result_count(value)
            if nested is not None:
                return nested
    return None


def _baseline(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("code", "success", "status", "state"):
        value = response.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if key in response:
                result[key] = value
    count = _result_count(response)
    if count is not None:
        result["result_count"] = count
    return result


def iter_replay_records(
    path: Path, operation: Operation, run_id: str
) -> Iterator[ReplayRecord]:
    if operation not in ("add", "search"):
        raise ValueError(f"unsupported operation: {operation}")
    source = Path(path)
    with source.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                envelope = json.loads(line)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError(f"{source.name}: line {line_number}: invalid JSON") from exc
            if not isinstance(envelope, dict):
                raise ValueError(f"{source.name}: line {line_number}: row must be an object")
            request = envelope.get("request")
            if not isinstance(request, dict):
                raise ValueError(
                    f"{source.name}: line {line_number}: request must be an object"
                )

            canonical = _canonical_request_bytes(request)
            digest = hashlib.sha256(canonical).hexdigest()
            replay_request = copy.deepcopy(request)
            if replay_request.get("user_id") == "":
                replay_request["user_id"] = f"replay-{run_id}"

            yield ReplayRecord(
                operation=operation,
                line_number=line_number,
                request_id=_request_id(operation, line_number, digest),
                request=replay_request,
                payload_sha256=digest,
                payload_size_bytes=len(canonical),
                top_level_fields=tuple(sorted(request)),
                baseline=_baseline(envelope.get("response")),
            )


def validate_dataset(path: Path) -> dict[str, Any]:
    dataset = discover_dataset(path)
    records = {
        "add": iter_replay_records(dataset.add_path, "add", "validation"),
        "search": iter_replay_records(dataset.search_path, "search", "validation"),
    }
    counts: dict[str, int] = {}
    shapes: dict[str, list[list[str]]] = {}
    for operation, items in records.items():
        count = 0
        unique_shapes: set[tuple[str, ...]] = set()
        for record in items:
            count += 1
            unique_shapes.add(record.top_level_fields)
        counts[operation] = count
        shapes[operation] = [list(shape) for shape in sorted(unique_shapes)]
    return {
        "status": "ok",
        "source_path": str(Path(path)),
        "add_file": dataset.add_path.name,
        "search_file": dataset.search_path.name,
        "add_count": counts["add"],
        "search_count": counts["search"],
        "request_shapes": shapes,
    }
