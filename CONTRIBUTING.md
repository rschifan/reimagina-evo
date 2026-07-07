# Contributing

Thanks for your interest in reimagina-evo.

## Development setup

```bash
make install                      # Python (uv) + web (npm) deps
cp .env.example .env              # fill DSNs
alembic upgrade head              # Postgres schema
pre-commit install                # auto-run lint/format on commit
```

Run `make help` to see every shortcut. The important ones:

| Command | What it does |
|---|---|
| `make check` | Run everything CI gates on (lint + unit/contract tests + web build). Run before every PR. |
| `make test` | Fast Python tests (unit + contract). |
| `make test-integration` | Integration tests — requires local Postgres + PostGIS. |
| `make dev-api` / `make dev-web` | Start the API / web dev server. |
| `make branch NAME=feat/x` | Create a branch off latest `main`. |
| `make ship` | `make check` + push + open PR in one go. |
| `make pr-merge` | Squash-merge the current branch's PR and delete the branch. |
| `make release VERSION=v0.2.0` | Tag + publish a GitHub release. |

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

- Python: `ruff check` + `ruff format` (run in CI and pre-commit). Prefer behaviour assertions in tests over implementation-detail assertions.
- Pre-commit: hooks defined in `.pre-commit-config.yaml` mirror CI. Run `pre-commit run --all-files` to lint/format the whole tree on demand.
- TypeScript: `tsc --noEmit` and `npm test` (vitest) must pass.
- DB changes: update the Alembic migrations, the `serving_geo` read contract if relevant, and tests — see `CLAUDE.md` → "DB/system change checklist".

## Reporting bugs / requesting features

Use the GitHub issue templates. For security-sensitive reports, see [SECURITY.md](SECURITY.md).
