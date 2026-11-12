"""Scenario and edit authoring — the named what-if and the areas drawn over it.

A scenario is ``app_core.scenario``; each rule in it is a row of
``curated_geo.scenario_edit``: an area, an attribute and a value. The CRUD half
of this module only records that intent. ``materialize_scenario`` is the other
half: it resolves the rules into the per-feature rows of
``curated_geo.scenario_override``, which the asset graph already partitions by
scenario, so the ordinary ``evaluate-scenario`` path then recomputes exactly
the affected indicators. It is the only writer of ``materialized_at``.

None of them commits. The caller owns the transaction, as with
``overrides.py``; an API route opens its own committed connection. That matters
most for materialisation, which is seven statements that are only correct
together: a refusal halfway through must leave no overrides behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg2.errors

from reimagina.assets.registry import authorable_attributes
from reimagina.scenarios.ceilings import BlockInputs, CeilingBreach, check_ceilings
from reimagina.scenarios.overrides import (
    _require_postgres,
    apply_overrides,
    effective_numeric,
    publish_scenario_version,
)


class DuplicateScenarioName(Exception):
    """A scenario with this name already exists in the project."""


class InvalidGeometry(Exception):
    """The drawn area is not, and cannot be repaired into, a polygon."""


class ScenarioHasNoAOI(Exception):
    """Materialisation was asked for a scenario with no area of interest.

    ``scenario.aoi_id`` is ``ON DELETE SET NULL`` (r048), so this is reachable
    without anyone editing the scenario: delete the AOI and the column empties
    itself. Treating null as "no scope" rather than "every scope" is the whole
    reason this is an exception. Materialisation selects features by a drawn
    area, and the AOI is the only other bound on that selection — without it a
    rule drawn over three blocks in Mexicali would silently expand to every
    block of that type in the database, including another project's.
    """


class CeilingExceeded(Exception):
    """One or more blocks would compute a value the indicators cannot store.

    Carries every ``CeilingBreach``, not just the first, because the user's
    next action is to lower a number and they need to know how far — and which
    district. ``curated_geo.indicator_result``'s r016 trigger RAISES rather
    than clamps, so the alternative to refusing here is a plpgsql exception
    partway through a city-scale run, naming a row id and nothing a planner
    could act on.
    """

    def __init__(self, breaches: list[CeilingBreach]) -> None:
        self.breaches = breaches
        worst = breaches[0]
        super().__init__(
            f"{len(breaches)} ceiling breach(es); "
            f"feature {worst.feature_id} would compute "
            f"{worst.would_be:.1f} for {worst.indicator_slug}, "
            f"whose valid_max is {worst.bound:.1f}"
        )

    @property
    def feature_ids(self) -> tuple[str, ...]:
        """Every offending feature, sorted and de-duplicated."""
        return tuple(sorted({b.feature_id for b in self.breaches}))


@dataclass(frozen=True)
class MaterializeResult:
    """What one materialisation did.

    ``matched_per_edit`` is per *edit*, keyed by its id, and counts what that
    edit's area caught inside the AOI — before any later edit supersedes it.
    That is the number a drawing UI needs ("your shape caught 4 blocks"), and
    it is deliberately not the number of rows the edit ended up owning: an edit
    fully covered by a later one still caught what it caught, and reporting 0
    for it would read as a mis-drawn area.
    """

    matched_per_edit: dict[str, int]
    overrides_written: int
    overrides_removed: int
    results_superseded: int


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


# ── Materialisation ─────────────────────────────────────────────────────────

#: The three attributes the ceiling kernel needs, in ``BlockInputs`` order.
#:
#: Exactly ``catalog._EDIFICABILITY_READS``; named here rather than imported
#: because ``BlockInputs`` calls the first one ``levels``, so a mapping has to
#: exist somewhere and a tuple that silently reorders itself would be worse
#: than one that is written down.
_CEILING_ATTRIBUTES = ("num_levels", "unit_size", "people_per_household")


def materialize_scenario(conn: Any, *, scenario_id: str) -> MaterializeResult:
    """Resolve a scenario's drawn rules into per-feature override rows.

    Seven steps, in this order, in the caller's one transaction:

    1. refuse without an AOI (``ScenarioHasNoAOI``);
    2. select the features each edit's area covers, scoped to the AOI;
    3. refuse the whole run if any resolved block breaches a ceiling;
    4. delete the overrides no edit implies any more;
    5. upsert the resolved ones in a single ``apply_overrides``;
    6. ``publish_scenario_version`` with **no** ``attributes``;
    7. mark this scenario's now-unbacked ``indicator_result`` rows ``'stale'``.

    **Steps 6 and 7 are the two halves of the same ghost**, and both are easy
    to leave out because nothing fails when you do.

    ``apply_overrides`` publishes only the attributes it just touched, so an
    attribute whose last override row vanished in step 4 would keep a current
    version forever and the indicators reading it would never go stale. Step 6
    passes no ``attributes`` precisely so ``publish_scenario_version``
    reconciles retirements too — see its docstring.

    ``bulk_write_numeric_results`` marks stale only the features it is
    *writing*. A block that lost one of the three edificability inputs is
    skipped by the compute, never appears in the written rows, and keeps its
    old ``status='current'`` scenario row — which
    ``serving_geo.indicator_overlay_tile`` goes on painting. Step 7 is what
    makes "remove every edit" visibly remove the choropleth.

    Nothing is written before step 3, so a refusal is total rather than
    partial: a scenario that breaches a ceiling keeps whatever it was
    materialised to last time, and ``materialized_at`` does not move.
    """
    _require_postgres(conn)
    aoi_id = _scenario_aoi(conn, scenario_id)
    matched, resolved, feature_type = _resolve_edits(conn, scenario_id, aoi_id)
    _refuse_ceiling_breaches(conn, resolved=resolved, feature_type=feature_type)

    removed = _delete_unimplied_overrides(conn, scenario_id, resolved)
    written = apply_overrides(
        conn,
        scenario_id=scenario_id,
        edits=[
            {"feature_id": feature_id, "attribute_key": key, "value_numeric": value}
            for (feature_id, key), value in sorted(resolved.items())
        ],
    )
    # No ``attributes``: this is the call that retires an attribute whose last
    # override row step 4 deleted.
    publish_scenario_version(conn, scenario_id)
    superseded = _supersede_unbacked_results(conn, scenario_id, removed)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_core.scenario SET materialized_at = now() WHERE id = %s",
            (scenario_id,),
        )
    return MaterializeResult(
        matched_per_edit=matched,
        overrides_written=written,
        overrides_removed=len(removed),
        results_superseded=superseded,
    )


def _scenario_aoi(conn: Any, scenario_id: str) -> str:
    """Step 1: the scenario's AOI, or a refusal. See ``ScenarioHasNoAOI``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT aoi_id FROM app_core.scenario WHERE id = %s", (scenario_id,)
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"scenario {scenario_id} does not exist")
    if row[0] is None:
        raise ScenarioHasNoAOI(
            f"scenario {scenario_id} has no area of interest, so there is no "
            "bound on what its edits would select. Set one (or re-create the "
            "AOI it pointed at) before materialising."
        )
    return str(row[0])


def _resolve_edits(
    conn: Any, scenario_id: str, aoi_id: str
) -> tuple[dict[str, int], dict[tuple[str, str], float], dict[str, str]]:
    """Step 2: ``(matched_per_edit, {(feature, attribute): value}, feature types)``.

    ``ST_Covers(e.geometry, f.geometry_centroid)`` is the platform's one answer
    to "did my drawn shape catch this?" — the same predicate
    ``count_features_within_polygon`` uses (r027) and the same one
    ``selection-filter.ts`` mirrors client-side, so the count the user saw
    while drawing is the count they get here. ``ST_Covers`` rather than
    ``ST_Contains`` because a grid-aligned manzana's ``ST_PointOnSurface`` can
    land exactly on a shared block edge, which DE-9IM's exclusive boundary
    would silently drop.

    The AOI test is an ``EXISTS`` against ``aoi_feature`` rather than an
    intersection with the AOI polygon, so materialisation scopes by exactly the
    membership every other read scopes by — including the identifier-based
    membership r040 introduced, which no geometry test can reproduce.

    A ``LEFT JOIN``, so an edit whose area caught nothing still appears in
    ``matched_per_edit`` with 0. An inner join would make "I drew in the wrong
    place" indistinguishable from "that edit does not exist".

    Collisions resolve by ``ordinal``, later wins, never by ``created_at``:
    ``now()`` is transaction-start time, so two edits written together are
    indistinguishable by timestamp. Iterating in ordinal order and letting a
    later write overwrite is the whole of that rule.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id::text, e.attribute_key, e.value_numeric,
                   f.id::text, f.type_id::text
            FROM curated_geo.scenario_edit e
            LEFT JOIN curated_geo.feature f
                   ON f.type_id = e.type_id
                  AND ST_Covers(e.geometry, f.geometry_centroid)
                  AND EXISTS (
                        SELECT 1 FROM curated_geo.aoi_feature af
                         WHERE af.aoi_id = %(aoi)s AND af.feature_id = f.id
                      )
            WHERE e.scenario_id = %(s)s
            ORDER BY e.ordinal
            """,
            {"s": scenario_id, "aoi": aoi_id},
        )
        rows = cur.fetchall()

    matched: dict[str, int] = {}
    resolved: dict[tuple[str, str], float] = {}
    feature_type: dict[str, str] = {}
    for edit_id, attribute_key, value, feature_id, type_id in rows:
        matched.setdefault(edit_id, 0)
        if feature_id is None:
            continue
        matched[edit_id] += 1
        resolved[(feature_id, attribute_key)] = float(value)
        feature_type[feature_id] = type_id
    return matched, resolved, feature_type


def _refuse_ceiling_breaches(
    conn: Any,
    *,
    resolved: dict[tuple[str, str], float],
    feature_type: dict[str, str],
) -> None:
    """Step 3: one ``BlockInputs`` per resolved block, each with its own area.

    Per block, not per selection. Two edits can resolve to different values on
    different blocks — six storeys in one district and twelve in another is an
    ordinary proposal — and the two area-scaled bounds then have nothing in
    common. A single triple paired with the selection's maximum area describes
    no real block: it clears the breach in the taller district or invents one
    in the shorter.

    The triple is the *post*-materialisation effective value: the resolved
    override where this run writes one, otherwise observation ⊕ base. Reading
    it at ``scenario_id=None`` is deliberate — an override row this run is
    about to delete in step 4 must not stand in for a value that will be gone.

    A missing input reads as 0, which ``check_ceilings`` skips. That is not a
    silent pass: a block missing any of the three is skipped by the compute
    too, so it writes no result and no bound can be breached.

    Deliberately not narrowed to ``MEX:block``. The bounds belong to the
    edificability indicators, which compute at block grain today, and refusing
    a value that could not have reached them is a visible, correctable error —
    missing one is a plpgsql exception that takes down a whole run.
    """
    feature_ids = sorted({feature_id for feature_id, _ in resolved})
    if not feature_ids:
        return

    by_type: dict[str, list[str]] = {}
    for feature_id in feature_ids:
        by_type.setdefault(feature_type[feature_id], []).append(feature_id)
    baseline: dict[str, dict[str, float]] = {k: {} for k in _CEILING_ATTRIBUTES}
    for type_id, ids in by_type.items():
        for key in _CEILING_ATTRIBUTES:
            baseline[key].update(
                effective_numeric(
                    conn,
                    type_id=type_id,
                    attribute_key=key,
                    scenario_id=None,
                    feature_ids=ids,
                )
            )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, ST_Area(geometry::geography) "
            "FROM curated_geo.feature WHERE id = ANY(%s::uuid[])",
            (feature_ids,),
        )
        areas = dict(cur.fetchall())

    def _value(feature_id: str, key: str) -> float:
        return resolved.get((feature_id, key), baseline[key].get(feature_id, 0.0))

    breaches = check_ceilings(
        BlockInputs(
            feature_id=feature_id,
            levels=_value(feature_id, "num_levels"),
            unit_size=_value(feature_id, "unit_size"),
            people_per_household=_value(feature_id, "people_per_household"),
            area_m2=float(areas.get(feature_id, 0.0)),
        )
        for feature_id in feature_ids
    )
    if breaches:
        raise CeilingExceeded(breaches)


def _delete_unimplied_overrides(
    conn: Any, scenario_id: str, resolved: dict[tuple[str, str], float]
) -> list[tuple[str, str]]:
    """Step 4: drop this scenario's override rows no surviving edit implies.

    Returns the ``(feature_id, attribute_key)`` pairs removed, because step 7
    needs exactly those ids and they are gone by the time it runs.

    An anti-join against the resolved pairs rather than "delete everything and
    re-insert". The rows carry no state worth keeping, but deleting them all
    would churn the version digest in ``publish_scenario_version`` for a
    scenario nobody edited, and the whole point of a content digest is that
    re-saving an unchanged what-if recomputes nothing.

    With nothing resolved the ``unnest`` yields no rows and every override goes
    — which is right: an edit-less scenario overrides nothing.
    """
    pairs = sorted(resolved)
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM curated_geo.scenario_override o
            WHERE o.scenario_id = %(s)s
              AND NOT EXISTS (
                    SELECT 1
                      FROM unnest(%(fids)s::uuid[], %(keys)s::text[])
                           AS implied(feature_id, attribute_key)
                     WHERE implied.feature_id = o.feature_id
                       AND implied.attribute_key = o.attribute_key
                  )
            RETURNING o.feature_id::text, o.attribute_key
            """,
            {
                "s": scenario_id,
                "fids": [feature_id for feature_id, _ in pairs],
                "keys": [key for _, key in pairs],
            },
        )
        return [(feature_id, key) for feature_id, key in cur.fetchall()]


def _supersede_unbacked_results(
    conn: Any, scenario_id: str, removed: list[tuple[str, str]]
) -> int:
    """Step 7: stale the scenario results the removed overrides were behind.

    The half of a removal a user can see. Without it, removing every edit from
    a scenario leaves the choropleth untouched: the recompute skips a block
    that lost one of its inputs, ``bulk_write_numeric_results`` stales only the
    features it writes, and the old ``status='current'`` row stays current
    forever.

    Paired per ``(feature, attribute)`` rather than crossed. Staling every
    indicator that reads *any* removed attribute on every feature that lost
    *any* override would be a safe over-approximation, but it would mark a
    block's results stale over an attribute that block never lost — and a
    stale row that nothing will rewrite is the same lie in the other
    direction.

    ``indicator_slug`` is denormalised onto the row by r016's trigger and
    indexed with ``feature_id``, so no join to the catalog is needed. The
    trigger refuses a change to it, not to ``status``.
    """
    if not removed:
        return 0
    from reimagina.indicators.catalog import SHIPPED_INDICATORS

    pairs: set[tuple[str, str]] = set()
    for feature_id, attribute_key in removed:
        for spec in SHIPPED_INDICATORS:
            if attribute_key in spec.reads:
                pairs.add((spec.slug, feature_id))
    if not pairs:
        return 0

    ordered = sorted(pairs)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE curated_geo.indicator_result r
               SET status = 'stale', updated_at = now()
              FROM unnest(%(slugs)s::text[], %(fids)s::uuid[])
                   AS unbacked(slug, feature_id)
             WHERE r.scenario_id = %(s)s
               AND r.status = 'current'
               AND r.indicator_slug = unbacked.slug
               AND r.feature_id = unbacked.feature_id
            """,
            {
                "s": scenario_id,
                "slugs": [slug for slug, _ in ordered],
                "fids": [feature_id for _, feature_id in ordered],
            },
        )
        return cur.rowcount


def _stringify_ids(row: dict) -> dict:
    for key in ("id", "project_id", "aoi_id", "type_id"):
        if row.get(key) is not None:
            row[key] = str(row[key])
    return row
