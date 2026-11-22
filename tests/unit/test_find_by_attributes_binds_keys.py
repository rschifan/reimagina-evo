"""``find_by_attributes`` must bind attribute keys, not splice them into SQL.

A key containing ``'`` broke the statement, and a crafted one could rewrite
the WHERE clause. Keys travel as parameters, like the values.
"""

from __future__ import annotations

from reimagina.storage.postgres.repositories import PostgresFeatureRepository


class _Cursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.log.append((sql, params))

    def fetchone(self):
        return None


class _Conn:
    def __init__(self):
        self.log: list = []

    def cursor(self):
        return _Cursor(self.log)


def test_a_key_is_a_parameter_not_sql():
    conn = _Conn()
    key = "x' = '' OR '1'='1' --"

    PostgresFeatureRepository(conn).find_by_attributes(
        type_slug="municipality", attributes={key: "09"}
    )

    sql, params = conn.log[0]
    assert key not in sql
    assert params == [key, "09", "municipality"]
