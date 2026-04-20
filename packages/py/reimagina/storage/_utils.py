"""Shared storage-layer utilities."""
from __future__ import annotations

import base64
import math
from typing import Any

import pandas as pd


def normalize_json_value(value):
    """Convert numpy/pandas-like values into JSON-safe Python values."""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {str(k): normalize_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [normalize_json_value(v) for v in value]
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return normalize_json_value(value.tolist())
        except (AttributeError, TypeError, ValueError):
            pass
    if hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return normalize_json_value(value.item())
        except (AttributeError, TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (AttributeError, TypeError, ValueError):
            pass
    try:
        missing = pd.isna(value)
    except Exception:
        missing = False
    if isinstance(missing, bool) and missing:
        return None
    return value


def scenario_filter(
    column: str,
    scenario_id: str | None,
    placeholder: str = "%s",
) -> tuple[str, list]:
    """Build a NULL-safe scenario_id WHERE clause fragment.

    Returns ``(sql, params)`` so callers can append to their clause lists
    without repeating the if/else. ``column`` may be qualified
    (``"ir.scenario_id"``) or wrapped in a cast
    (``"CAST(scenario_id AS VARCHAR)"``) — the helper does not introspect it.
    """
    if scenario_id is None:
        return f"{column} IS NULL", []
    return f"{column} = {placeholder}", [scenario_id]


def encode_cursor(created_at: Any, feature_id: str) -> str:
    """Encode a (created_at, id) pair as an opaque, URL-safe pagination cursor."""
    ts = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
    payload = f"{ts}|{feature_id}"
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Decode a cursor into (created_at_iso, feature_id). Raises ValueError on bad input."""
    try:
        payload = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, fid = payload.split("|", 1)
        return ts, fid
    except Exception as exc:
        raise ValueError(f"Invalid pagination cursor: {cursor!r}") from exc
