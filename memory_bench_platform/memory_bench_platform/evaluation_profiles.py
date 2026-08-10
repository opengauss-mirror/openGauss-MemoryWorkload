from __future__ import annotations

from typing import Any


def metric_envelope(
    *,
    primary_metric: str,
    metrics: list[dict[str, Any]],
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the common scorer envelope used by benchmark adapters."""
    return {
        "primary_metric": primary_metric,
        "metrics": metrics,
        "artifacts": artifacts or [],
    }
