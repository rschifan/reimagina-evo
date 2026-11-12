"""Delta tiles: a scenario's difference from the baseline, as a tile.

``GET /scenarios/{id}/comparison`` already answers "what did my what-if do?"
in numbers — per feature ``{baseline, scenario, delta, state}``, from
``reimagina.scenarios.comparison``. This migration paints the same answer on
the map: ``indicator_overlay_tile`` takes an optional ``delta`` parameter and
then emits ``delta`` and ``state`` per feature in place of ``value_numeric``
and ``quality_flag``.

**One function, two views.** Extending the existing function rather than
adding a second one keeps ``reimagina.tiles.functions.TILE_FUNCTIONS`` and
``infra/tiles/layers.json`` naming one relation for the choropleth channel,
and keeps the signature ``(z, x, y, query_params json) RETURNS bytea`` that
makes Martin and the API's own ``/tiles/{function}/{z}/{x}/{y}`` route
interchangeable. ``layers.json`` still documents the new parameter, because
the *contract* did change even though the signature did not.

**The classification is not restated here, it is reproduced.** The rules are
``comparison._joined`` and ``comparison._classify``, and a tile that
disagreed with the endpoint by one block would be a bug a user finds in the
first minute — the summary saying "6 changed" over a map colouring 7. So,
literally:

======== ============== ============= ========================
baseline scenario row   state         delta
======== ============== ============= ========================
yes      yes, different ``changed``   ``scenario - baseline``
yes      yes, equal     ``unchanged`` ``0``
yes      none           ``unchanged`` ``0``
none     yes            ``new``       NULL
======== ============== ============= ========================

Three details carry that table, and each one is a way of getting it wrong:

- the effective value is ``COALESCE(scenario, baseline)``, resolved in the
  select list *before* anything is classified. A scenario stores only what it
  changed, so classifying on the raw scenario column would make every block
  outside the drawn area — nearly all of them, on a real AOI — read as having
  no value, and a map that should say "we changed three blocks" would say "we
  have no data".
- both sides filter ``value_numeric IS NOT NULL``, and a row is emitted iff
  *either* side carries a number. That is what makes the table exhaustive:
  without the baseline-side filter, a feature valueless on both sides arrives
  as ``(NULL, NULL)`` and comes back ``new`` with a null delta — a fifth case
  reported as though the scenario had added something. It also means a
  ``'missing'``-flagged scenario row does not shadow a numeric baseline, the
  same COALESCE rule ``storage._utils.scenario_overlay`` applies one layer
  down.
- ``new`` carries no delta rather than carrying the scenario's own number. A
  value with nothing to compare against is not a change of that size, and the
  delta ramp is diverging and centred on zero, so "from nothing to 120
  units/ha" would paint as the largest improvement on the map. ``ST_AsMVT``
  omits a NULL property, so absence is how that reaches the client.

⚠ **The delta branch emits rows, so the baseline/scenario choice is an
anti-join** — here a ``FULL OUTER JOIN`` between two sides that are each at
most one row per feature (r016's two partial unique indexes:
``ir_one_current_base`` over ``(feature_id, indicator_id)`` where
``scenario_id IS NULL``, and ``ir_one_current_scenario`` over
``(feature_id, scenario_id, indicator_id)`` where it is not, both
``WHERE status='current'``).
The widened form ``scenario_id = X OR scenario_id IS NULL`` is the one thing
that must not appear in a row-emitting tile query: where a scenario has
recomputed a feature, both its row and the baseline's match, and ``ST_AsMVT``
emits both — two features with the same ``feature_id``, one painted with the
scenario's value and one with the baseline's, in whichever order the planner
returned them. That is also why r043's value branch is reproduced below
unchanged rather than rewritten around the new parameter.

**Delta without a scenario is an empty tile.** The function already answers a
caller who forgot ``indicator_slug`` with an empty tile rather than the whole
table; a caller who asks for a difference and names nothing to difference
against gets the same answer. Serving the baseline's own values under the
``delta`` property would paint every block ``unchanged`` — "my scenario did
nothing", produced by a missing parameter.

**``delta`` is read as a boolean the way Postgres reads one** — the full
vocabulary, which is wider than it looks: ``1``, ``t``, ``tr``, ``tru``,
``true``, ``y``, ``ye``, ``yes`` and ``on``, in any letter case, with
surrounding whitespace ignored. The prefixes and the trim are not pedantry.
``?delta=true `` with a trailing space, or a hand-shortened ``?delta=tru``,
would otherwise be *false* — and the failure is silent in the worst direction:
a client asking for a difference is served values, under a property name it is
not reading, on a ramp that means something else. (``o`` is excluded because
Postgres excludes it: it is ambiguous between ``on`` and ``off``.)

Spelled out here rather than delegated to ``pg_input_is_valid(…, 'boolean')``,
which would make the claim true by construction but needs PostgreSQL 16, and
this repository's own notes record three Postgres stacks in play. The list is
pinned against Postgres's parser by
``test_the_delta_flag_is_read_exactly_as_postgres_reads_a_boolean``, which asks
the server about each spelling instead of trusting this comment.

Anything else — ``false``, ``0``, a typo, an empty string — is the value tile,
so an unrecognised input falls back to the behaviour every client had before
this migration rather than to the new one.

The two casts the delta branch needs are guarded by ``CASE`` in the ``params``
CTE (``scenario_uuid``, ``aoi_uuid``) rather than by a regex sitting beside
the cast in an ``AND`` chain: ``AND`` has no guaranteed evaluation order, and
``'nonsense'::uuid`` raises rather than matching nothing. r024's form is kept
as it is in the value branch, where changing it would change the baseline path
this migration promises not to touch.

Revision ID: r049
Revises: r048
Create Date: 2026-11-12
"""

from __future__ import annotations

from alembic import op

revision = "r049"
down_revision = "r048"
branch_labels = None
depends_on = None

_UUID_RE = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Every spelling Postgres itself accepts for boolean true, unique prefixes
#: included. See the module docstring for why the prefixes are in the list.
_TRUE_WORDS = "'1', 't', 'tr', 'tru', 'true', 'y', 'ye', 'yes', 'on'"

#: The whitespace Postgres's boolean parser ignores around a value. Trimmed
#: because a trailing space in a hand-typed or hand-assembled URL is the
#: likeliest way anyone meets the boundary at all.
_SPACE = r"E' \t\n\r\f\v'"

#: Zoom-dependent simplification and tile clipping, shared by both branches so
#: the two views cannot drift into drawing different outlines for one feature.
#: Character-identical to r043's, indentation included.
_GEOM = """
          ST_AsMVTGeom(
            ST_Transform(
              CASE
                WHEN z >= 14 THEN f.geometry
                WHEN z >= 12 THEN ST_Simplify(f.geometry, 0.00002)
                WHEN z >= 10 THEN ST_Simplify(f.geometry, 0.0001)
                ELSE              ST_Simplify(f.geometry, 0.0005)
              END,
              3857
            ),
            ST_TileEnvelope(z, x, y),
            4096,
            64,
            true
          ) AS geom"""

#: The bbox test and zoom floor, likewise shared. The floor belongs to the
#: function and not to a branch: a choropleth at city scale is misleading
#: whichever property it paints.
_IN_TILE = """
          AND f.geometry && ST_Transform(
            ST_TileEnvelope(z, x, y, margin => 64.0 / 4096),
            4326
          )
          AND z >= 8"""


def _value_branch(scenario_predicate: str) -> str:
    """r043's tile: one value per feature, the scenario's over the baseline's.

    Reproduced rather than referenced because ``CREATE OR REPLACE FUNCTION``
    replaces the whole body. ``scenario_predicate`` is the anti-join on the
    way up and, as in r043, the only part of this that a downgrade changes.
    """
    return f"""
      SELECT ST_AsMVT(tile, 'indicator_overlay', 4096, 'geom')
      FROM (
        SELECT{_GEOM},
          CAST(f.id AS text)     AS feature_id,
          f.type_slug             AS type_slug,
          ir.value_numeric        AS value_numeric,
          ir.quality_flag         AS quality_flag
        FROM curated_geo.indicator_result ir
        JOIN curated_geo.feature f
          ON f.id = ir.feature_id
        JOIN params p ON TRUE
        WHERE
          p.indicator_slug IS NOT NULL
          AND ir.indicator_slug = p.indicator_slug
          AND ir.status = 'current'
          AND ({scenario_predicate})
          AND (
            p.feature_type IS NULL
            OR ir.feature_type = p.feature_type
          )
          AND (
            p.aoi_id_txt IS NULL
            OR (
              p.aoi_id_txt ~ '{_UUID_RE}'
              AND EXISTS (
                SELECT 1
                FROM curated_geo.aoi_feature af
                WHERE af.feature_id = f.id
                  AND af.aoi_id = p.aoi_id_txt::uuid
              )
            )
          ){_IN_TILE}
      ) tile
      WHERE tile.geom IS NOT NULL"""


def _side(scenario_clause: str) -> str:
    """One side of the comparison: ``(feature_id, value_numeric)``, deduplicated
    by r016 rather than by a ``DISTINCT`` here.

    ``p.scenario_uuid IS NOT NULL`` on the scenario side is not redundant with
    the branch's outer guard: ``ir.scenario_id = NULL`` evaluates to NULL and
    so matches nothing, which is the right answer by accident rather than by
    statement.
    """
    return f"""
            SELECT ir.feature_id, ir.value_numeric
            FROM curated_geo.indicator_result ir
            JOIN params p ON TRUE
            WHERE
              p.indicator_slug IS NOT NULL
              AND ir.indicator_slug = p.indicator_slug
              AND ir.status = 'current'
              AND {scenario_clause}
              AND ir.value_numeric IS NOT NULL
              AND (
                p.feature_type IS NULL
                OR ir.feature_type = p.feature_type
              )"""


_BASELINE_SIDE = _side("ir.scenario_id IS NULL")
_SCENARIO_SIDE = _side(
    "(p.scenario_uuid IS NOT NULL AND ir.scenario_id = p.scenario_uuid)"
)

_DELTA_BRANCH = f"""
      SELECT ST_AsMVT(tile, 'indicator_overlay', 4096, 'geom')
      FROM (
        SELECT{_GEOM},
          CAST(f.id AS text)     AS feature_id,
          f.type_slug             AS type_slug,
          CASE
            WHEN cmp.baseline IS NOT NULL THEN cmp.effective - cmp.baseline
          END                     AS delta,
          CASE
            WHEN cmp.baseline IS NULL         THEN 'new'
            WHEN cmp.effective = cmp.baseline THEN 'unchanged'
            ELSE                                   'changed'
          END                     AS state
        FROM (
          SELECT
            COALESCE(b.feature_id, s.feature_id)       AS feature_id,
            b.value_numeric                            AS baseline,
            COALESCE(s.value_numeric, b.value_numeric) AS effective
          FROM ({_BASELINE_SIDE}
          ) b
          FULL OUTER JOIN ({_SCENARIO_SIDE}
          ) s ON s.feature_id = b.feature_id
        ) cmp
        JOIN curated_geo.feature f
          ON f.id = cmp.feature_id
        JOIN params p ON TRUE
        WHERE
          p.scenario_uuid IS NOT NULL
          AND (
            p.aoi_id_txt IS NULL
            OR (
              p.aoi_uuid IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM curated_geo.aoi_feature af
                WHERE af.feature_id = f.id
                  AND af.aoi_id = p.aoi_uuid
              )
            )
          ){_IN_TILE}
      ) tile
      WHERE tile.geom IS NOT NULL"""

#: The three columns only the delta branch reads. ``CASE`` and not an ``AND``
#: chain — see the module docstring.
_DELTA_PARAMS = f""",
          lower(btrim(COALESCE(query_params->>'delta', ''), {_SPACE}))
            IN ({_TRUE_WORDS})                        AS delta_on,
          CASE
            WHEN query_params->>'scenario_id' ~ '{_UUID_RE}'
            THEN (query_params->>'scenario_id')::uuid
          END                                         AS scenario_uuid,
          CASE
            WHEN query_params->>'aoi_id' ~ '{_UUID_RE}'
            THEN (query_params->>'aoi_id')::uuid
          END                                         AS aoi_uuid"""


def _body(scenario_predicate: str, *, delta: bool) -> str:
    """The function, with the delta view or without it.

    ``delta=False`` renders r043's definition exactly — the value branch
    alone, with no ``delta_on`` to switch on — which is what ``downgrade``
    installs.

    ⚠ ``CASE`` and not a ``UNION ALL`` of the two branches. Unioned branches
    would have to agree on a column list, so the value tile would carry an
    all-NULL ``delta`` column: no feature would show the property, but the
    string would still be in the layer's key dictionary, and the value tile
    would no longer be the tile r043 shipped. ``CASE`` evaluates one branch,
    and each branch declares only its own properties.
    """
    selected = (
        f"""
      SELECT CASE
               WHEN pp.delta_on THEN ({_DELTA_BRANCH}
               )
               ELSE ({_value_branch(scenario_predicate)}
               )
             END
      FROM params pp"""
        if delta
        else _value_branch(scenario_predicate)
    )
    return f"""
    CREATE OR REPLACE FUNCTION serving_geo.indicator_overlay_tile(
        z           integer,
        x           integer,
        y           integer,
        query_params json DEFAULT '{{}}'
    )
    RETURNS bytea
    LANGUAGE sql
    STABLE
    PARALLEL SAFE
    AS $tile$
      WITH params AS (
        SELECT
          NULLIF(query_params->>'indicator_slug', '') AS indicator_slug,
          NULLIF(query_params->>'scenario_id', '')    AS scenario_id_txt,
          NULLIF(query_params->>'feature_type', '')   AS feature_type,
          NULLIF(query_params->>'aoi_id', '')         AS aoi_id_txt{
        _DELTA_PARAMS if delta else ""
    }
      ){selected}
    $tile$
"""


#: r043's anti-join, unchanged: a baseline row qualifies only when the
#: scenario has no *valued* row for the same (feature, indicator).
_OVERLAY = f"""
            (p.scenario_id_txt IS NULL AND ir.scenario_id IS NULL)
            OR (p.scenario_id_txt IS NOT NULL
                AND p.scenario_id_txt ~ '{_UUID_RE}'
                AND (
                  ir.scenario_id = p.scenario_id_txt::uuid
                  OR (ir.scenario_id IS NULL AND NOT EXISTS (
                        SELECT 1
                        FROM curated_geo.indicator_result _ov
                        WHERE _ov.feature_id = ir.feature_id
                          AND _ov.indicator_id = ir.indicator_id
                          AND _ov.status = 'current'
                          AND _ov.scenario_id = p.scenario_id_txt::uuid
                          AND (_ov.value_numeric IS NOT NULL
                            OR _ov.value_text IS NOT NULL
                            OR _ov.value_json IS NOT NULL)
                      ))
                ))
"""


def upgrade() -> None:
    op.execute(_body(_OVERLAY, delta=True))


def downgrade() -> None:
    op.execute(_body(_OVERLAY, delta=False))
