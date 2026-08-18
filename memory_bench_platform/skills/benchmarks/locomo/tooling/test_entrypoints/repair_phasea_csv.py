#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import shutil
import sys
import time
from pathlib import Path
from typing import Iterable


DEFAULT_ERROR_MARKERS = (
    "Request timed out before a response was generated.",
    "[ERROR] ConnectionError",
)


def _read_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV missing header: {csv_path}")
        return list(reader.fieldnames), list(reader)


def detect_repair_qis(
    rows: Iterable[dict[str, str]],
    error_markers: Iterable[str] = DEFAULT_ERROR_MARKERS,
) -> list[int]:
    markers = tuple(marker for marker in error_markers if marker)
    repair_qis: list[int] = []
    for row in rows:
        raw_qi = row.get("qi", "")
        try:
            qi = int(raw_qi)
        except ValueError:
            continue
        response = row.get("response", "") or ""
        if any(marker in response for marker in markers):
            repair_qis.append(qi)
    return sorted(set(repair_qis))


def write_seed_csv(
    source_csv: Path,
    seed_csv: Path,
    *,
    remove_qis: Iterable[int],
) -> int:
    fieldnames, rows = _read_rows(source_csv)
    removed = set(remove_qis)
    kept_rows = []
    for row in rows:
        try:
            qi = int(row.get("qi", ""))
        except ValueError:
            kept_rows.append(row)
            continue
        if qi not in removed:
            kept_rows.append(row)

    seed_csv.parent.mkdir(parents=True, exist_ok=True)
    with seed_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept_rows)
    return len(kept_rows)


def merge_replayed_rows(
    main_csv: Path,
    replay_csv: Path,
    *,
    replace_qis: Iterable[int],
    create_backup: bool = True,
) -> tuple[Path | None, int]:
    fieldnames, main_rows = _read_rows(main_csv)
    _replay_fieldnames, replay_rows = _read_rows(replay_csv)

    replace_set = set(replace_qis)
    replay_map: dict[int, dict[str, str]] = {}
    for row in replay_rows:
        try:
            qi = int(row.get("qi", ""))
        except ValueError:
            continue
        if qi in replace_set:
            replay_map[qi] = row

    backup_path: Path | None = None
    if create_backup:
        backup_path = main_csv.with_name(f"{main_csv.name}.merge_bak_{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(main_csv, backup_path)

    replaced = 0
    for idx, row in enumerate(main_rows):
        try:
            qi = int(row.get("qi", ""))
        except ValueError:
            continue
        replay_row = replay_map.get(qi)
        if replay_row is not None:
            main_rows[idx] = replay_row
            replaced += 1

    with main_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(main_rows)
    return backup_path, replaced


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repair timed-out phaseA CSV rows")
    sub = parser.add_subparsers(dest="command", required=True)

    p_detect = sub.add_parser("detect")
    p_detect.add_argument("csv_path")

    p_seed = sub.add_parser("seed")
    p_seed.add_argument("source_csv")
    p_seed.add_argument("seed_csv")
    p_seed.add_argument("--qis", required=True, help="Comma-separated qi list to remove from seed csv")

    p_merge = sub.add_parser("merge")
    p_merge.add_argument("main_csv")
    p_merge.add_argument("replay_csv")
    p_merge.add_argument("--qis", required=True, help="Comma-separated qi list to replace in main csv")
    p_merge.add_argument("--no-backup", action="store_true")

    return parser


def _parse_qis(raw: str) -> list[int]:
    qis: list[int] = []
    for item in raw.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        qis.append(int(stripped))
    return sorted(set(qis))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "detect":
        _fieldnames, rows = _read_rows(Path(args.csv_path))
        qis = detect_repair_qis(rows)
        print(",".join(str(qi) for qi in qis))
        return 0
    if args.command == "seed":
        count = write_seed_csv(
            Path(args.source_csv),
            Path(args.seed_csv),
            remove_qis=_parse_qis(args.qis),
        )
        print(count)
        return 0
    if args.command == "merge":
        backup_path, replaced = merge_replayed_rows(
            Path(args.main_csv),
            Path(args.replay_csv),
            replace_qis=_parse_qis(args.qis),
            create_backup=not args.no_backup,
        )
        print(
            {
                "replaced": replaced,
                "backup_path": str(backup_path) if backup_path else "",
            }
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
