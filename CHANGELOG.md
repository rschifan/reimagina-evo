# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until `v1.0.0` the public API, database schema, and CLI contracts are considered unstable — breaking changes may land in any `0.y` release.

## [Unreleased]

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

[Unreleased]: https://github.com/rschifan/reimagina-evo/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rschifan/reimagina-evo/releases/tag/v0.1.0
