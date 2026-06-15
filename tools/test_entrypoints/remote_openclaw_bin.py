#!/usr/bin/env python3
from __future__ import annotations

import os
import shlex
import subprocess
import sys


def main() -> int:
    ssh_host = os.environ.get("REMOTE_OPENCLAW_SSH_HOST", "jcp@123.60.114.206")
    ssh_port = os.environ.get("REMOTE_OPENCLAW_SSH_PORT", "10008")
    remote_container = os.environ.get("REMOTE_OPENCLAW_CONTAINER", "jcp-dev")
    openclaw_bin = os.environ.get("REMOTE_OPENCLAW_BIN", "openclaw")

    inner_cmd = shlex.join([openclaw_bin, *sys.argv[1:]])
    remote_cmd = f"docker exec {shlex.quote(remote_container)} bash -lc {shlex.quote(inner_cmd)}"
    proc = subprocess.run(
        ["ssh", "-p", ssh_port, ssh_host, remote_cmd],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
