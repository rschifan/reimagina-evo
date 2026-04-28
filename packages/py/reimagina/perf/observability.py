"""Operational Postgres observability helpers for performance monitoring."""

from __future__ import annotations

from reimagina.storage import backend_for_connection


def _require_postgres(conn) -> None:
    backend = backend_for_connection(conn)
    if backend.name != "postgres":
        raise ValueError("Performance observability is Postgres/PostGIS-only.")


def _query_dicts(conn, sql: str, params: tuple | list | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]


def pg_extension_status(conn) -> dict[str, object]:
    _require_postgres(conn)
    extensions = _query_dicts(
        conn,
        """
        SELECT extname, extversion
        FROM pg_extension
        WHERE extname IN ('postgis', 'pg_stat_statements')
        ORDER BY extname
        """,
    )
    settings = _query_dicts(
        conn,
        """
        SELECT name, setting
        FROM pg_settings
        WHERE name IN ('shared_preload_libraries', 'track_io_timing')
        ORDER BY name
        """,
    )
    setting_map = {row["name"]: row["setting"] for row in settings}
    installed = {row["extname"]: row["extversion"] for row in extensions}
    preload_libs = {
        lib.strip()
        for lib in setting_map.get("shared_preload_libraries", "").split(",")
        if lib.strip()
    }
    return {
        "extensions": installed,
        "settings": setting_map,
        "pg_stat_statements_available": "pg_stat_statements" in installed,
        "pg_stat_statements_preloaded": "pg_stat_statements" in preload_libs,
        "auto_explain_preloaded": "auto_explain" in preload_libs,
    }


def relation_sizes(conn, *, limit: int = 10) -> list[dict]:
    _require_postgres(conn)
    return _query_dicts(
        conn,
        """
        SELECT
          schemaname,
          relname,
          n_live_tup::bigint AS est_rows,
          pg_total_relation_size(relid) AS total_bytes,
          pg_size_pretty(pg_total_relation_size(relid)) AS total_size
        FROM pg_stat_user_tables
        WHERE schemaname IN ('curated_geo', 'serving_geo', 'explore')
        ORDER BY pg_total_relation_size(relid) DESC
        LIMIT %s
        """,
        (limit,),
    )


def maintenance_status(conn, *, limit: int = 10) -> list[dict]:
    _require_postgres(conn)
    return _query_dicts(
        conn,
        """
        SELECT
          schemaname,
          relname,
          n_live_tup::bigint AS est_rows,
          seq_scan,
          idx_scan,
          last_vacuum,
          last_autovacuum,
          last_analyze,
          last_autoanalyze
        FROM pg_stat_user_tables
        WHERE schemaname IN ('curated_geo', 'serving_geo', 'explore')
        ORDER BY n_live_tup DESC, relname
        LIMIT %s
        """,
        (limit,),
    )


def index_inventory(conn, *, limit: int = 20) -> list[dict]:
    _require_postgres(conn)
    return _query_dicts(
        conn,
        """
        SELECT
          ui.schemaname,
          ui.relname,
          ui.indexrelname AS index_name,
          ui.idx_scan,
          pg_relation_size(ui.indexrelid) AS index_bytes,
          pg_size_pretty(pg_relation_size(ui.indexrelid)) AS index_size
        FROM pg_stat_user_indexes ui
        WHERE ui.schemaname IN ('curated_geo', 'serving_geo', 'explore')
        ORDER BY pg_relation_size(ui.indexrelid) DESC
        LIMIT %s
        """,
        (limit,),
    )


def slow_queries(conn, *, limit: int = 10) -> dict[str, object]:
    _require_postgres(conn)
    status = pg_extension_status(conn)
    if not status["pg_stat_statements_available"]:
        return {"available": False, "queries": []}

    queries = _query_dicts(
        conn,
        """
        SELECT
          query,
          calls,
          total_exec_time,
          mean_exec_time,
          rows,
          shared_blks_hit,
          shared_blks_read,
          temp_blks_read,
          temp_blks_written
        FROM pg_stat_statements
        ORDER BY total_exec_time DESC
        LIMIT %s
        """,
        (limit,),
    )
    return {"available": True, "queries": queries}


def build_observability_report(conn, *, limit: int = 10) -> dict[str, object]:
    _require_postgres(conn)
    return {
        "extensions": pg_extension_status(conn),
        "relation_sizes": relation_sizes(conn, limit=limit),
        "maintenance": maintenance_status(conn, limit=limit),
        "indexes": index_inventory(conn, limit=limit),
        "slow_queries": slow_queries(conn, limit=limit),
    }
