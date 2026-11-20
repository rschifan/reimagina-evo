# Docker Infrastructure

`docker-compose.yml` defines PostgreSQL/PostGIS/pgRouting, PgBouncer, Martin
and the static PMTiles host. `martin.yaml` configures tile discovery and serving.

Follow the [installation guide](../../docs/INSTALLATION.md#path-a-docker) to
configure `.env`, start PostgreSQL/PgBouncer, migrate, then start tile services.
`make stack-down` preserves database volumes.

