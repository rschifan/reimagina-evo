"""Asset graph: curated_geo.asset, asset_version, asset_dependency,
asset_materialization, and the freshness/lineage views built on top of them.

Why this exists
---------------
Staleness is declared four times in this schema and implemented zero times:

  * ``indicator_result.status='stale'`` (r016) is only ever written by write-path
    supersession (``repositories.py``), so it means "a newer write replaced this
    row", never "an input changed".
  * ``aoi_indicator_summary.status='stale'`` (r015) — no writer anywhere.
  * ``tile_snapshot_registry.status='superseded'`` (r013) — no writer anywhere.
  * ``IndicatorSpec.reads=("poi_version","network_version")`` — attribute-shaped
    tokens with no producer; ``pipeline/osm_network.py`` asks a human to bump them.

Each is a *push* of a fact that should be *computed*. This migration replaces all
four with one rule:

    version(source | external, partition) = recorded by whatever changed it
    version(derived,           partition) = H(closure of upstream versions,
                                              code versions and params digests)

and "stale" becomes ``materialized_version <> expected_version`` — derived on
read, so there is no marker to miss and no invalidation pass to run.

Closure hashing, not nested hashing
-----------------------------------
``asset_expected_version_v`` hashes the *whole upstream closure* rather than
nesting a hash per level. The two are equivalent for staleness — both change
exactly when some ancestor's recorded version, code version, params digest or
edge shape changes — but the closure form is a plain reachability query plus one
aggregate, which the Python twin in ``reimagina.assets.version`` can reproduce
line for line. Nested hashing would make the two implementations diverge on
evaluation order, and a silent split between them is the one failure this design
cannot tolerate.

Edges are hashed alongside nodes so that adding, removing or re-shaping a
dependency moves the version even when the closure's *membership* is unchanged
(e.g. narrowing an attribute projection or changing a radius).

``md5`` is a builtin, so this needs no pgcrypto. It is used here as an identity
function, not a security primitive.

The graph is small and the data is huge — dozens of assets against millions of
features — so these views are computed live on every read. They are O(graph),
never O(rows).

Revision ID: r041
Revises: r040
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "r041"
down_revision = "r040"
branch_labels = None
depends_on = None


_NEW_CATALOG_ROWS = [
    # (schema, object, logical_name, layer_kind, description,
    #  primary_geometry_column, intended_use, display_order)
    (
        "curated_geo",
        "asset",
        "assets",
        "canonical",
        "Registry of every named, versioned artifact the platform produces or observes",
        None,
        "See what participates in the dependency graph and how each is versioned",
        60,
    ),
    (
        "curated_geo",
        "asset_dependency",
        "asset_edges",
        "canonical",
        "The one dependency edge table: dataset, attribute and composite edges alike",
        None,
        "Trace what an asset consumes and what consumes it",
        61,
    ),
    (
        "serving_geo",
        "asset_freshness_v",
        "asset_freshness_serving",
        "serving",
        "Per asset+partition: expected vs materialized version, and the resulting state",
        None,
        "Answer 'what is out of date' without running a job",
        120,
    ),
    (
        "explore",
        "asset_freshness_v",
        "asset_freshness_catalog",
        "explore",
        "Engineer view: freshness joined with asset keys, kinds and stale reasons",
        None,
        "Audit staleness and find which upstream moved",
        950,
    ),
]


def upgrade() -> None:
    # ── asset ──────────────────────────────────────────────────────────────
    #
    # Three kinds, because the platform has three genuinely different producers:
    #   source   — a pipeline this graph runs (overture, cdmx, inegi, points)
    #   external — produced outside it entirely (Overpass/osmnx writes
    #              curated_geo.road_edge with no ingestion_run at all; the Azure
    #              mirrors are hand-copied; scripts merge into feature.attributes).
    #              The graph can observe and version these, never run them.
    #   derived  — this graph produces it.
    #
    # `code_version` is deliberately NOT provenance.code_version(). That is a git
    # SHA, so putting it in a fingerprint would make a README commit invalidate
    # every derived asset in the platform. This column is a per-asset version
    # bumped when that asset's own logic changes, exactly as Dagster's per-asset
    # code_version works. The git SHA stays on job_run, where it already lives.
    op.execute("""
        CREATE TABLE curated_geo.asset (
            id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            asset_key            text        NOT NULL UNIQUE,
            kind                 text        NOT NULL
                                             CHECK (kind IN ('source','external','derived')),
            producer             text,
            backing_kind         text        NOT NULL
                                             CHECK (backing_kind IN (
                                               'feature_type','observation_prefix','relation',
                                               'raster_layer','file','config','indicator',
                                               'rollup','aoi_summary','tile_snapshot',
                                               'scenario_override')),
            backing_ref          text,
            feature_type_id      uuid        REFERENCES curated_geo.feature_type_catalog(id)
                                             ON DELETE RESTRICT,
            partition_dimensions text[]      NOT NULL DEFAULT '{}',
            code_version         text,
            params_digest        text,
            version_strategy     text        CHECK (version_strategy IN
                                               ('commit','digest','observed')),
            probe_sql            text,
            description          text,
            created_at           timestamptz NOT NULL DEFAULT now(),

            -- A derived asset is produced here and its version is computed, so it
            -- carries a producer + code_version and never a version_strategy.
            -- A source/external asset is the mirror image.
            CONSTRAINT asset_kind_shape CHECK (
                (kind = 'derived'
                    AND producer IS NOT NULL
                    AND code_version IS NOT NULL
                    AND version_strategy IS NULL)
                OR
                (kind IN ('source','external')
                    AND producer IS NULL
                    AND code_version IS NULL
                    AND version_strategy IS NOT NULL)
            ),
            -- An observed asset is one whose writes can bypass every pipeline.
            -- Without the probe it would silently report itself fresh forever,
            -- which is the most dangerous state a staleness system can reach.
            CONSTRAINT asset_probe_shape CHECK (
                (version_strategy = 'observed') = (probe_sql IS NOT NULL)
            )
        )
    """)
    op.execute("CREATE INDEX asset_kind_idx ON curated_geo.asset (kind)")

    # ── asset_version ──────────────────────────────────────────────────────
    #
    # Recorded versions for source/external assets only; derived versions are
    # computed and never stored here. Append-only with a moving is_current
    # pointer — the same SCD-2 shape curated_geo.observation already uses
    # (obs_one_current, r003), which is this schema's only existing precedent.
    #
    # No feature ids are stored. curated_geo.feature.ingestion_run_id already IS
    # the per-run change log and feature_ingestion_run_idx (r007) already indexes
    # it, so a 2M-building Overture load costs one row here rather than a 32 MB
    # uuid[].
    op.execute("""
        CREATE TABLE curated_geo.asset_version (
            id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            asset_id         uuid        NOT NULL REFERENCES curated_geo.asset(id)
                                         ON DELETE CASCADE,
            partition_key    text        NOT NULL DEFAULT '',
            version          text        NOT NULL CHECK (version <> ''),
            sequence         bigint      NOT NULL,
            ingestion_run_id uuid        REFERENCES curated_geo.ingestion_run(id)
                                         ON DELETE SET NULL,
            dataset_version  text,
            row_count        integer     CHECK (row_count IS NULL OR row_count >= 0),
            observed_at      timestamptz NOT NULL DEFAULT now(),
            is_current       boolean     NOT NULL DEFAULT true,
            valid_from       timestamptz NOT NULL DEFAULT now(),
            valid_to         timestamptz,
            CHECK (valid_to IS NULL OR valid_to >= valid_from),
            CHECK (is_current = (valid_to IS NULL)),
            UNIQUE (asset_id, partition_key, sequence)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX asset_version_one_current
            ON curated_geo.asset_version (asset_id, partition_key)
            WHERE is_current
    """)

    # ── asset_dependency ───────────────────────────────────────────────────
    #
    # One edge table, not two mechanisms. An attribute dependency is an edge with
    # a non-empty `attributes` projection; a dataset dependency is an edge with an
    # empty one; a composite component is relation='derived_from' with a weight.
    # This is what lets IndicatorSpec.reads and .depends_on become projections of
    # a single declaration rather than two parallel systems.
    #
    # Every `relation` value abstracts a query that already exists in the repo:
    #   self            — geodesic_metrics_by_type / effective_numeric
    #   child_in_parent — centroid_in_parent_counts
    #   overlap         — curated_geo.feature_overlap (r009), used by jobs/rollup
    #   within_radius   — accessibility's ST_DWithin on the block centroid
    #   derived_from    — list_current_for_indicator (the composite edge)
    #
    # partition_mapping says how the consumer's partition resolves on the producer:
    #   identity — same partition (a scenario's override set)
    #   baseline — always the base partition (a shared source layer read under a
    #              scenario; this is what keeps a scenario edit from staling the
    #              baseline)
    #   fan_in   — every partition the producer has (a rollup reading all children)
    op.execute("""
        CREATE TABLE curated_geo.asset_dependency (
            consumer_asset_id uuid   NOT NULL REFERENCES curated_geo.asset(id)
                                     ON DELETE CASCADE,
            producer_asset_id uuid   NOT NULL REFERENCES curated_geo.asset(id)
                                     ON DELETE RESTRICT,
            relation          text   NOT NULL
                                     CHECK (relation IN ('self','child_in_parent',
                                       'overlap','within_radius','derived_from')),
            attributes        text[] NOT NULL DEFAULT '{}',
            radius_m          double precision,
            weight            double precision,
            partition_mapping text   NOT NULL DEFAULT 'identity'
                                     CHECK (partition_mapping IN
                                       ('identity','baseline','fan_in')),
            created_at        timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (consumer_asset_id, producer_asset_id, relation),
            CHECK (consumer_asset_id <> producer_asset_id),
            CONSTRAINT asset_dependency_radius CHECK (
                (relation = 'within_radius' AND radius_m IS NOT NULL AND radius_m > 0)
                OR
                (relation <> 'within_radius' AND radius_m IS NULL)
            ),
            CONSTRAINT asset_dependency_weight CHECK (
                weight IS NULL
                OR (relation = 'derived_from' AND weight >= 0 AND weight <= 1)
            )
        )
    """)
    op.execute("""
        CREATE INDEX asset_dependency_producer_idx
            ON curated_geo.asset_dependency (producer_asset_id)
    """)

    # Deferred validation. Carried over from indicator_dependency_validate (r016)
    # because the invariants are the same ones and the seeding shape is the same:
    # child rows are written one at a time inside a single transaction, so an
    # immediate trigger would reject a 0.5/0.5 composite on its first row.
    #
    # Beyond r016's rules this also closes a footgun that exists today:
    # aggregation_recipe lets an operator choose ANY attribute_key_prefix with no
    # reserved-name check, and four indicators read attributes through
    # effective_numeric — so a recipe writing 'floors' creates
    # indicator <- observation <- recipe <- observation and nothing rejects it.
    # Once recipes are registered here, the acyclicity check makes that
    # unrepresentable.
    op.execute("""
        CREATE OR REPLACE FUNCTION curated_geo.asset_dependency_validate()
        RETURNS TRIGGER AS $$
        DECLARE
            weighted_count int;
            null_weight_count int;
            weight_sum double precision;
            cycle_exists boolean;
            bad_consumer text;
        BEGIN
            -- Only a derived asset can consume: a source or external asset is by
            -- definition produced outside this graph.
            SELECT a.asset_key INTO bad_consumer
            FROM curated_geo.asset a
            JOIN (
                SELECT DISTINCT consumer_asset_id
                FROM curated_geo.asset_dependency
            ) dep ON dep.consumer_asset_id = a.id
            WHERE a.kind <> 'derived'
            LIMIT 1;
            IF bad_consumer IS NOT NULL THEN
                RAISE EXCEPTION
                    'asset_dependency.consumer_asset_id must reference a derived asset (got %)',
                    bad_consumer;
            END IF;

            WITH RECURSIVE dep_graph(consumer_asset_id, producer_asset_id, depth) AS (
                SELECT consumer_asset_id, producer_asset_id, 1
                FROM curated_geo.asset_dependency
                UNION ALL
                SELECT d.consumer_asset_id, dg.producer_asset_id, dg.depth + 1
                FROM curated_geo.asset_dependency d
                JOIN dep_graph dg ON dg.consumer_asset_id = d.producer_asset_id
                WHERE dg.depth < 50
            )
            SELECT EXISTS(
                SELECT 1 FROM dep_graph WHERE consumer_asset_id = producer_asset_id
            ) INTO cycle_exists;
            IF cycle_exists THEN
                RAISE EXCEPTION 'asset_dependency contains a cycle';
            END IF;

            FOR weighted_count, null_weight_count, weight_sum IN
                SELECT
                    COUNT(*) FILTER (WHERE weight IS NOT NULL),
                    COUNT(*) FILTER (WHERE weight IS NULL),
                    COALESCE(SUM(weight), 0)
                FROM curated_geo.asset_dependency
                WHERE relation = 'derived_from'
                GROUP BY consumer_asset_id
            LOOP
                IF weighted_count > 0 AND null_weight_count > 0 THEN
                    RAISE EXCEPTION
                        'asset_dependency: mix of weighted and null-weight derived_from rows for one consumer';
                END IF;
                IF weighted_count > 0 AND abs(weight_sum - 1.0) > 1e-9 THEN
                    RAISE EXCEPTION
                        'asset_dependency: derived_from weights must sum to 1.0 (got %)', weight_sum;
                END IF;
            END LOOP;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER asset_dependency_validate_trigger
        AFTER INSERT OR UPDATE OR DELETE ON curated_geo.asset_dependency
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION curated_geo.asset_dependency_validate()
    """)

    # ── asset_materialization ──────────────────────────────────────────────
    #
    # Append-only record of what was actually produced, carrying the exact input
    # version map that was hashed. This is what closes F8 ("record code version,
    # input versions, and seed") by construction rather than as an add-on, and it
    # is what makes staleness *derived* — a comparison against a recorded fact —
    # instead of a marker somebody has to remember to write.
    #
    # coverage_scope exists because a partition is NOT atomically materialized
    # here: bulk_write_numeric_results stales only the feature ids it wrote, and
    # indicators/edificability.py deliberately relies on that to skip features
    # missing an attribute. So one (indicator, scenario) partition legitimately
    # holds rows from several runs, and freshness has to be able to say "partial"
    # rather than lying with "fresh".
    op.execute("""
        CREATE TABLE curated_geo.asset_materialization (
            id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            asset_id       uuid        NOT NULL REFERENCES curated_geo.asset(id)
                                       ON DELETE CASCADE,
            partition_key  text        NOT NULL DEFAULT '',
            version        text        NOT NULL CHECK (version <> ''),
            input_versions jsonb       NOT NULL DEFAULT '{}'::jsonb,
            coverage_scope jsonb       NOT NULL DEFAULT '{"complete": true}'::jsonb,
            job_run_id     uuid        REFERENCES jobs_runtime.job_run(id) ON DELETE SET NULL,
            row_count      integer     CHECK (row_count IS NULL OR row_count >= 0),
            status         text        NOT NULL DEFAULT 'succeeded'
                                       CHECK (status IN ('succeeded','failed')),
            started_at     timestamptz NOT NULL DEFAULT now(),
            completed_at   timestamptz NOT NULL DEFAULT now(),
            CHECK (completed_at >= started_at),
            CHECK (jsonb_typeof(coverage_scope -> 'complete') = 'boolean')
        )
    """)
    op.execute("""
        CREATE INDEX asset_materialization_latest_idx
            ON curated_geo.asset_materialization (asset_id, partition_key, completed_at DESC)
    """)

    # ── serving_geo.asset_component_v ──────────────────────────────────────
    #
    # The algebra, one row per term. See the module docstring for why this hashes
    # the closure rather than nesting per level.
    op.execute("""
        CREATE OR REPLACE VIEW serving_geo.asset_component_v AS
        WITH RECURSIVE
        node AS (
            SELECT av.asset_id, av.partition_key
            FROM curated_geo.asset_version av
            WHERE av.is_current
            UNION
            SELECT m.asset_id, m.partition_key
            FROM curated_geo.asset_materialization m
            UNION
            -- Every asset is askable about its baseline partition even before
            -- anything has been recorded for it, so a never-materialized derived
            -- asset still has an expected version to compare against.
            SELECT a.id, ''
            FROM curated_geo.asset a
        ),
        upstream(root_asset, root_partition, asset_id, partition_key, depth) AS (
            SELECT n.asset_id, n.partition_key, n.asset_id, n.partition_key, 0
            FROM node n
            UNION
            SELECT u.root_asset, u.root_partition, d.producer_asset_id,
                   pp.partition_key, u.depth + 1
            FROM upstream u
            JOIN curated_geo.asset_dependency d
              ON d.consumer_asset_id = u.asset_id
            CROSS JOIN LATERAL (
                SELECT CASE d.partition_mapping
                            WHEN 'baseline' THEN ''
                            ELSE u.partition_key
                       END AS partition_key
                WHERE d.partition_mapping <> 'fan_in'
                UNION ALL
                SELECT n2.partition_key
                FROM node n2
                WHERE d.partition_mapping = 'fan_in'
                  AND n2.asset_id = d.producer_asset_id
            ) pp
            WHERE u.depth < 50
        ),
        component AS (
            -- Node contributions: a recorded version for source/external, the
            -- declared code version + params digest for derived. A source with no
            -- recorded version contributes 'missing', which is stable and leaves
            -- everything downstream deterministically stale until it is observed.
            SELECT u.root_asset, u.root_partition,
                   a.asset_key || '@' || u.partition_key || '=' ||
                   CASE
                       WHEN a.kind = 'derived'
                           THEN 'code:' || a.code_version ||
                                ':params:' || COALESCE(a.params_digest, '')
                       ELSE COALESCE(av.version, 'missing')
                   END AS part
            FROM upstream u
            JOIN curated_geo.asset a ON a.id = u.asset_id
            LEFT JOIN curated_geo.asset_version av
                   ON av.asset_id = u.asset_id
                  AND av.partition_key = u.partition_key
                  AND av.is_current
            UNION ALL
            -- Edge contributions, so that adding, removing or re-shaping a
            -- dependency moves the version even when closure membership does not
            -- (narrowing an attribute projection, changing a radius, reweighting).
            --
            -- ⚠ round(x::numeric, 6)::text, never x::text. Postgres renders
            -- 1500.0::double precision::text as '1500' while Python's str() gives
            -- '1500.0' — and 1500.0 is ACCESSIBILITY_CUTOFF_M, the only radius the
            -- catalog actually ships. Using the float's own text rendering would
            -- have silently split this view from its Python twin in
            -- reimagina.assets.version on the one row that matters. Both sides
            -- now format at a fixed scale of 6.
            SELECT u.root_asset, u.root_partition,
                   'edge:' || c.asset_key || '->' || p.asset_key ||
                   ':' || d.relation ||
                   ':' || array_to_string(d.attributes, ',') ||
                   ':' || COALESCE(round(d.radius_m::numeric, 6)::text, '') ||
                   ':' || COALESCE(round(d.weight::numeric, 6)::text, '') ||
                   ':' || d.partition_mapping AS part
            FROM upstream u
            JOIN curated_geo.asset_dependency d ON d.consumer_asset_id = u.asset_id
            JOIN curated_geo.asset c ON c.id = d.consumer_asset_id
            JOIN curated_geo.asset p ON p.id = d.producer_asset_id
        )
        SELECT
            a.id                                            AS asset_id,
            a.asset_key,
            component.root_partition                        AS partition_key,
            component.part
        FROM component
        JOIN curated_geo.asset a ON a.id = component.root_asset
    """)

    # The version is a thin aggregate over the components. Split in two so that a
    # disagreement with the Python twin can be *diagnosed* — comparing hashes only
    # says "these differ", comparing component sets says which term drifted.
    op.execute("""
        CREATE OR REPLACE VIEW serving_geo.asset_expected_version_v AS
        SELECT
            asset_id,
            asset_key,
            partition_key,
            -- DISTINCT is load-bearing, not tidiness. In a diamond
            -- (composite -> compactness -> block, composite -> density -> block)
            -- the same node is reachable by more than one path; where those paths
            -- differ in length the recursion emits it once per depth, and without
            -- DISTINCT the hash would depend on the *shape* of the graph rather
            -- than on its content. It would still be deterministic, so nothing
            -- would look wrong — it would just make the Python twin reproduce
            -- path multiplicity to agree. A set of components has neither problem.
            md5(string_agg(DISTINCT part, '|' ORDER BY part)) AS expected_version
        FROM serving_geo.asset_component_v
        GROUP BY asset_id, asset_key, partition_key
    """)

    # ── serving_geo.asset_freshness_v ──────────────────────────────────────
    #
    # 'absent' is not a failure — a partition nobody has ever computed is simply
    # not there. 'partial' is the honest answer for a partition materialized over
    # a subset of its features; reporting it as 'fresh' is the specific lie this
    # design refuses to tell.
    op.execute("""
        CREATE OR REPLACE VIEW serving_geo.asset_freshness_v AS
        WITH materialized AS (
            SELECT DISTINCT ON (m.asset_id, m.partition_key)
                   m.asset_id, m.partition_key, m.version, m.coverage_scope,
                   m.completed_at, m.job_run_id
            FROM curated_geo.asset_materialization m
            WHERE m.status = 'succeeded'
            ORDER BY m.asset_id, m.partition_key, m.completed_at DESC
        ),
        recorded AS (
            -- A source or external asset is never materialized *by this graph*:
            -- its recorded version is its state. Without this arm every ingested
            -- layer would report 'absent' forever and `assets stale` would be
            -- unreadable. The two arms cannot overlap — a derived asset has no
            -- asset_version rows and a source has no materializations — so
            -- UNION ALL is safe.
            SELECT av.asset_id, av.partition_key, av.version,
                   '{"complete": true}'::jsonb AS coverage_scope,
                   av.valid_from                AS completed_at,
                   NULL::uuid                   AS job_run_id
            FROM curated_geo.asset_version av
            WHERE av.is_current
        ),
        latest AS (
            SELECT * FROM materialized
            UNION ALL
            SELECT * FROM recorded
        )
        SELECT
            e.asset_id,
            e.asset_key,
            a.kind,
            e.partition_key,
            -- Staleness is a property of *derived* things. A source or external
            -- asset has nothing to be stale against: whatever was recorded IS its
            -- version, so that is what it reports as expected. Comparing a
            -- recorded version against the closure hash would mark every ingested
            -- layer permanently stale.
            CASE WHEN a.kind = 'derived' THEN e.expected_version ELSE l.version END
                                                            AS expected_version,
            l.version                                       AS materialized_version,
            l.completed_at                                  AS materialized_at,
            l.job_run_id,
            COALESCE((l.coverage_scope ->> 'complete')::boolean, true) AS coverage_complete,
            CASE
                WHEN l.version IS NULL                       THEN 'absent'
                WHEN a.kind <> 'derived'                     THEN 'fresh'
                WHEN l.version <> e.expected_version         THEN 'stale'
                WHEN NOT COALESCE((l.coverage_scope ->> 'complete')::boolean, true)
                                                             THEN 'partial'
                ELSE 'fresh'
            END                                             AS state
        FROM serving_geo.asset_expected_version_v e
        JOIN curated_geo.asset a ON a.id = e.asset_id
        LEFT JOIN latest l
               ON l.asset_id = e.asset_id
              AND l.partition_key = e.partition_key
    """)

    # ── serving_geo.asset_lineage_v ────────────────────────────────────────
    #
    # Both directions from one recursion: 'upstream' answers "what does this
    # composite need", 'downstream' answers "I changed this layer, what must be
    # recomputed". The two user-facing questions are the same query read in
    # opposite directions, so there is no second implementation to drift.
    op.execute("""
        CREATE OR REPLACE VIEW serving_geo.asset_lineage_v AS
        WITH RECURSIVE up(root_id, asset_id, depth) AS (
            SELECT a.id, a.id, 0 FROM curated_geo.asset a
            UNION
            SELECT u.root_id, d.producer_asset_id, u.depth + 1
            FROM up u
            JOIN curated_geo.asset_dependency d ON d.consumer_asset_id = u.asset_id
            WHERE u.depth < 50
        )
        SELECT 'upstream'::text AS direction,
               r.asset_key      AS root_key,
               t.asset_key      AS related_key,
               t.kind           AS related_kind,
               min(up.depth)    AS depth
        FROM up
        JOIN curated_geo.asset r ON r.id = up.root_id
        JOIN curated_geo.asset t ON t.id = up.asset_id
        WHERE up.depth > 0
        GROUP BY r.asset_key, t.asset_key, t.kind
        UNION ALL
        SELECT 'downstream'::text,
               t.asset_key,
               r.asset_key,
               r.kind,
               min(up.depth)
        FROM up
        JOIN curated_geo.asset r ON r.id = up.root_id
        JOIN curated_geo.asset t ON t.id = up.asset_id
        WHERE up.depth > 0
        GROUP BY t.asset_key, r.asset_key, r.kind
    """)

    # ── explore views ──────────────────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE VIEW explore.asset_v AS
        SELECT
            a.asset_key,
            a.kind,
            a.backing_kind,
            a.backing_ref,
            a.version_strategy,
            a.code_version,
            a.partition_dimensions,
            ft.context      AS feature_type_context,
            ft.type_slug    AS feature_type_slug,
            a.description,
            a.created_at
        FROM curated_geo.asset a
        LEFT JOIN curated_geo.feature_type_catalog ft ON ft.id = a.feature_type_id
    """)
    op.execute("""
        CREATE OR REPLACE VIEW explore.asset_freshness_v AS
        SELECT
            f.asset_key,
            f.kind,
            f.partition_key,
            f.state,
            f.expected_version,
            f.materialized_version,
            f.coverage_complete,
            f.materialized_at
        FROM serving_geo.asset_freshness_v f
    """)

    for row in _NEW_CATALOG_ROWS:
        op.execute(
            "INSERT INTO explore.layer_catalog "
            "(schema_name, object_name, logical_name, layer_kind, description, "
            "primary_geometry_column, intended_use, display_order) VALUES "
            f"({_sql_null_or(row[0])}, {_sql_null_or(row[1])}, {_sql_null_or(row[2])}, "
            f"{_sql_null_or(row[3])}, {_sql_null_or(row[4])}, {_sql_null_or(row[5])}, "
            f"{_sql_null_or(row[6])}, {row[7]})"
        )


def _sql_null_or(value: str | None) -> str:
    """Quote-and-escape, or NULL.

    Every text column goes through this, not just the nullable one: the prose in
    these rows contains apostrophes ("what is out of date"), and interpolating it
    raw produced a syntax error the first time this migration ran.
    """
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def downgrade() -> None:
    for row in _NEW_CATALOG_ROWS:
        op.execute(
            f"DELETE FROM explore.layer_catalog "
            f"WHERE schema_name = '{row[0]}' AND object_name = '{row[1]}'"
        )
    op.execute("DROP VIEW IF EXISTS explore.asset_freshness_v")
    op.execute("DROP VIEW IF EXISTS explore.asset_v")
    op.execute("DROP VIEW IF EXISTS serving_geo.asset_lineage_v")
    op.execute("DROP VIEW IF EXISTS serving_geo.asset_freshness_v")
    op.execute("DROP VIEW IF EXISTS serving_geo.asset_expected_version_v")
    op.execute("DROP VIEW IF EXISTS serving_geo.asset_component_v")
    op.execute("DROP TABLE IF EXISTS curated_geo.asset_materialization")
    op.execute(
        "DROP TRIGGER IF EXISTS asset_dependency_validate_trigger "
        "ON curated_geo.asset_dependency"
    )
    op.execute("DROP TABLE IF EXISTS curated_geo.asset_dependency")
    op.execute("DROP FUNCTION IF EXISTS curated_geo.asset_dependency_validate()")
    op.execute("DROP TABLE IF EXISTS curated_geo.asset_version")
    op.execute("DROP TABLE IF EXISTS curated_geo.asset")
