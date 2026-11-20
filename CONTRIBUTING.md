# Contributing

Thanks for your interest in reimagina-evo.

## Development setup

Follow the [installation guide](docs/INSTALLATION.md): configure `.env`,
prepare PostgreSQL, run `make install`, then `make migrate`. This performs the
full database and catalog bootstrap; a bare Alembic upgrade is insufficient.
Run Python tools through `uv run` so they use the project environment.

Optional Git hooks require a separate tool installation:

```bash
uv tool install pre-commit
pre-commit install
```

Run `make help` to see every shortcut. The important ones:

| Command | What it does |
|---|---|
| `make check` | Lint, unit/contract tests, web build and web unit tests. CI also runs integration and conformance. |
| `make test` | Fast Python tests (unit + contract). |
| `make test-integration` | Integration tests — requires local Postgres + PostGIS. |
| `make dev` | Start the API, web dev server and worker together. |
| `make branch NAME=feat/x` | Create a branch off latest `main`. |
| `make ship` | `make check` + push + open PR in one go. |
| `make pr-merge` | Squash-merge the current branch's PR and delete the branch. |
| `make release VERSION=v0.2.0` | Build a local release bundle from a tagged clean `main`; requires local delivery sources. Does not publish. |

## Branch + commit conventions

- Branch off `main`. Name branches `feat/…`, `fix/…`, `chore/…`, `docs/…`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `feat(storage): …`, `fix(api): …`.
- Keep commits focused. Squash noise before opening the PR.

## Pull requests

1. Open a PR against `main`.
2. CI must be green (`lint-py`, `test-py-unit`, `test-py-integration`, `build-web`); the integration job also runs conformance.
3. One approval required before merge.
4. `main` uses **squash merge only** — the PR title becomes the commit message.

## Code quality

- Python: `uv run ruff check` + `uv run ruff format` (run in CI and pre-commit). Prefer behaviour assertions in tests over implementation-detail assertions.
- Pre-commit: hooks defined in `.pre-commit-config.yaml` use the project Ruff version and add file hygiene and Prettier checks. They do not run every CI check. Run `pre-commit run --all-files` to lint/format the whole tree on demand.
- Web: `make build-web` (typecheck and build) and `make test-web` (Vitest) must pass.
- DB changes: update the Alembic migrations, the `serving_geo` read contract if relevant, and tests — see [`docs/architecture/`](docs/architecture).

## Reporting bugs / requesting features

Use the GitHub issue templates. For security-sensitive reports, see [SECURITY.md](SECURITY.md).
