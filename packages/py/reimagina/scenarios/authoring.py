"""Scenario and edit authoring — the named what-if and the areas drawn over it.

A scenario is ``app_core.scenario``; each rule in it is a row of
``curated_geo.scenario_edit``: an area, an attribute and a value. Nothing here
resolves an edit into per-block overrides — that is a later step, which is also
the only writer of ``materialized_at``. These functions only record intent.

None of them commits. The caller owns the transaction, as with
``overrides.py``; an API route opens its own committed connection.
"""

from __future__ import annotations

from typing import Any

import psycopg2.errors

from reimagina.assets.registry import authorable_attributes
from reimagina.scenarios.overrides import _require_postgres


class DuplicateScenarioName(Exception):
    """A scenario with this name already exists in the project."""


class InvalidGeometry(Exception):
    """The drawn area is not, and cannot be repaired into, a polygon."""


def create_scenario(
    conn: Any,
    *,
    project_id: str,
    name: str,
    aoi_id: str | None = None,
    description: str | None = None,
) -> str:
    """Create a scenario and return its id.

    ``UNIQUE (project_id, name)`` is what lets a scenario be addressed by name,
    so a clash is a user error and surfaces as ``DuplicateScenarioName`` rather
    than a driver exception a route would turn into a 500. The insert runs in a
    savepoint: a failed statement aborts the whole Postgres transaction, and the
    caller's earlier work in it should not be collateral.
    """
    _require_postgres(conn)
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT create_scenario")
        try:
            cur.execute(
                """
                INSERT INTO app_core.scenario (project_id, aoi_id, name, description)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (project_id, aoi_id, name, description),
            )
        except psycopg2.errors.UniqueViolation as exc:
            cur.execute("ROLLBACK TO SAVEPOINT create_scenario")
            raise DuplicateScenarioName(
                f"scenario {name!r} already exists in this project"
            ) from exc
        scenario_id = str(cur.fetchone()[0])
        cur.execute("RELEASE SAVEPOINT create_scenario")
        return scenario_id


def list_scenarios(
    conn: Any, *, project_id: str | None = None, aoi_id: str | None = None
) -> list[dict]:
    """Scenarios, newest first, optionally narrowed by project and/or AOI."""
    _require_postgres(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, aoi_id, name, description,
                   created_at, updated_at, materialized_at
            FROM app_core.scenario
            WHERE (%(p)s::uuid IS NULL OR project_id = %(p)s::uuid)
              AND (%(a)s::uuid IS NULL OR aoi_id = %(a)s::uuid)
            ORDER BY created_at DESC, name
            """,
            {"p": project_id, "a": aoi_id},
        )
        cols = [d[0] for d in cur.description]
        return [
            _stringify_ids(dict(zip(cols, row, strict=True))) for row in cur.fetchall()
        ]


def get_scenario(conn: Any, scenario_id: str) -> dict | None:
    """One scenario with its ``edits`` (in ordinal order) and ``override_count``.

    ``materialized_at`` is read, never written, here. ``None`` means the edits
    have not been expanded into overrides yet, which is a different statement
    from "expanded, and there was nothing to do".
    """
    _require_postgres(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, aoi_id, name, description,
                   created_at, updated_at, materialized_at,
                   (SELECT count(*) FROM curated_geo.scenario_override o
                     WHERE o.scenario_id = s.id) AS override_count
            FROM app_core.scenario s
            WHERE id = %s
            """,
            (scenario_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        scenario = _stringify_ids(dict(zip(cols, row, strict=True)))
        cur.execute(
            """
            SELECT id, ordinal, type_id, attribute_key, value_numeric,
                   ST_AsText(geometry) AS geometry_wkt, created_at
            FROM curated_geo.scenario_edit
            WHERE scenario_id = %s
            ORDER BY ordinal
            """,
            (scenario_id,),
        )
        cols = [d[0] for d in cur.description]
        scenario["edits"] = [
            _stringify_ids(dict(zip(cols, r, strict=True))) for r in cur.fetchall()
        ]
        return scenario


def add_edit(
    conn: Any,
    *,
    scenario_id: str,
    geometry_wkt: str,
    type_id: str,
    attribute_key: str,
    value_numeric: float,
) -> str:
    """Append one edit to a scenario and return its id.

    * ``attribute_key`` must be in ``authorable_attributes()``; anything else is
      plumbing, not a policy lever, and raises ``ValueError``.
    * The geometry is repaired on write with ``ST_MakeValid`` and then reduced
      to its polygonal part. The column CHECK is a *type* check, so a
      self-intersecting bowtie, which is still an ``ST_Polygon``, passes it; an
      invalid polygon reaching ``ST_Covers`` later either raises a GEOS
      exception mid-run or answers wrongly. If nothing polygonal survives
      (``POLYGON EMPTY``, a line, unparseable text) the result is
      ``InvalidGeometry``.
    * ``ordinal`` is ``max + 1`` under a row lock on the scenario. Without the
      ``FOR UPDATE``, two concurrent calls read the same max, both insert it,
      and the second hits ``UNIQUE (scenario_id, ordinal)`` — a 500 on an
      ordinary double-submit. The lock is released with the caller's
      transaction, so a caller that holds one open for long serialises writers.
      ``ordinal`` rather than ``created_at`` orders edits because ``now()`` is
      transaction-start time: two edits written together share it.
    """
    _require_postgres(conn)
    allowed = authorable_attributes()
    if attribute_key not in allowed:
        raise ValueError(
            f"{attribute_key!r} is not an authorable attribute; "
            f"choose one of {', '.join(allowed)}"
        )

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT add_edit_geometry")
        try:
            cur.execute(
                """
                SELECT ST_AsEWKB(g)
                FROM (SELECT ST_CollectionExtract(
                          ST_MakeValid(ST_SetSRID(ST_GeomFromText(%s), 4326)), 3
                      ) AS g) r
                WHERE NOT ST_IsEmpty(g)
                """,
                (geometry_wkt,),
            )
        except (
            psycopg2.errors.InvalidParameterValue,
            psycopg2.errors.InternalError_,
        ) as exc:
            cur.execute("ROLLBACK TO SAVEPOINT add_edit_geometry")
            raise InvalidGeometry(f"geometry could not be parsed: {exc}") from exc
        repaired = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT add_edit_geometry")
        if repaired is None:
            raise InvalidGeometry(
                "geometry has no polygonal area after repair "
                "(empty, or a point or line rather than a polygon)"
            )

        cur.execute(
            "SELECT id FROM app_core.scenario WHERE id = %s FOR UPDATE", (scenario_id,)
        )
        if cur.fetchone() is None:
            raise LookupError(f"scenario {scenario_id} does not exist")
        cur.execute(
            """
            INSERT INTO curated_geo.scenario_edit
              (scenario_id, ordinal, geometry, type_id, attribute_key, value_numeric)
            SELECT %(s)s,
                   COALESCE(max(ordinal), 0) + 1,
                   ST_GeomFromEWKB(%(g)s), %(t)s, %(k)s, %(v)s
            FROM curated_geo.scenario_edit
            WHERE scenario_id = %(s)s
            RETURNING id
            """,
            {
                "s": scenario_id,
                "g": repaired[0],
                "t": type_id,
                "k": attribute_key,
                "v": value_numeric,
            },
        )
        edit_id = str(cur.fetchone()[0])
        cur.execute(
            "UPDATE app_core.scenario SET updated_at = now() WHERE id = %s",
            (scenario_id,),
        )
        return edit_id


def remove_edit(conn: Any, *, edit_id: str) -> bool:
    """Delete one edit; ``True`` if it existed.

    Ordinals are not renumbered: they only have to be ordered, not dense, and
    renumbering would rewrite rows other readers may hold ids for.
    """
    _require_postgres(conn)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM curated_geo.scenario_edit WHERE id = %s RETURNING scenario_id",
            (edit_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        cur.execute(
            "UPDATE app_core.scenario SET updated_at = now() WHERE id = %s", (row[0],)
        )
        return True


def _stringify_ids(row: dict) -> dict:
    for key in ("id", "project_id", "aoi_id", "type_id"):
        if row.get(key) is not None:
            row[key] = str(row[key])
    return row
