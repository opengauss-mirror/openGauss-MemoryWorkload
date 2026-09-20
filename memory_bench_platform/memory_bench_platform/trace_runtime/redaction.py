from __future__ import annotations

import re
from typing import Any, Iterable, Mapping


SECRET_FIELDS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
        "access_token",
        "refresh_token",
        "token",
        "api_token",
        "client_secret",
        "bearer_token",
        "password",
        "passwd",
        "private_key",
    }
)

_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(token|secret|api[_-]?key|authorization|cookie)$", re.IGNORECASE
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[^\s,;]+")
_BASIC_RE = re.compile(r"(?i)(\bBasic\s+)[^\s,;]+")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|api[_-]?key|token|authorization)=)[^&#\s]+"
)
_OPAQUE_SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16})\b"
)


def _is_secret_key(key: object) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    normalized = normalized.lower().replace("-", "_")
    return normalized in SECRET_FIELDS or bool(_SECRET_KEY_RE.search(normalized))


def _redact_string(value: str) -> str:
    value = _BEARER_RE.sub(r"\1[REDACTED]", value)
    value = _BASIC_RE.sub(r"\1[REDACTED]", value)
    value = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", value)
    return _OPAQUE_SECRET_RE.sub("[REDACTED]", value)


def redact_bytes(value: bytes) -> bytes:
    return _redact_string(value.decode("latin-1")).encode("latin-1")


def _pointer_parts(pointer: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _apply_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return "[REDACTED]"
    current = value
    parts = _pointer_parts(pointer)
    for part in parts[:-1]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return value
    leaf = parts[-1]
    if isinstance(current, dict) and leaf in current:
        current[leaf] = "[REDACTED]"
    elif isinstance(current, list) and leaf.isdigit() and int(leaf) < len(current):
        current[int(leaf)] = "[REDACTED]"
    return value


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key).lower(): _redact_string(str(value))
        for key, value in headers.items()
        if not _is_secret_key(key)
    }


def redact_json(value: Any, pointers: Iterable[str] = ()) -> Any:
    if isinstance(value, dict):
        result = {
            key: "[REDACTED]" if _is_secret_key(key) else redact_json(item)
            for key, item in value.items()
        }
    elif isinstance(value, list):
        result = [redact_json(item) for item in value]
    elif isinstance(value, str):
        result = _redact_string(value)
    else:
        result = value
    for pointer in pointers:
        result = _apply_pointer(result, pointer)
    return result


def contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (_is_secret_key(key) and item != "[REDACTED]") or contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_secret(item) for item in value)
    return isinstance(value, str) and _redact_string(value) != value
