<!--
Thank you for the PR! Fill in the sections below.
Keep the title in Conventional Commit form, e.g. `feat(storage): add X`.
-->

## Summary

<!-- 1–3 bullets: what changed and why -->

## Type of change

- [ ] feat — new user-facing capability
- [ ] fix — bug fix
- [ ] chore — tooling / infra / deps
- [ ] docs — documentation only
- [ ] refactor — no behaviour change
- [ ] test — tests only
- [ ] perf — performance improvement

## Checklist

- [ ] Tests added / updated (unit, integration, contract as appropriate)
- [ ] `pytest tests/unit tests/contract` passes locally
- [ ] `ruff check` passes
- [ ] If DB schema changed: migrations on both backends + `serving_geo` read contract + docs updated
- [ ] If API contract changed: `docs/architecture/API.md` updated
- [ ] No secrets, credentials, or large binary assets included

## How to verify

<!-- Steps a reviewer can run to reproduce / validate -->
