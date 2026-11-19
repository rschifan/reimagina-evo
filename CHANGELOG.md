# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

From `v1.0.0` the public API, database schema, and CLI contracts follow Semantic Versioning. `0.y` releases made no stability promise.

## [Unreleased]

## [1.0.0] — 2026-11-20

First stable release. It is the cumulative result of the work since `0.1.0`:
the indicator layer, the orchestration plane, scenario authoring, and the
delivery documentation.

### Added

- Twelve indicators at block grain, including accessibility over the street
  network, a density + accessibility livability score and an urban-form
  composite; composite inputs are oriented and normalised so the weights mean
  what they say.
- An asset graph and one version algebra: an edit to an attribute invalidates
  exactly the indicators that read it, and a completed load recomputes what it
  invalidated.
- An orchestration plane on DBOS 3: per-unit run state, as-completed dispatch,
  cancellable runs, SSE run telemetry, a dry run of the planner and run
  controls, with a conformance suite that asserts the port contract.
- Scenario authoring: draw a rule on the map, override attributes, read the
  result as an overlay on the baseline, with delta tiles and a scenario
  workspace page for planners.
- Web app: indicator catalog for operators, a run dashboard and execution
  timeline, an exploration map page with legend and inspector, an AOI picker,
  a grain selector, and AOI-scoped downloads.
- Pilot data pipelines: INEGI Marco Geoestadístico, census observations and
  roll-ups, per-city amenity ingest, WorldPop and raster zonal statistics.
- The delivery documents (architecture map, database and API reference,
  technical documentation), their reproducible PDF renderer,
  the delivery manifest generator and `scripts/acceptance_run.sh`.
- 62 API operations; database schema at migration `r051`.

### Changed

- The CLI moved from argparse to Typer.
- Only block grain is offered; the AGEB grain and the straight-line indicator
  family were retired.
- The DuckDB backend was removed; PostgreSQL is the single storage engine.
- `data_source_catalog.storage_uri` records where data came from.
- The API waits for a free pooled connection instead of failing.
- `make release` builds the release bundle (PDFs, manifest, checksums, source
  archive) into `dist/`.
- Internal tooling files are no longer part of the repository.

### Fixed

- `storage._utils.scenario_overlay` returned a feature twice when a scenario had
  a valueless (`quality_flag='missing'`) row for a feature the baseline had a
  value for: the value test guarded only the anti-join, so the
  `scenario_id = X` branch still matched the valueless row alongside the
  baseline's. Readers that key by feature collapsed the duplicate to whichever
  row arrived last — plan-dependent, hence an intermittent wrong value rather
  than a visible duplicate — and `fetch_aoi_children` double-counted the
  feature's weight in the AOI summary. A scenario row now qualifies only when it
  carries a value or no baseline row exists for the pair.

## [0.1.0] — 2026-04-22

### Added

- Feature / observation / indicator / scenario data model on PostGIS.
- Bronze → silver → `curated_geo` → `serving_geo` ingest pipeline.
- Raster ingest with COG conversion; `curated_geo.raster_layer` registry.
- Declarative aggregation recipes (`curated_geo.aggregation_recipe`) for raster→observation and observation→observation rollups.
- Live MVT tile functions (Martin) + in-process titiler router for COGs.
- FastAPI read-side API: `/tile-sources`, `/indicators`, `/indicators/{slug}/stats`, downloads.
- MapLibre-GL web app with modular hover-tooltip and hover-highlight primitives.
- Operator CLI: ingest, publish, status, raster pipelines, derivation management.

[Unreleased]: https://github.com/rschifan/reimagina-evo/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/rschifan/reimagina-evo/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/rschifan/reimagina-evo/releases/tag/v0.1.0
