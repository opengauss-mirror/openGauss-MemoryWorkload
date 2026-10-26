import pytest

from memory_bench_platform.deployment_config import GoldenVolume
from memory_bench_platform.deployment_golden import (
    MARKER, classify_volume, expected_manifest, validate_finalize_summary,
    validate_seed_summary,
)


def _golden():
    return GoldenVolume.model_validate({
        "name": "milvus_locomo_golden", "helper_image": "python:3.11-alpine",
        "collection": "memory_001", "users": ["user-001", "user-002"],
        "count_per_user": 10, "dimensions": 1024, "seed_version": "seed-v1",
        "index": {"type": "HNSW", "metric": "L2", "M": 16, "efConstruction": 200},
        "finalize": {"enabled": True, "target": "memory", "command": ["compact"]},
    })


def test_golden_match_uses_baseline_contents_across_service_and_image_changes():
    expected = expected_manifest(_golden(), "vector-new", "sha256:new")
    saved = {**expected, "service": "vector-old", "image_id": "sha256:old"}
    assert classify_volume({MARKER: saved, "etcd": None}, expected) == "ready"
    with pytest.raises(ValueError, match="does not match"):
        classify_volume({MARKER: {**saved, "users": ["other"]}, "etcd": None}, expected)


def test_golden_rejects_partial_or_unsealed_volume():
    expected = expected_manifest(_golden(), "vector", "sha256:image")
    assert classify_volume({}, expected) == "build"
    for entries in ({"etcd": None}, {MARKER: expected}):
        with pytest.raises(ValueError, match="incomplete"):
            classify_volume(entries, expected)


def test_golden_seed_requires_exact_clean_user_counts():
    expected = expected_manifest(_golden(), "vector", "sha256:image")
    seed = {"inserted": 20, "deleted_dynamic": 0, "deleted_seed": 0,
            "results": [{"user_id": user, "validated_count": 10, "reused": False}
                        for user in expected["users"]]}
    validate_seed_summary(seed, expected)
    with pytest.raises(ValueError, match="clean baseline"):
        validate_seed_summary({**seed, "deleted_dynamic": 1}, expected)
    with pytest.raises(ValueError, match="user set"):
        validate_seed_summary({**seed, "results": seed["results"][:1]}, expected)
    with pytest.raises(ValueError, match="user set"):
        validate_seed_summary({**seed, "results": [None]}, expected)


def test_golden_finalize_requires_completed_collection_compaction():
    expected = expected_manifest(_golden(), "vector", "sha256:image")
    validate_finalize_summary({"status": "complete", "collections": [
        {"collection": "memory_001", "state": "Completed"}]}, expected)
    with pytest.raises(ValueError, match="compaction"):
        validate_finalize_summary({"status": "complete", "collections": []}, expected)
