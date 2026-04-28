"""CLI helpers for the canonical feature_type_catalog."""

from __future__ import annotations

from reimagina.data.feature import FEATURE_TYPE_ROWS, seed_feature_types
from reimagina.db import connect


def seed_types(*, dsn: str) -> dict[str, object]:
    """Insert the canonical MEX + global feature types. Idempotent."""
    with connect(dsn) as conn:
        seed_feature_types(conn)
    return {
        "dsn": dsn,
        "seeded_count": len(FEATURE_TYPE_ROWS),
    }
