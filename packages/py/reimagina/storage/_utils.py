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


def scenario_overlay(
    alias: str,
    scenario_id: str | None,
    placeholder: str = "%s",
) -> tuple[str, list]:
    """Read a scenario as an *overlay*: its own rows, else the baseline's.

    A scenario stores only what it changed, so reading one means "this
    scenario's row where it has one, the baseline's otherwise" — the same
    ``COALESCE(override, observation, base)`` rule
    ``scenarios.overrides.effective_numeric`` already applies to indicator
    *inputs*, finally applied to their outputs. Without it, every indicator a
    scenario did not recompute simply has no value under that scenario: the map
    shows a hole, and ``compute_composite`` intersects its components to nothing
    and writes zero rows.

    ``alias`` is the ``indicator_result`` alias in the caller's query (``"ir"``,
    ``"civ"`` for the serving view). It is required — see below.

    ⚠ Expressed as an anti-join, not as ``scenario_id = X OR scenario_id IS
    NULL``. That widening looks equivalent and silently double-counts: where a
    scenario *has* recomputed a feature, both rows match, so an aggregate
    averages that feature twice and a join returns it twice. Here a baseline row
    qualifies only when no scenario row exists for the same
    (feature, indicator) — so exactly one row survives per pair, and the
    predicate drops into a WHERE clause, a JOIN condition or an aggregate
    unchanged, with no DISTINCT ON and no restructuring of the caller's query.

    Baseline reads (``scenario_id is None``) are untouched: the predicate stays
    ``IS NULL``, so nothing about existing behaviour moves.
    """
    if not alias:
        # Not optional, and not style. Unqualified column names inside the
        # NOT EXISTS below bind to the *inner* table first, so `_ov.feature_id =
        # feature_id` would silently become `_ov.feature_id = _ov.feature_id` —
        # always true — and every baseline row would be excluded the moment any
        # scenario row existed. The failure is invisible until a scenario has
        # recomputed something, which is exactly when it matters.
        raise ValueError(
            "scenario_overlay needs an explicit table alias to correlate against"
        )
    prefix = f"{alias}."
    if scenario_id is None:
        return f"{prefix}scenario_id IS NULL", []
    return (
        f"""({prefix}scenario_id = {placeholder}
             OR ({prefix}scenario_id IS NULL AND NOT EXISTS (
                   SELECT 1 FROM curated_geo.indicator_result _ov
                    WHERE _ov.feature_id = {prefix}feature_id
                      AND _ov.indicator_id = {prefix}indicator_id
                      AND _ov.status = 'current'
                      AND _ov.scenario_id = {placeholder})))""",
        [scenario_id, scenario_id],
    )


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
