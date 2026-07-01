"""Single-source indicator_result validation (Phase 3, component 10).

One validator, shared by both storage backends, replacing the DuckDB Python
mirror of the Postgres ``indicator_result_before_write`` trigger. Bounds are a
declarative Pandera schema (also attachable as a Hamilton ``@check_output`` for
in-graph fail-fast); the cross-column row rules that don't fit a column schema
(scenario match, applies_to gate, value-column cardinality, missing_reason) stay
explicit and carry the same messages as before. The Postgres trigger remains the
DB-level backstop.

Bounds + applies_to originate in ``IndicatorSpec`` (seeded into
``indicator_catalog``); both this validator and the trigger read the same
values, so there is one source of truth.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pandera.pandas as pa
from pandera.pandas import Check, Column, DataFrameSchema

_MEASURED = ("measured", "estimated", "imputed")


def bounds_schema(
    *, valid_min: float | None, valid_max: float | None
) -> DataFrameSchema:
    """Pandera schema enforcing value_numeric ∈ [valid_min, valid_max].

    The single declarative bounds check — used both here (repo write path) and
    as a Hamilton ``@check_output`` on the dataflow result frame. ``ignore_na``
    lets missing/failed rows (NULL value_numeric) through; cardinality rules
    elsewhere ensure NULLs are only where they belong.
    """
    checks: list[Check] = []
    if valid_min is not None:
        checks.append(
            Check.ge(
                valid_min,
                error=f"indicator_result.value_numeric below valid_min {valid_min}",
                ignore_na=True,
            )
        )
    if valid_max is not None:
        checks.append(
            Check.le(
                valid_max,
                error=f"indicator_result.value_numeric above valid_max {valid_max}",
                ignore_na=True,
            )
        )
    return DataFrameSchema(
        {"value_numeric": Column(float, checks, nullable=True, required=True)}
    )


def _check_row_rules(
    row: dict[str, Any],
    *,
    applies_to: list[str],
    run_scenario: str | None,
) -> None:
    """The cross-column rules the Postgres trigger enforces (same messages)."""
    scenario_id_col = row.get("scenario_id")
    if scenario_id_col != run_scenario:
        raise ValueError(
            f"indicator_result.scenario_id ({scenario_id_col!r}) must match "
            f"job_run.scenario_id ({run_scenario!r})"
        )
    quality_flag = row["quality_flag"]
    feature_type = row["feature_type"]
    if quality_flag != "not_applicable" and feature_type not in applies_to:
        raise ValueError(
            f"feature type {feature_type!r} is not in indicator applies_to "
            f"({applies_to!r}); write quality_flag='not_applicable' to suppress"
        )
    values_set = sum(
        0 if row.get(col) is None else 1
        for col in ("value_numeric", "value_text", "value_json")
    )
    status = row.get("status")
    if quality_flag in _MEASURED and status != "failed":
        if values_set != 1:
            raise ValueError(
                f"indicator_result: exactly one value column must be set for "
                f"quality_flag={quality_flag}"
            )
    elif values_set != 0:
        raise ValueError(
            f"indicator_result: all value columns must be NULL for "
            f"quality_flag={quality_flag} / status={status}"
        )
    if quality_flag == "missing" and not row.get("missing_reason"):
        raise ValueError(
            "indicator_result.missing_reason is required when quality_flag='missing'"
        )


def validate_result_rows(
    rows: list[dict[str, Any]],
    *,
    applies_to: list[str],
    valid_min: float | None,
    valid_max: float | None,
    run_scenario: str | None,
) -> None:
    """Validate indicator_result rows for one indicator/run; raise ValueError.

    Each row dict carries: value_numeric, value_text, value_json, quality_flag,
    status, missing_reason, feature_type, scenario_id. Row rules run first
    (per-row, exact messages); numeric bounds run once over the frame via
    ``bounds_schema`` (Pandera).
    """
    for row in rows:
        _check_row_rules(row, applies_to=applies_to, run_scenario=run_scenario)
    if valid_min is None and valid_max is None:
        return
    frame = pd.DataFrame(
        {"value_numeric": [row.get("value_numeric") for row in rows]}, dtype="float64"
    )
    try:
        bounds_schema(valid_min=valid_min, valid_max=valid_max).validate(
            frame, lazy=True
        )
    except pa.errors.SchemaErrors as exc:
        raise ValueError(str(exc)) from exc
