"""Shared storage-layer utilities."""

from __future__ import annotations

import base64
import math
from typing import Any
from uuid import UUID

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

    ⚠ A scenario row shadows the baseline only when it *carries a value*. Keying
    on row existence alone would contradict the ``COALESCE`` rule this is modelled
    on: ``effective_numeric`` resolves ``COALESCE(so.value_numeric,
    obs.value_numeric, base)``, and COALESCE skips a NULL override rather than
    letting it win. ``write_result(value_numeric=None, quality_flag="missing")``
    is a supported call and ``'missing'`` / ``'not_applicable'`` are valid flags,
    so valueless current rows are by design.

    Without the value test, such a row suppresses the baseline *and* is itself
    dropped by the ``value_numeric IS NOT NULL`` filter that ``value_stats`` and
    ``compute_composite`` apply — so the feature disappears from the statistics
    and from every composite that consumes it, rather than falling back. That is
    the same class of hole this function exists to close, one layer down.

    ⚠ The value test belongs on *both* branches, and that is what makes them
    complementary. Guarding only the anti-join stops a valueless scenario row
    from suppressing the baseline, but the ``scenario_id = X`` branch still
    matches that row unconditionally — so both rows qualify and the pair comes
    back twice. That is precisely the double-count the anti-join exists to
    avoid, reintroduced one case down, and it is worse than the widened form
    because it only bites once a scenario has recorded a valueless row. It
    surfaced as an *intermittent* failure rather than a stable one: every reader
    that keys by feature (a dict comprehension, ``dict(row)``, ``fetchone()``)
    collapses the duplicate to whichever row the server sent last, and that
    order follows the query plan — an index scan on ``ir_scenario_slug_idx``
    returns the scenario row first and the baseline wins; once the table has
    statistics the planner switches to a seq scan, physical order puts the
    scenario row last, and the same read reports no value. In
    ``fetch_aoi_children``, which puts this predicate in a ``LEFT JOIN … ON``,
    the duplicate is not cosmetic at all: the feature's ``overlap_measure``
    weight is counted twice and the AOI summary is quietly wrong.

    So a scenario row qualifies when it carries a value *or* when there is no
    baseline row to fall through to — the second half being what keeps a
    ``missing`` row the baseline never computed from vanishing from both
    branches. Exactly one row survives per pair in all four configurations:
    valued scenario row wins; valueless one yields to the baseline; valueless
    one with no baseline is still returned; no scenario row leaves the baseline.

    The trade-off is deliberate: a scenario cannot express "I have recomputed
    this and the answer is now unknown" and have that beat the baseline. Doing
    that needs a distinct signal — a ``quality_flag`` the read layer honours —
    not the absence of a value, which is indistinguishable from "not computed".

    Baseline reads (``scenario_id is None``) are untouched: the predicate stays
    ``IS NULL``, so nothing about existing behaviour moves.
    """
    if not alias or not alias.isidentifier():
        # Not optional, and not style. Unqualified column names inside the
        # NOT EXISTS below bind to the *inner* table first, so `_ov.feature_id =
        # feature_id` would silently become `_ov.feature_id = _ov.feature_id` —
        # always true — and every baseline row would be excluded the moment any
        # scenario row existed. The failure is invisible until a scenario has
        # recomputed something, which is exactly when it matters.
        #
        # `isidentifier()` and not just a truth test, because the sibling
        # `scenario_filter` above takes a *column expression* in this same
        # position. Converting a call site by analogy — `scenario_overlay(
        # "ir.scenario_id", ...)` — otherwise passes the guard and emits
        # `ir.scenario_id.scenario_id`, which Postgres only rejects when that
        # query actually runs.
        raise ValueError(
            "scenario_overlay needs a bare table alias to correlate against "
            f"(got {alias!r}); pass 'ir', not 'ir.scenario_id'"
        )
    prefix = f"{alias}."
    if scenario_id is None:
        return f"{prefix}scenario_id IS NULL", []
    return (
        f"""(({prefix}scenario_id = {placeholder}
              AND ({prefix}value_numeric IS NOT NULL
                OR {prefix}value_text IS NOT NULL
                OR {prefix}value_json IS NOT NULL
                OR NOT EXISTS (
                   SELECT 1 FROM curated_geo.indicator_result _bl
                    WHERE _bl.feature_id = {prefix}feature_id
                      AND _bl.indicator_id = {prefix}indicator_id
                      AND _bl.status = 'current'
                      AND _bl.scenario_id IS NULL)))
             OR ({prefix}scenario_id IS NULL AND NOT EXISTS (
                   SELECT 1 FROM curated_geo.indicator_result _ov
                    WHERE _ov.feature_id = {prefix}feature_id
                      AND _ov.indicator_id = {prefix}indicator_id
                      AND _ov.status = 'current'
                      AND _ov.scenario_id = {placeholder}
                      AND (_ov.value_numeric IS NOT NULL
                        OR _ov.value_text IS NOT NULL
                        OR _ov.value_json IS NOT NULL))))""",
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


def is_run_id(run_id: Any) -> bool:
    """Whether ``run_id`` could name a run row at all.

    ``orchestration_run.id`` is ``uuid`` (r028), so anything that is not one
    cannot be a run — but asked in SQL the question is not answered, it *fails*:
    Postgres raises ``invalid input syntax for type uuid``, psycopg2 surfaces it
    as ``DataError``, and ``apps.api.errors.database_error_handler`` maps every
    non-connection driver error to 500. ``GET /jobs/whatever`` therefore returns
    a server error for what is plainly a client typo, and aborts the request's
    transaction on the way out.

    Lives here rather than in ``orchestration.runstore``, where it started, because
    it now has two callers on opposite sides of a seam: runstore itself, and the
    Ops dashboard's ``OpsRunReader``. Public rather than underscored for the same
    reason — reaching across a package boundary for a private name is the smell
    this move exists to remove.
    """
    if isinstance(run_id, UUID):
        return True
    try:
        UUID(str(run_id))
    except (ValueError, AttributeError, TypeError):
        return False
    return True
