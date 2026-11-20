# Web App

Frontend application boundary of the prototype.

- Owns pages, components, frontend state, and API consumption
- Talks only to `apps/api`

Four pages, each its own Vite entry (`vite.config.ts`), sharing one top bar:

| Page | Entry | What it is for |
|---|---|---|
| Map | `index.html` → `src/main.ts` | The baseline city: spatial layers, indicator overlay, selection metrics |
| Scenarios | `scenarios.html` → `src/scenarios/` | The scenario workspace: every scenario on a pilot side by side, one on the map, its rules |
| Catalog | `catalog.html` → `src/catalog/` | What each indicator is and where it comes from |
| Runs | `ops.html` → `src/ops/` | The run dashboard |

The scenario workspace offers only the indicators a scenario rule can change
(`GET /scenarios/indicators`); see `docs/architecture/SCENARIOS_LAYER.md`.
