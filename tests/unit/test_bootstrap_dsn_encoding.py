"""Bootstrap must create and migrate the database the connection URL names."""

import pytest

from reimagina.storage.postgres.bootstrap import database_name_from_dsn
from reimagina.storage.postgres.migrations import postgres_alembic_config


@pytest.mark.parametrize(
    "dsn, name",
    [
        ("postgresql://localhost/scratch%20test", "scratch test"),
        ("postgresql://localhost/scratch%22test", 'scratch"test'),
    ],
)
def test_database_name_uses_libpq_url_decoding(dsn, name):
    assert database_name_from_dsn(dsn) == name


def test_encoded_credentials_reach_alembic_without_config_interpolation():
    dsn = "postgresql://operator:p%40ss%25word@localhost/scratch_test"
    cfg = postgres_alembic_config(dsn)
    assert cfg.get_main_option("sqlalchemy.url") == dsn
