"""Validate reusable Milvus baseline volumes before deployment writes."""
from __future__ import annotations

from .deployment_config import GoldenVolume


MARKER = ".memory-bench-golden.json"
_BASELINE_FIELDS = ("schema_version", "collection", "users", "count_per_user",
                    "dimensions", "seed_version", "index")


def expected_manifest(config: GoldenVolume, service: str, image_id: str) -> dict:
    return {
        "schema_version": 1,
        "service": service,
        "image_id": image_id,
        "collection": config.collection,
        "users": config.users,
        "count_per_user": config.count_per_user,
        "dimensions": config.dimensions,
        "seed_version": config.seed_version,
        "index": config.index.model_dump(by_alias=True),
    }


def baseline_matches(saved: dict, expected: dict) -> bool:
    return all(saved.get(field) == expected.get(field) for field in _BASELINE_FIELDS)


def classify_volume(entries: dict, expected: dict) -> str:
    if not entries:
        return "build"
    marker = entries.get(MARKER)
    if not isinstance(marker, dict) or len(entries) < 2:
        raise ValueError("Golden volume is incomplete; use a new volume name")
    if not baseline_matches(marker, expected):
        raise ValueError("Golden volume manifest does not match this baseline")
    return "ready"


def validate_seed_summary(summary: dict, manifest: dict) -> None:
    expected = set(manifest["users"])
    rows = summary.get("results", [])
    actual = ({row.get("user_id") for row in rows}
              if isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
              else set())
    if actual != expected or len(rows) != len(expected):
        raise ValueError("seed summary user set does not match Golden manifest")
    count = manifest["count_per_user"]
    if summary.get("inserted") != len(expected) * count:
        raise ValueError("Golden build did not insert the exact baseline row count")
    if summary.get("deleted_dynamic") != 0 or summary.get("deleted_seed") != 0:
        raise ValueError("Golden build must start from a clean baseline without deletes")
    if any(row.get("validated_count") != count or row.get("reused") for row in rows):
        raise ValueError("Golden build contains reused or incomplete user data")


def validate_finalize_summary(summary: dict, manifest: dict) -> None:
    rows = summary.get("collections", [])
    if (summary.get("status") != "complete" or not isinstance(rows, list)
            or len(rows) != 1 or not isinstance(rows[0], dict)
            or rows[0].get("collection") != manifest["collection"]
            or rows[0].get("state") != "Completed"):
        raise ValueError("Golden compaction is not complete for the expected collection")
