from __future__ import annotations

import re
import subprocess
from typing import Any


_SEMVER_TAG_RE = re.compile(r"^v?(\d+)(?:\.(\d+))(?:\.(\d+))(?:\.(\d+))?$")


def _tag_sort_key(tag: str) -> tuple[int, ...] | None:
    match = _SEMVER_TAG_RE.fullmatch(tag.strip())
    if not match:
        return None
    numbers = [int(part) for part in match.groups() if part is not None]
    return tuple(numbers)


def resolve_latest_release_tag(
    upstream: str,
    *,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", upstream],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except Exception as exc:  # pragma: no cover - defensive path
        return {
            "status": "resolution_failed",
            "upstream": upstream,
            "error": str(exc),
        }

    if proc.returncode != 0:
        return {
            "status": "resolution_failed",
            "upstream": upstream,
            "error": proc.stderr.strip() or proc.stdout.strip() or f"git ls-remote exited with {proc.returncode}",
        }

    candidates: list[tuple[tuple[int, ...], str]] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/")
        sort_key = _tag_sort_key(tag)
        if sort_key is None:
            continue
        candidates.append((sort_key, tag))

    if not candidates:
        return {
            "status": "no_official_release_tag_found",
            "upstream": upstream,
        }

    candidates.sort(key=lambda item: (item[0], item[1]))
    _, tag = candidates[-1]
    return {
        "status": "resolved",
        "upstream": upstream,
        "resolved_version": tag,
        "source": "git_ls_remote_tags",
    }


def build_version_selection(manifest: Any) -> dict[str, Any]:
    policy = manifest.version_policy
    targets: list[dict[str, Any]] = []
    for target in policy.targets:
        target_record = target.model_dump(mode="json")
        if target.version_source == "upstream_release_tag" and target.upstream:
            target_record["resolved_default"] = resolve_latest_release_tag(target.upstream)
        else:
            target_record["resolved_default"] = {
                "status": "runtime_observed_only",
                "upstream": target.upstream,
            }
        targets.append(target_record)
    return {
        "selection_mode": policy.default_selection,
        "overridden": False,
        "targets": targets,
    }
