"""Stream a deterministic digest of a named volume's data tree."""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path


def digest_tree(root: Path, marker_name: str) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path == root / marker_name:
            continue
        name = path.relative_to(root).as_posix().encode("utf-8", "surrogateescape")
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            kind = b"D"
        elif stat.S_ISREG(mode):
            kind = b"F"
        elif stat.S_ISLNK(mode):
            kind = b"L"
        else:
            raise ValueError(f"unsupported volume entry: {path}")
        digest.update(kind + len(name).to_bytes(8, "big") + name)
        if kind == b"F":
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif kind == b"L":
            target = os.fsencode(os.readlink(path))
            digest.update(len(target).to_bytes(8, "big") + target)
    return digest.hexdigest()


if __name__ == "__main__":
    print(digest_tree(Path(sys.argv[1]), sys.argv[2]))
