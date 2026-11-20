# Web App

Frontend application boundary of the prototype.

- Owns pages, components, frontend state, and API consumption
- Reads application data and submits changes through `apps/api`.
- Loads tiles from endpoints returned by the API's tile-source manifest:
  the API itself, Martin, or configured raster/snapshot hosts.

For prerequisites, configuration and startup, follow the
[installation guide](../../docs/INSTALLATION.md).

Four pages, each its own Vite entry (`vite.config.ts`), sharing one top bar:

| Page | Entry | What it is for |
|---|---|---|
| Map | `index.html` → `src/main.ts` | The baseline city: spatial layers, indicator overlay, selection metrics |
| Scenarios | `scenarios.html` → `src/scenarios/` | The scenario workspace: every scenario on a pilot side by side, one on the map, its rules |
| Catalog | `catalog.html` → `src/catalog/` | What each indicator is and where it comes from |
| Runs | `ops.html` → `src/ops/` | The run dashboard |

The scenario workspace offers only the indicators a scenario rule can change
(`GET /scenarios/indicators`); see the
[scenario reference](../../docs/architecture/SCENARIOS_LAYER.md).
