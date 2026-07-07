# Reimagina Phase 3 — Target-Based Interventions (Draft Proposal)

> Status: draft for discussion. Nothing here is committed to implementation.
> Audience: Reimagina core team + client (Uriel). Scope: intervention
> attributes that act as *spatial targets*, with specialized models that
> allocate geospatial elements to satisfy those targets — and a plug-and-play
> path for adding new allocation models and use cases.
>
> ⚠️ **Architecture mismatch — read before implementing.** This proposal is
> written on the legacy `element_state` event-sourced model (intervention log,
> `scenario_generation`, "replay"), which was **never built** — there are zero
> references to `element_state` in the codebase. The shipped system uses
> `scenario_override` + effective-value resolution + the attribute-keyed
> dirty-set. Re-base this proposal onto that model before acting on it.

## 1. The core idea

Some intervention attributes are not concrete edits — they are **declarative
targets**. The system figures out *how* to satisfy them. Two illustrative
cases from the brief:

- **Sidewalk: distance between trees.** "Trees 8 m apart" is a target. A model
  decides *where* to place the tree elements to meet it.
- **Land use: % commercial vs. residential.** A target proportion over a
  territory. A model decides *which parcels* get which use, with some
  controlled randomness in the allocation.

## 2. Key architectural insight

**A target is a declarative intervention; a specialized model elaborates it
into concrete elements.**

This fits the Phase-2 event-sourced core cleanly and preserves its central
invariant — *replaying the intervention log always produces the same result*.
We add one pipeline stage between authoring (Stage 1) and resolve (Stage 2):

> **Stage 1.5 — Elaboration / Allocation.** A `target` intervention is read by
> an **Allocator** (the specialized model), which produces concrete element
> placements (tree points) or attribute assignments (land use per parcel).

```
Stage 1   author target intervention   (API)
Stage 1.5 allocator elaborates target → concrete elements   (worker)   ← NEW
Stage 2   replay / resolve element_state   (worker)
Stage 3   rebuild AoI overlap   (worker)
Stage 4   recompute indicators   (worker)
Stage 5   compare panel   (API)
```

A target intervention looks like:

```json
{
  "operation":   "target",
  "target_type": "tree_spacing",
  "params":      { "spacing_m": 8 },
  "scope":       "<sidewalk element id / AoI id>"
}
```

Downstream is unchanged: allocator outputs are just elements / attributes,
consumed by the Stage-3 overlap rebuild and Stage-4 indicator recompute like
any other edit.

## 3. Two hard requirements

These are not optional — skipping either breaks event sourcing.

1. **Persist the realized output, not just the target.** Allocation is
   expensive and/or stochastic. The allocator runs **once** at Generate and
   writes concrete elements into `element_state` (origin `scenario:S`). Replay
   reads the materialized result; it does **not** re-run the model.

2. **Record seed + allocator version** in the generation audit
   (`scenario_generation`). "Controlled randomness" must be reproducible or the
   replay invariant breaks. A seeded RNG plus the stored seed gives
   deterministic replay while still looking random to the user. Version the
   allocator the same way indicators record `indicator_code_version`.

## 4. Plug-and-play: one protocol + a registry

The client's explicit ask is that new models feel plug-and-play. Use the
**same pattern already used by the indicator catalog** (`indicators/
catalog.py`) — one interface, one registry. This is symmetric with existing
code, not a new paradigm.

```python
from typing import Protocol
from random import Random

class Allocator(Protocol):
    target_type: str

    def allocate(
        self,
        target: Target,
        ctx: SpatialContext,      # resolved element_state / AoI geometry in scope
        rng: Random,              # seeded — see requirement #2
    ) -> list[ElementPlacement]:  # concrete elements / attribute assignments
        ...
```

- A **registry** maps `target_type → Allocator`. Adding a new model =
  implement the protocol + register. No change to the pipeline.
- Every allocator ships a **target-satisfaction check** as a post-condition
  (spacing ≈ 8 m ± ε; land-use % within tolerance). An allocator without its
  assertion is unfinished. This fits naturally as an asset-check / validation
  step.

## 5. Concrete models — no reinvention, laddered by hardness

Pick the lowest rung that satisfies the target. Do not jump to ML or an
optimizer when geometry suffices.

| Target | Tool | Note |
|---|---|---|
| **Tree spacing along a line** | **Shapely** `LineString.interpolate` / `segmentize` | Pure geometry, ~10 lines. **Not ML.** Resist overengineering. |
| **Land use %, controlled randomness** | Seeded weighted assignment (suitability weights + target proportions) | Often enough. The lazy first rung. |
| **Land use with spatial constraints** | **`spopt`** (PySAL spatial optimization) | p-median / location-allocation / regionalization, GeoDataFrame-native. Industry standard, fits our GeoPandas stack — no wheel reinvention. |
| **Hard constraints** (contiguity, quotas, adjacency) | **Google OR-Tools** (CP-SAT / MILP) | SOTA solver. Use only when `spopt` heuristics are insufficient. |
| **Spatial coherence + randomness** | Simulated annealing / Monte Carlo | "Controlled randomness" with spatial structure. |
| **Future ML suitability** | Same `Allocator` interface; **MLflow** for versioning | Predict a suitability surface → allocate against it. Plugs into the same protocol, no architecture change. |

### Tree spacing — illustrative

```python
from shapely import LineString

def place_trees(sidewalk: LineString, spacing_m: float) -> list:
    n = int(sidewalk.length // spacing_m)
    return [sidewalk.interpolate(i * spacing_m) for i in range(n + 1)]
```

(Real version reprojects to a metric CRS before measuring — `length` on a 4326
geometry is in degrees, the same trap the Phase-2 indicators flag for
`ST_Area`.)

## 6. How it stays modular

- **Targets are interventions** — one new `operation` value, no new write
  model.
- **Allocators are pluggable models behind one protocol + registry** — mirrors
  the indicator catalog; new model / new use case = implement + register.
- **Outputs are concrete elements** persisted with seed + version — reproducible
  replay, no special-casing downstream.
- **Validation is a post-condition** per allocator — target satisfied within
  tolerance, or the generation fails honestly.
- **The recalc engine** (see the Job Orchestration doc) treats allocator
  outputs like any other elements / attributes — the impact graph picks them up
  for free.

New indicators *and* new allocation models slot into the same two registries.
That is the seamless, plug-and-play story.

## 7. What to deliberately skip for now

| Skip | Add when |
|---|---|
| OR-Tools | `spopt` heuristics demonstrably fall short on real constraints |
| ML allocators | A trained suitability model exists and beats weighted heuristics |
| MLflow | First model is trained / versioned |
| A generic constraint DSL | More than ~2 constraint families recur across allocators |

Start with **Shapely** (tree spacing) + **seeded weighted allocation** (land
use). Add `spopt` when real spatial constraints appear; OR-Tools only when
`spopt` is not enough.

## 8. One-paragraph summary

Target-based interventions become a new declarative intervention type
elaborated by pluggable "allocator" models behind a single interface and
registry — the same pattern the indicator catalog already uses. The allocator
runs once at Generate, materializes concrete elements into the projection, and
records its seed and version so stochastic allocation still replays
deterministically. Models ladder from pure Shapely geometry (tree spacing) to
PySAL `spopt` and Google OR-Tools (constrained land-use allocation), with ML
suitability models slotting into the same interface later via MLflow — no
rearchitecture per new model or use case.
