from __future__ import annotations

import json
import os
import shutil
import subprocess


def main() -> None:
    binary = shutil.which("hermes")
    if not binary:
        print(json.dumps({"status": "missing", "binary": None}, ensure_ascii=False))
        return
    version = subprocess.run([binary, "--version"], text=True, capture_output=True, timeout=15)
    memory_status = subprocess.run([binary, "memory", "status"], text=True, capture_output=True, timeout=20)
    provider = os.environ.get("HERMES_PROVIDER")
    model = os.environ.get("HERMES_MODEL")
    oneshot_cmd = [binary, "chat", "-q", "Reply with exactly OK", "-Q", "--ignore-rules"]
    if model:
        oneshot_cmd += ["--model", model]
    if provider:
        oneshot_cmd += ["--provider", provider]
    oneshot = subprocess.run(
        oneshot_cmd,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    print(
        json.dumps(
            {
                "status": "ok" if oneshot.returncode == 0 and bool(oneshot.stdout.strip()) else "degraded",
                "binary": binary,
                "version_stdout": version.stdout.strip(),
                "memory_status_stdout": memory_status.stdout.strip(),
                "memory_status_stderr": memory_status.stderr.strip(),
                "oneshot_exit_code": oneshot.returncode,
                "oneshot_stdout": oneshot.stdout.strip(),
                "oneshot_stderr": oneshot.stderr.strip(),
                "oneshot_non_empty": bool(oneshot.stdout.strip()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
