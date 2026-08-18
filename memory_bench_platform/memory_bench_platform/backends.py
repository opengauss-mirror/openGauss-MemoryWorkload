from __future__ import annotations

from pathlib import Path


def validate_openviking_source(
    source_path: str,
    *,
    api_base: str,
    api_key: str,
    vlm_model: str,
    embedding_model: str,
) -> dict:
    root = Path(source_path).resolve()
    required = {
        "README.md": (root / "README.md").exists(),
        "pyproject.toml": (root / "pyproject.toml").exists(),
        "docs": (root / "docs").is_dir(),
    }
    return {
        "status": "ok" if all(required.values()) else "invalid",
        "source_path": str(root),
        "checks": required,
        "suggested_config": {
            "vlm": {
                "provider": "volcengine",
                "model": vlm_model,
                "api_key": api_key,
                "api_base": api_base,
            },
            "embedding": {
                "provider": "volcengine",
                "model": embedding_model,
                "api_key": api_key,
                "api_base": api_base,
            },
        },
    }
