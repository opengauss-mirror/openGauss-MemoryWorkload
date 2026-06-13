from __future__ import annotations

import json
import shutil
import subprocess

MIN_PLUGIN_VERSION = (2026, 4, 8)


def parse_version_tuple(version_text: str) -> tuple[int, int, int] | None:
    parts = version_text.strip().split()
    if len(parts) < 2:
        return None
    nums = parts[1].split(".")
    if len(nums) < 3:
        return None
    try:
        return tuple(int(x) for x in nums[:3])
    except ValueError:
        return None


def version_gte(left: tuple[int, int, int] | None, right: tuple[int, int, int]) -> bool:
    if left is None:
        return False
    return left >= right


def main() -> None:
    binary = shutil.which("openclaw")
    if not binary:
        print(json.dumps({"status": "missing", "binary": None}, ensure_ascii=False))
        return
    version = subprocess.run([binary, "--version"], text=True, capture_output=True, timeout=15)
    parsed_version = parse_version_tuple(version.stdout)
    try:
        health = subprocess.run([binary, "health"], text=True, capture_output=True, timeout=15)
        health_payload = {
            "health_exit_code": health.returncode,
            "health_stdout": health.stdout.strip(),
            "health_stderr": health.stderr.strip(),
            "health_timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        health_payload = {
            "health_exit_code": None,
            "health_stdout": (exc.stdout or "").strip(),
            "health_stderr": (exc.stderr or "").strip(),
            "health_timeout": True,
        }
    try:
        plugin_status = subprocess.run(
            [binary, "openviking", "status", "--json"],
            text=True,
            capture_output=True,
            timeout=8,
        )
        plugin_payload = {
            "plugin_status_exit_code": plugin_status.returncode,
            "plugin_status_stdout": plugin_status.stdout.strip(),
            "plugin_status_stderr": plugin_status.stderr.strip(),
            "plugin_status_timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        plugin_payload = {
            "plugin_status_exit_code": None,
            "plugin_status_stdout": (exc.stdout or "").strip(),
            "plugin_status_stderr": (exc.stderr or "").strip(),
            "plugin_status_timeout": True,
        }
    print(
        json.dumps(
            {
                "status": "ok",
                "binary": binary,
                "version_exit_code": version.returncode,
                "version_stdout": version.stdout.strip(),
                "required_min_version": ".".join(str(x) for x in MIN_PLUGIN_VERSION),
                "parsed_version": None if parsed_version is None else ".".join(str(x) for x in parsed_version),
                "plugin_version_supported": version_gte(parsed_version, MIN_PLUGIN_VERSION),
                **health_payload,
                **plugin_payload,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
