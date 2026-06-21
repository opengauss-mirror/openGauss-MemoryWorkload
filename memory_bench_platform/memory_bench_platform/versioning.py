from __future__ import annotations

import json
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


def build_version_selection(
    manifest: Any,
    *,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    policy = manifest.version_policy
    overrides = overrides or {}
    targets: list[dict[str, Any]] = []
    overridden = False
    for target in policy.targets:
        target_record = target.model_dump(mode="json")
        if target.version_source == "upstream_release_tag" and target.upstream:
            target_record["resolved_default"] = resolve_latest_release_tag(target.upstream)
        else:
            target_record["resolved_default"] = {
                "status": "runtime_observed_only",
                "upstream": target.upstream,
            }
        selected_version = None
        selected_by = "latest_official_release_tag"
        override_version = overrides.get(target.name)
        if override_version:
            selected_version = override_version
            selected_by = "user_specified_official_version"
            overridden = True
        else:
            resolved_default = target_record.get("resolved_default") or {}
            if isinstance(resolved_default, dict):
                selected_version = resolved_default.get("resolved_version")
        target_record["selected_version"] = selected_version
        target_record["selected_by"] = selected_by
        targets.append(target_record)
    return {
        "selection_mode": "user_specified_official_version" if overridden else policy.default_selection,
        "overridden": overridden,
        "overrides": dict(overrides),
        "targets": targets,
    }


def _target_env_key(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", name.strip()).strip("_")
    return normalized.upper()


def build_external_runner_env(version_selection: dict[str, Any]) -> dict[str, str]:
    env = {
        "MEMORY_BENCH_VERSION_SELECTION_JSON": json.dumps(version_selection, ensure_ascii=False),
    }
    for section_name, section_payload in version_selection.items():
        if not isinstance(section_payload, dict):
            continue
        selection_mode = section_payload.get("selection_mode")
        if isinstance(selection_mode, str) and selection_mode:
            env[f"MEMORY_BENCH_{section_name.upper()}_SELECTION_MODE"] = selection_mode
        for target in section_payload.get("targets", []):
            if not isinstance(target, dict):
                continue
            target_name = target.get("name")
            if not isinstance(target_name, str) or not target_name.strip():
                continue
            resolved_default = target.get("resolved_default")
            if not isinstance(resolved_default, dict):
                continue
            resolved_version = target.get("selected_version") or resolved_default.get("resolved_version")
            if not isinstance(resolved_version, str) or not resolved_version.strip():
                continue
            env_key = _target_env_key(target_name)
            env[f"MEMORY_BENCH_EXPECTED_{env_key}_VERSION"] = resolved_version
            upstream = target.get("upstream")
            if isinstance(upstream, str) and upstream.strip():
                env[f"MEMORY_BENCH_EXPECTED_{env_key}_UPSTREAM"] = upstream
    return env
