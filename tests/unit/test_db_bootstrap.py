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

    # bootstrap_local_platform now seeds the indicator catalog after migrating,
    # so a fresh DB isn't empty. Stub the DB round-trip to keep this test offline.
    seeded: dict[str, bool] = {}

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            pass

    monkeypatch.setattr("reimagina.db.connect", lambda dsn: _FakeConn())
    monkeypatch.setattr(
        "reimagina.indicators.catalog.seed_catalog",
        lambda conn: seeded.setdefault("called", True),
    )

    result = bootstrap_local_platform(
        app_dsn="postgresql://localhost/reimagina",
        test_dsn="postgresql://localhost/reimagina_test",
        admin_dsn="postgresql://localhost/postgres",
    )

    assert result["app"]["created"] is False
    assert result["test"]["created"] is True
    assert seeded.get("called") is True  # catalog seeded on bootstrap
