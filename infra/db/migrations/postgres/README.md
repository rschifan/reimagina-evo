# PostgreSQL migrations

Alembic revisions live in `infra/db/migrations/versions`; `alembic.ini` points
to that migration tree. The PostgreSQL runner is
`packages/py/reimagina/storage/postgres/migrations.py`.

Use `make migrate` for installation: it creates missing application/test
databases, applies revisions, seeds catalogs/assets and provisions DBOS.
A bare Alembic upgrade performs only the schema-revision step.

See the [installation guide](../../../../docs/INSTALLATION.md#5-install-dependencies-and-migrate).
