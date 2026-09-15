from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from threading import Lock
from typing import TextIO
import uuid

from pydantic import ValidationError

from .protocol import TraceBundleChannel, TraceBundleManifest, TraceRecord
from .redaction import contains_secret, redact_bytes


@dataclass(frozen=True)
class LoadedBundle:
    path: Path
    manifest: TraceBundleManifest
    records: dict[str, list[TraceRecord]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decoded_raw(value: str | None) -> bytes | None:
    if value is None:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64 payload in trace record") from exc


def _raw_contains_secret(raw: bytes | None) -> bool:
    if raw is None:
        return False
    if redact_bytes(raw) != raw:
        return True
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        data_lines = [
            line[5:].lstrip()
            for line in raw.replace(b"\r\n", b"\n").splitlines()
            if line.startswith(b"data:")
        ]
        if not data_lines:
            return False
        try:
            payload = json.loads(b"\n".join(data_lines))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
    return contains_secret(payload)


def _validate_record_secrets(record: TraceRecord) -> None:
    values = [
        record.trace_id,
        record.scope.model_dump(mode="json"),
        record.request.path,
        record.request.headers,
        record.request.body,
        record.response.headers,
        record.response.body,
        *[frame.data for frame in record.response.stream_frames],
    ]
    raw_values = [
        _decoded_raw(record.request.raw_body_base64),
        _decoded_raw(record.response.raw_body_base64),
        *[_decoded_raw(frame.raw_base64) for frame in record.response.stream_frames],
    ]
    if any(contains_secret(value) for value in values) or any(
        _raw_contains_secret(raw) for raw in raw_values
    ):
        raise ValueError("trace_secret_detected")


class BundleWriter:
    def __init__(
        self,
        output_path: Path,
        *,
        bundle_id: str,
        profile_id: str,
        channels: dict[str, dict[str, str]],
        source_run_id: str | None = None,
    ):
        invalid_channels = [
            name
            for name in channels
            if Path(name).name != name or name in {"", ".", ".."}
        ]
        if invalid_channels:
            raise ValueError(f"invalid trace channel name: {invalid_channels[0]!r}")
        self.output_path = output_path
        self.bundle_id = bundle_id
        self.profile_id = profile_id
        self.source_run_id = source_run_id
        self.channels = channels
        self._temp_path = output_path.parent / f".{output_path.name}.tmp-{uuid.uuid4().hex}"
        self._temp_path.mkdir(parents=True)
        self._counts = {name: 0 for name in channels}
        self._request_ids: set[str] = set()
        self._lock = Lock()
        self._finalized = False
        self._streams: dict[str, TextIO] = {}
        for name in channels:
            (self._temp_path / name).mkdir(parents=True)
            self._streams[name] = (
                self._temp_path / name / "records.jsonl"
            ).open("a", encoding="utf-8")

    def _close_streams(self, *, sync: bool) -> None:
        for stream in self._streams.values():
            if stream.closed:
                continue
            stream.flush()
            if sync:
                os.fsync(stream.fileno())
            stream.close()

    def append(self, record: TraceRecord) -> None:
        if record.channel not in self.channels:
            raise ValueError(f"unknown trace channel: {record.channel}")
        request_id = record.scope.request_id
        _validate_record_secrets(record)
        with self._lock:
            if self._finalized:
                raise RuntimeError("trace bundle has already been finalized")
            if request_id and request_id in self._request_ids:
                raise ValueError(f"duplicate request_id: {request_id}")
            if request_id:
                self._request_ids.add(request_id)
            expected = self._counts[record.channel] + 1
            if record.ordinal != expected:
                record = record.model_copy(update={"ordinal": expected})
            stream = self._streams[record.channel]
            stream.write(record.model_dump_json())
            stream.write("\n")
            self._counts[record.channel] = expected

    def finalize(self) -> TraceBundleManifest:
        with self._lock:
            if self._finalized:
                raise RuntimeError("trace bundle has already been finalized")
            if self.output_path.exists():
                raise FileExistsError(f"trace bundle output already exists: {self.output_path}")
            self._close_streams(sync=True)
            manifest_channels: dict[str, TraceBundleChannel] = {}
            for name, config in self.channels.items():
                records_path = self._temp_path / name / "records.jsonl"
                records_path.touch(exist_ok=True)
                manifest_channels[name] = TraceBundleChannel(
                    protocol=config["protocol"],
                    records=f"{name}/records.jsonl",
                    record_count=self._counts[name],
                    sha256=_sha256(records_path),
                    match_mode=config["match_mode"],
                    order_scope=config["order_scope"],
                )
            manifest = TraceBundleManifest(
                bundle_id=self.bundle_id,
                profile_id=self.profile_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                source_run_id=self.source_run_id,
                channels=manifest_channels,
                producer={"package": "memory-bench-platform"},
            )
            manifest_path = self._temp_path / "manifest.json"
            manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            os.replace(self._temp_path, self.output_path)
            self._finalized = True
            return manifest

    def discard(self) -> None:
        with self._lock:
            if not self._finalized and self._temp_path.exists():
                self._close_streams(sync=False)
                shutil.rmtree(self._temp_path)


def load_bundle(path: Path) -> LoadedBundle:
    manifest_path = path / "manifest.json"
    try:
        manifest = TraceBundleManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        raise ValueError(f"invalid trace bundle manifest: {exc}") from exc

    records_by_channel: dict[str, list[TraceRecord]] = {}
    request_ids: set[str] = set()
    bundle_root = path.resolve()
    for name, channel in manifest.channels.items():
        records_path = (path / channel.records).resolve()
        try:
            records_path.relative_to(bundle_root)
        except ValueError as exc:
            raise ValueError(f"trace records path escapes bundle root: {channel.records}") from exc
        if not records_path.is_file():
            raise ValueError(f"trace records file does not exist: {channel.records}")
        actual_sha256 = _sha256(records_path)
        if actual_sha256 != channel.sha256:
            raise ValueError(
                f"trace records sha256 mismatch for {name}: "
                f"expected {channel.sha256}, got {actual_sha256}"
            )
        records: list[TraceRecord] = []
        with records_path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = TraceRecord.model_validate_json(line)
                except (ValidationError, ValueError) as exc:
                    raise ValueError(
                        f"invalid trace record schema_version or payload in "
                        f"{channel.records}:{line_number}: {exc}"
                    ) from exc
                _validate_record_secrets(record)
                if record.channel != name:
                    raise ValueError(f"trace record channel mismatch in {channel.records}:{line_number}")
                if record.protocol != channel.protocol:
                    raise ValueError(f"trace record protocol mismatch in {channel.records}:{line_number}")
                if record.ordinal != len(records) + 1:
                    raise ValueError(f"trace record ordinal is not monotonic in {channel.records}")
                for frame_index, frame in enumerate(record.response.stream_frames, 1):
                    if frame.ordinal != frame_index:
                        raise ValueError(
                            f"trace stream frame ordinal is not monotonic in "
                            f"{channel.records}:{line_number}"
                        )
                request_id = record.scope.request_id
                if request_id and request_id in request_ids:
                    raise ValueError(f"duplicate request_id: {request_id}")
                if request_id:
                    request_ids.add(request_id)
                records.append(record)
        if len(records) != channel.record_count:
            raise ValueError(
                f"trace record count mismatch for {name}: "
                f"expected {channel.record_count}, got {len(records)}"
            )
        records_by_channel[name] = records
    return LoadedBundle(path=path, manifest=manifest, records=records_by_channel)
