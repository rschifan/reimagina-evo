from __future__ import annotations

from reimagina.storage.manager import backend_name_for_dsn, duckdb_path_from_dsn


def test_backend_name_for_dsn_supports_duckdb_and_postgres():
    assert backend_name_for_dsn("duckdb:///data/local/reimagina.duckdb") == "duckdb"
    assert backend_name_for_dsn("postgresql://localhost/reimagina") == "postgres"


def test_duckdb_path_parser_supports_relative_and_absolute_paths():
    assert (
        duckdb_path_from_dsn("duckdb:///data/local/reimagina.duckdb")
        == "data/local/reimagina.duckdb"
    )
    assert (
        duckdb_path_from_dsn("duckdb:////tmp/reimagina.duckdb")
        == "/tmp/reimagina.duckdb"
    )
    assert duckdb_path_from_dsn("duckdb:///:memory:") == ":memory:"
