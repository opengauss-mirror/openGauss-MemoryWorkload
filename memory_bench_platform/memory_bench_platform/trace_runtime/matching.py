from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import json
from threading import Lock
from typing import Iterable, Literal

from memory_bench_platform.protocol import TraceCounterSnapshot

from .protocol import ProviderRequest, TraceRecord, TraceScope


MatchMode = Literal["strict", "fingerprint", "compat", "ordered"]


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def matching_key(
    request: ProviderRequest,
    mode: MatchMode,
    scope: TraceScope,
    order_scope: Literal["global", "session", "user", "fingerprint"] = "fingerprint",
) -> str:
    body = request.body if isinstance(request.body, dict) else request.body
    if mode == "compat":
        inputs = body.get("input", []) if isinstance(body, dict) else []
        input_count = len(inputs) if isinstance(inputs, list) else 1
        return _digest(
            {
                "path": request.path,
                "input_count": input_count,
                "model": body.get("model") if isinstance(body, dict) else None,
                "encoding_format": body.get("encoding_format") if isinstance(body, dict) else None,
                "dimensions": body.get("dimensions") if isinstance(body, dict) else None,
            }
        )
    if mode == "ordered":
        if order_scope == "session":
            return scope.session_id or "missing-session"
        if order_scope == "user":
            return scope.user_id or "missing-user"
        if order_scope == "global":
            return "global"
        return _digest({"path": request.path, "body": body})
    normalized_body = dict(body) if isinstance(body, dict) else body
    if mode == "fingerprint" and isinstance(normalized_body, dict):
        normalized_body.pop("stream", None)
    return _digest(
        {
            "method": request.method.upper(),
            "path": request.path,
            "body": normalized_body,
        }
    )


@dataclass
class _Counters:
    loaded: int
    matched: int = 0
    mismatched: int = 0
    errors: int = 0
    active: int = 0
    queued: int = 0
    peak_active: int = 0


@dataclass
class _QueueEntry:
    record: TraceRecord
    remaining: int


class ReplayMatcher:
    def __init__(
        self,
        *,
        records: Iterable[TraceRecord],
        match_mode: MatchMode,
        copies: int,
        order_scope: Literal["global", "session", "user", "fingerprint"] = "fingerprint",
    ):
        self.match_mode = match_mode
        self.order_scope = order_scope
        self._queues: dict[str, deque[_QueueEntry]] = defaultdict(deque)
        loaded = 0
        for record in records:
            key = matching_key(record.request, match_mode, record.scope, order_scope)
            self._queues[key].append(_QueueEntry(record=record, remaining=copies))
            loaded += copies
        self._counters = _Counters(loaded=loaded)
        self._lock = Lock()

    def match(
        self,
        request: ProviderRequest,
        scope: TraceScope,
        *,
        hold_active: bool = False,
    ) -> TraceRecord | None:
        key = matching_key(request, self.match_mode, scope, self.order_scope)
        with self._lock:
            self._counters.active += 1
            self._counters.peak_active = max(
                self._counters.peak_active, self._counters.active
            )
            if (
                self.match_mode == "ordered"
                and self.order_scope == "session"
                and not scope.session_id
            ) or (
                self.match_mode == "ordered"
                and self.order_scope == "user"
                and not scope.user_id
            ):
                self._counters.mismatched += 1
                if not hold_active:
                    self._counters.active -= 1
                return None
            queue = self._queues.get(key)
            if queue:
                entry = queue[0]
                record = entry.record
                entry.remaining -= 1
                if entry.remaining == 0:
                    queue.popleft()
            else:
                record = None
            if record is None:
                self._counters.mismatched += 1
            else:
                self._counters.matched += 1
            if not hold_active:
                self._counters.active -= 1
            return record

    def finish_request(self) -> None:
        with self._lock:
            self._counters.active = max(0, self._counters.active - 1)

    def record_error(self) -> None:
        with self._lock:
            self._counters.errors += 1

    def snapshot(self) -> TraceCounterSnapshot:
        with self._lock:
            remaining = self._counters.loaded - self._counters.matched
            return TraceCounterSnapshot(
                loaded=self._counters.loaded,
                matched=self._counters.matched,
                mismatched=self._counters.mismatched,
                remaining=remaining,
                errors=self._counters.errors,
                active=self._counters.active,
                queued=self._counters.queued,
                peak_active=self._counters.peak_active,
            )
