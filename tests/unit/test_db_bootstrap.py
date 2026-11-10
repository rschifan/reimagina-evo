from __future__ import annotations

from reimagina.db.bootstrap import REQUIRED_RELATIONS, bootstrap_local_platform


def test_required_relations_cover_first_spatial_layer():
    assert "curated_geo.data_source_catalog" in REQUIRED_RELATIONS
    assert "curated_geo.ingestion_run" in REQUIRED_RELATIONS
    assert "curated_geo.feature_type_catalog" in REQUIRED_RELATIONS
    assert "curated_geo.feature" in REQUIRED_RELATIONS
    assert "serving_geo.feature_v" in REQUIRED_RELATIONS


def test_bootstrap_local_platform_runs_create_then_migrate(monkeypatch):
    monkeypatch.setattr(
        "reimagina.db.bootstrap.bootstrap_postgres",
        lambda **kwargs: {
            "app": {"created": False, "dsn": kwargs["app_dsn"], "migrated": True},
            "test": {"created": True, "dsn": kwargs["test_dsn"], "migrated": True},
        },
    )

    # bootstrap_local_platform seeds feature types, the indicator catalog and
    # the asset registry after migrating, so a fresh DB isn't empty. Stub the
    # DB round-trip to keep this test offline.
    #
    # ⚠ Recorded as an ordered list, not a set of booleans. The order is the
    # contract: a feature-backed asset resolves its `feature_type_id`, so
    # `seed_assets` raises "references unknown feature type … run `feat
    # seed-types` first" when the catalog is empty. That failure was real —
    # `bootstrap-db` died on a database whose r003 seed had never landed, and
    # told the user to run the step that comes *after* it.
    calls: list[str] = []

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            pass

    monkeypatch.setattr("reimagina.db.connect", lambda dsn: _FakeConn())
    monkeypatch.setattr(
        "reimagina.data.feature.seed_feature_types",
        lambda conn: calls.append("feature_types"),
    )
    monkeypatch.setattr(
        "reimagina.indicators.catalog.seed_catalog",
        lambda conn: calls.append("catalog"),
    )
    monkeypatch.setattr(
        "reimagina.assets.registry.seed_assets",
        lambda conn: calls.append("assets"),
    )

    result = bootstrap_local_platform(
        app_dsn="postgresql://localhost/reimagina",
        test_dsn="postgresql://localhost/reimagina_test",
        admin_dsn="postgresql://localhost/postgres",
    )

    assert result["app"]["created"] is False
    assert result["test"]["created"] is True

    # Feature types must exist before assets resolve against them. The
    # indicator catalog is independent of both, so only its presence matters.
    assert "catalog" in calls
    # Asserted before the index comparison: `.index()` on a missing entry
    # raises ValueError, which is how a reverted fix reported itself — an
    # opaque traceback in place of the reason.
    assert "feature_types" in calls, (
        f"bootstrap_local_platform never seeded feature types (calls: {calls}); "
        f"seed_assets resolves feature_type_id and raises without them"
    )
    assert "assets" in calls
    assert calls.index("feature_types") < calls.index("assets"), (
        f"bootstrap seeded in the order {calls}; assets resolve feature_type_id "
        f"and cannot be seeded first"
    )
    # The asset registry is seeded from code rather than from a migration body,
    # so bootstrap is the only thing that guarantees a fresh DB has it. Feature
    # types are seeded here for the same reason — r003 happens to insert them,
    # but a database that predates that seed has none and bootstrap must not
    # depend on the accident.
    assert calls.count("assets") == 1
    assert calls.count("feature_types") == 1
