# Contributing

Thanks for your interest in reimagina-evo.

## Development setup

```bash
uv sync                           # or: pip install -e '.[dev]'
cp .env.example .env              # fill DSNs
alembic upgrade head              # Postgres + DuckDB schema
```

Run the test suites:

```bash
pytest tests/unit tests/contract              # fast, no services required
pytest tests/integration                      # requires Postgres + PostGIS running
pytest tests/performance                      # benchmarks; opt-in
```

Front-end:

```bash
cd apps/web
npm install
npm run typecheck
npm run build
```

## Branch + commit conventions

- Branch off `main`. Name branches `feat/…`, `fix/…`, `chore/…`, `docs/…`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `feat(storage): …`, `fix(api): …`.
- Keep commits focused. Squash noise before opening the PR.

## Pull requests

1. Open a PR against `main`.
2. CI must be green (`lint-py`, `test-py-unit`, `build-web`).
3. One approval required before merge.
4. `main` uses **squash merge only** — the PR title becomes the commit message.

## Code quality

- Python: `ruff check` + `ruff format` (run in CI). Prefer behaviour assertions in tests over implementation-detail assertions.
- TypeScript: `tsc --noEmit` must pass.
- DB changes: update migrations on **both** backends (Postgres + DuckDB), the `serving_geo` read contract if relevant, and tests — see `CLAUDE.md` → "DB/system change checklist".

## Reporting bugs / requesting features

Use the GitHub issue templates. For security-sensitive reports, see [SECURITY.md](SECURITY.md).
