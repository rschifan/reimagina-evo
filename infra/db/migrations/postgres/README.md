# PostgreSQL Migration Note

PostgreSQL/PostGIS migrations remain temporarily managed through the existing Alembic stack in `infra/db/migrations/versions`.

This folder exists to make the backend split explicit while the project transitions toward a backend-aware migration runner:

- `common/` holds shared logical revision ordering
- `duckdb/` holds SQL-first DuckDB migrations
- `postgres/` documents the Postgres side until the project moves its logical migration manifest fully behind the storage manager
