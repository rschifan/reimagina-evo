# Storage Capability Matrix

This project intentionally supports **one logical data model** on top of **two storage backends**:

- **DuckDB + `spatial`** for local development and fast integration testing
- **PostgreSQL + PostGIS** for production

## Capability summary

| Capability | DuckDB | PostgreSQL + PostGIS | Policy |
|---|---|---|---|
| Schemas / namespaces | Yes | Yes | Keep `app_core`, `curated_geo`, `serving_geo` in both |
| `GEOMETRY` type | Yes | Yes | Shared logical model |
| Spatial indexes | Limited / local-use only | GiST / production-grade | Tune only for PostGIS |
| Normal views | Yes | Yes | `serving_geo` exists in both |
| Materialized views | No | Yes | Postgres-only optimization |
| Triggers | No | Yes | Move critical invariants into shared services |
| Partial indexes | No | Yes | Postgres-only defense in depth |
| PostGIS `geography` metrics | No | Yes | Use ratio-preserving geometry math in DuckDB |
| Advanced concurrency semantics | Limited | Strong | Production stays Postgres |

## Practical rule

- If a behavior must work in both backends, enforce it in shared application/storage services.
- If a behavior is primarily about production safety or performance, keep it Postgres-first.
- DuckDB local success is useful for correctness and developer speed, but it is not proof of production parity.
