# Third-party dependencies and licences

Complete inventory of the third-party and open-source code this platform
incorporates, with the licence each one declares.

> **Generated — do not edit.** Regenerate with
> `python scripts/third_party_licences.py`, which reads the installed
> Python distributions and every `package.json` under
> `apps/web/node_modules`. Regenerate before any delivery, so the list
> describes the code actually being delivered rather than the code that
> was installed when someone last remembered.

`Distribution` separates code that reaches a browser from code used
only to build or test, and from server-side Python. A build-time
formatter and a library served to every user are not the same
disclosure.

## Summary

| | Count |
|---|---|
| Python distributions | 115 |
| npm packages | 144 |
| of which ship in the production bundle | 63 |
| **Strong copyleft (GPL / AGPL / SSPL)** | **0** |
| Weak copyleft (LGPL / MPL / EPL / CDDL) | 8 |
| Undeclared | 0 |
| Permissive | 251 |

## Needing attention

Everything not in this section declares a permissive licence (MIT, BSD,
Apache-2.0, ISC and similar) that imposes no restriction beyond
attribution.

| Package | Version | Licence | Category | Distribution |
|---|---|---|---|---|
| `certifi` | 2026.10.23 | Mozilla Public License 2.0 (MPL 2.0) | weak-copyleft | server-side |
| `lightningcss` | 1.33.0 | MPL-2.0 | weak-copyleft | build/test only |
| `lightningcss-linux-x64-gnu` | 1.33.0 | MPL-2.0 | weak-copyleft | build/test only |
| `lightningcss-linux-x64-musl` | 1.33.0 | MPL-2.0 | weak-copyleft | build/test only |
| `pathspec` | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) | weak-copyleft | server-side |
| `psycopg` | 3.3.8 | LGPL-3.0-only | weak-copyleft | server-side |
| `psycopg-binary` | 3.3.8 | LGPL-3.0-only | weak-copyleft | server-side |
| `psycopg2-binary` | 2.9.13 | GNU Library or Lesser General Public License (LGPL) | weak-copyleft | server-side |

## Python

| Package | Version | Licence | Distribution |
|---|---|---|---|
| `affine` | 3.0.1 | BSD-3-Clause | server-side |
| `alembic` | 1.20.1 | MIT | server-side |
| `annotated-doc` | 0.0.5 | MIT | server-side |
| `annotated-types` | 0.8.0 | MIT | server-side |
| `anyio` | 4.16.0 | MIT | server-side |
| `apache-hamilton` | 1.90.0 | Apache-2.0 | server-side |
| `ast_serialize` | 0.13.1 | MIT | server-side |
| `attrs` | 26.1.0 | MIT | server-side |
| `cachetools` | 8.0.0 | MIT | server-side |
| `certifi` | 2026.10.23 | Mozilla Public License 2.0 (MPL 2.0) | server-side |
| `charset-normalizer` | 3.5.2 | MIT | server-side |
| `click` | 8.5.1 | BSD-3-Clause | server-side |
| `color-operations` | 0.4.0 | MIT | server-side |
| `coverage` | 7.17.2 | Apache-2.0 | server-side |
| `croniter` | 6.2.4 | MIT | server-side |
| `dbos` | 3.4.0 | MIT License | server-side |
| `exactextract` | 0.3.0 | Apache License
                            Version 2.0, January 2004
        ... | server-side |
| `fastapi` | 0.162.2 | MIT | server-side |
| `geojson-pydantic` | 2.2.0 | MIT License | server-side |
| `geopandas` | 1.2.0 | BSD-3-Clause | server-side |
| `greenlet` | 3.5.6 | MIT AND PSF-2.0 | server-side |
| `h11` | 0.16.0 | MIT License | server-side |
| `httpcore` | 1.0.9 | BSD-3-Clause | server-side |
| `httpcore2` | 2.16.0 | BSD-3-Clause | server-side |
| `httptools` | 0.9.0 | MIT | server-side |
| `httpx` | 0.28.1 | BSD License | server-side |
| `httpx2` | 2.16.0 | BSD-3-Clause | server-side |
| `idna` | 3.20 | BSD-3-Clause | server-side |
| `iniconfig` | 2.3.1 | MIT | server-side |
| `Jinja2` | 3.1.6 | BSD License | server-side |
| `librt` | 0.16.0 | MIT | server-side |
| `Mako` | 1.4.3 | MIT | server-side |
| `mapbox-vector-tile` | 2.2.0 | MIT License | server-side |
| `markdown-it-py` | 4.2.0 | MIT License | server-side |
| `MarkupSafe` | 3.0.4 | BSD-3-Clause | server-side |
| `mdurl` | 0.1.2 | MIT License | server-side |
| `morecantile` | 7.1.0 | MIT License | server-side |
| `mypy` | 2.4.1 | MIT | server-side |
| `mypy_extensions` | 1.2.0 | MIT | server-side |
| `networkx` | 3.7 | BSD-3-Clause | server-side |
| `numexpr` | 2.14.2 | MIT | server-side |
| `numpy` | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | server-side |
| `openlineage-python` | 1.55.0 | Apache-2.0 | server-side |
| `opentelemetry-api` | 1.45.2 | Apache-2.0 | server-side |
| `osmnx` | 2.1.1 | MIT | server-side |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause | server-side |
| `pandas` | 3.1.1 | BSD-3-Clause | server-side |
| `pandera` | 0.35.0 | MIT License | server-side |
| `pathspec` | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) | server-side |
| `pluggy` | 1.7.0 | MIT License | server-side |
| `protobuf` | 6.33.6 | 3-Clause BSD License | server-side |
| `psycopg` | 3.3.8 | LGPL-3.0-only | server-side |
| `psycopg-binary` | 3.3.8 | LGPL-3.0-only | server-side |
| `psycopg2-binary` | 2.9.13 | GNU Library or Lesser General Public License (LGPL) | server-side |
| `pyarrow` | 26.0.1 | Apache-2.0 | server-side |
| `pyclipper` | 1.4.0 | OSI Approved; MIT License | server-side |
| `pydantic` | 2.14.3 | MIT | server-side |
| `pydantic_core` | 2.50.4 | MIT | server-side |
| `Pygments` | 2.21.0 | BSD-2-Clause | server-side |
| `pyogrio` | 0.13.0 | MIT License | server-side |
| `pyparsing` | 3.3.3 | MIT | server-side |
| `pyproj` | 3.8.0 | MIT | server-side |
| `pystac` | 1.16.0 | Apache Software License | server-side |
| `pystac-core` | 1.16.0 | Apache Software License | server-side |
| `pystac-ext-classification` | 2.0.2 | Apache Software License | server-side |
| `pystac-ext-datacube` | 2.2.2 | Apache Software License | server-side |
| `pystac-ext-eo` | 1.1.2 | Apache Software License | server-side |
| `pystac-ext-file` | 2.1.2 | Apache Software License | server-side |
| `pystac-ext-grid` | 1.1.2 | Apache Software License | server-side |
| `pystac-ext-item-assets` | 1.0.2 | Apache Software License | server-side |
| `pystac-ext-label` | 1.0.3 | Apache Software License | server-side |
| `pystac-ext-mgrs` | 1.0.2 | Apache Software License | server-side |
| `pystac-ext-mlm` | 1.4.2 | Apache Software License | server-side |
| `pystac-ext-pointcloud` | 1.0.2 | Apache Software License | server-side |
| `pystac-ext-projection` | 2.0.2 | Apache Software License | server-side |
| `pystac-ext-raster` | 1.1.2 | Apache Software License | server-side |
| `pystac-ext-render` | 2.0.1 | Apache Software License | server-side |
| `pystac-ext-sar` | 1.0.2 | Apache Software License | server-side |
| `pystac-ext-sat` | 1.0.2 | Apache Software License | server-side |
| `pystac-ext-scientific` | 1.0.2 | Apache Software License | server-side |
| `pystac-ext-storage` | 2.0.2 | Apache Software License | server-side |
| `pystac-ext-table` | 1.2.2 | Apache Software License | server-side |
| `pystac-ext-timestamps` | 1.1.2 | Apache Software License | server-side |
| `pystac-ext-version` | 1.2.2 | Apache Software License | server-side |
| `pystac-ext-view` | 1.0.2 | Apache Software License | server-side |
| `pystac-ext-xarray-assets` | 1.0.2 | Apache Software License | server-side |
| `pytest` | 9.2.0 | MIT | server-side |
| `pytest-cov` | 7.1.0 | MIT | server-side |
| `python-dateutil` | 4.0.0 | BSD-3-Clause | server-side |
| `python-dotenv` | 1.2.4 | BSD-3-Clause | server-side |
| `PyYAML` | 6.0.3 | MIT License | server-side |
| `rasterio` | 1.5.2 | BSD-3-Clause | server-side |
| `requests` | 2.34.2 | Apache Software License | server-side |
| `rich` | 15.0.0 | MIT License | server-side |
| `rio-tiler` | 9.4.13 | BSD License | server-side |
| `ruff` | 0.17.4 | MIT | server-side |
| `sf-hamilton` | 1.90.0 | Apache-2.0 | server-side |
| `shapely` | 2.2.0 | BSD-3-Clause | server-side |
| `shellingham` | 1.5.4 | ISC License (ISCL) | server-side |
| `simplejson` | 4.2.0 | MIT OR AFL-2.1 | server-side |
| `SQLAlchemy` | 2.1.8 | MIT | server-side |
| `starlette` | 1.10.0 | BSD-3-Clause | server-side |
| `titiler-core` | 2.4.0 | MIT License | server-side |
| `truststore` | 0.10.5 | MIT | server-side |
| `typeguard` | 4.6.0 | MIT | server-side |
| `typer` | 0.28.0 | MIT | server-side |
| `typing-inspect` | 0.9.0 | MIT License | server-side |
| `typing-inspection` | 0.4.5 | MIT | server-side |
| `typing_extensions` | 4.16.0 | PSF-2.0 | server-side |
| `tzdata` | 2026.6 | Apache-2.0 | server-side |
| `urllib3` | 2.8.0 | MIT | server-side |
| `uvicorn` | 0.54.0 | BSD-3-Clause | server-side |
| `uvloop` | 0.23.0 | Apache Software License; MIT License | server-side |
| `watchfiles` | 1.3.0 | MIT License | server-side |
| `websockets` | 17.4 | BSD-3-Clause | server-side |

## npm

| Package | Version | Licence | Distribution |
|---|---|---|---|
| `@exodus/bytes` | 1.16.0 | MIT | build/test only |
| `@floating-ui/core` | 1.8.0 | MIT | browser bundle |
| `@floating-ui/dom` | 1.8.0 | MIT | browser bundle |
| `@floating-ui/utils` | 0.2.12 | MIT | browser bundle |
| `@internationalized/date` | 3.12.3 | Apache-2.0 | browser bundle |
| `@jridgewell/gen-mapping` | 0.3.13 | MIT | browser bundle |
| `@jridgewell/remapping` | 2.3.5 | MIT | browser bundle |
| `@jridgewell/resolve-uri` | 3.1.2 | MIT | browser bundle |
| `@jridgewell/sourcemap-codec` | 1.6.0 | MIT | browser bundle |
| `@jridgewell/trace-mapping` | 0.3.31 | MIT | browser bundle |
| `@lucide/svelte` | 1.70.0 | ISC | browser bundle |
| `@mapbox/jsonlint-lines-primitives` | 2.0.3 | MIT | browser bundle |
| `@mapbox/point-geometry` | 1.1.0 | ISC | browser bundle |
| `@mapbox/tiny-sdf` | 2.3.0 | BSD-2-Clause | browser bundle |
| `@mapbox/unitbezier` | 1.0.0 | BSD-2-Clause | browser bundle |
| `@mapbox/vector-tile` | 3.0.0 | BSD-3-Clause | browser bundle |
| `@maplibre/geojson-vt` | 6.3.0 | ISC | browser bundle |
| `@maplibre/maplibre-gl-style-spec` | 26.5.1 | ISC | browser bundle |
| `@maplibre/mlt` | 1.3.0 | (MIT OR Apache-2.0) | browser bundle |
| `@maplibre/vt-pbf` | 4.3.3 | MIT | browser bundle |
| `@oxc-project/types` | 0.156.0 | MIT | build/test only |
| `@rolldown/binding-linux-x64-gnu` | 1.2.16 | MIT | build/test only |
| `@rolldown/binding-linux-x64-musl` | 1.2.16 | MIT | build/test only |
| `@rolldown/pluginutils` | 1.0.1 | MIT | build/test only |
| `@sveltejs/acorn-typescript` | 1.0.13 | MIT | browser bundle |
| `@sveltejs/load-config` | 0.2.3 | MIT | build/test only |
| `@sveltejs/vite-plugin-svelte` | 7.3.1 | MIT | build/test only |
| `@swc/helpers` | 0.5.23 | Apache-2.0 | browser bundle |
| `@tailwindcss/node` | 4.3.5 | MIT | build/test only |
| `@tailwindcss/oxide` | 4.3.5 | MIT | build/test only |
| `@tailwindcss/oxide-linux-x64-gnu` | 4.3.5 | MIT | build/test only |
| `@tailwindcss/oxide-linux-x64-musl` | 4.3.5 | MIT | build/test only |
| `@tailwindcss/oxide-wasm32-wasi` | 4.3.5 | MIT | build/test only |
| `@tailwindcss/vite` | 4.3.5 | MIT | build/test only |
| `@types/chai` | 5.2.3 | MIT | build/test only |
| `@types/deep-eql` | 4.0.2 | MIT | build/test only |
| `@types/estree` | 1.0.10 | MIT | browser bundle |
| `@types/geojson` | 7946.0.16 | MIT | browser bundle |
| `@vitest/mocker` | 5.1.0 | MIT | build/test only |
| `@vitest/spy` | 5.1.0 | MIT | build/test only |
| `acorn` | 8.18.0 | MIT | browser bundle |
| `aria-query` | 5.3.1 | Apache-2.0 | browser bundle |
| `assertion-error` | 2.0.1 | MIT | build/test only |
| `axobject-query` | 4.1.0 | Apache-2.0 | browser bundle |
| `bcp-47` | 2.1.2 | MIT | build/test only |
| `bcp-47-match` | 2.0.3 | MIT | build/test only |
| `bidi-js` | 1.1.0 | MIT | browser bundle |
| `bits-ui` | 2.20.6 | MIT | browser bundle |
| `chai` | 6.3.0 | MIT | build/test only |
| `chokidar` | 4.0.3 | MIT | build/test only |
| `clsx` | 2.1.1 | MIT | browser bundle |
| `data-urls` | 8.0.0 | MIT | build/test only |
| `decimal.js` | 10.6.0 | MIT | build/test only |
| `deepmerge` | 4.3.1 | MIT | build/test only |
| `dequal` | 2.0.3 | MIT | browser bundle |
| `detect-libc` | 2.1.2 | Apache-2.0 | build/test only |
| `devalue` | 5.9.4 | MIT | browser bundle |
| `earcut` | 3.2.4 | ISC | browser bundle |
| `enhanced-resolve` | 5.26.1 | MIT | build/test only |
| `entities` | 9.0.0 | BSD-2-Clause | build/test only |
| `es-module-lexer` | 2.3.2 | MIT | build/test only |
| `esm-env` | 1.2.2 | MIT | browser bundle |
| `esrap` | 2.4.0 | MIT | browser bundle |
| `estree-walker` | 3.0.3 | MIT | build/test only |
| `expect-type` | 1.4.0 | Apache-2.0 | build/test only |
| `fdir` | 6.5.0 | MIT | build/test only |
| `gl-matrix` | 3.4.4 | MIT | browser bundle |
| `graceful-fs` | 4.2.11 | ISC | build/test only |
| `html-encoding-sniffer` | 7.0.0 | MIT | build/test only |
| `inline-style-parser` | 0.2.7 | MIT | browser bundle |
| `is-alphabetical` | 2.0.1 | MIT | build/test only |
| `is-alphanumerical` | 2.0.1 | MIT | build/test only |
| `is-decimal` | 2.0.1 | MIT | build/test only |
| `is-potential-custom-element-name` | 1.0.1 | MIT | build/test only |
| `is-reference` | 3.0.3 | MIT | browser bundle |
| `jiti` | 2.7.0 | MIT | build/test only |
| `jsdom` | 31.0.0 | MIT | build/test only |
| `json-stringify-pretty-compact` | 4.0.0 | MIT | browser bundle |
| `kdbush` | 4.1.0 | ISC | browser bundle |
| `lightningcss` | 1.33.0 | MPL-2.0 | build/test only |
| `lightningcss-linux-x64-gnu` | 1.33.0 | MPL-2.0 | build/test only |
| `lightningcss-linux-x64-musl` | 1.33.0 | MPL-2.0 | build/test only |
| `locate-character` | 3.0.0 | MIT | browser bundle |
| `lru-cache` | 11.5.3 | BlueOak-1.0.0 | build/test only |
| `lz-string` | 1.5.0 | MIT | browser bundle |
| `magic-string` | 0.30.21 | MIT | browser bundle |
| `maplibre-gl` | 6.17.0 | BSD-3-Clause | browser bundle |
| `minimist` | 1.2.8 | MIT | browser bundle |
| `mri` | 1.2.0 | MIT | build/test only |
| `murmurhash-js` | 1.0.0 | MIT | browser bundle |
| `nanoid` | 3.3.20 | MIT | build/test only |
| `obug` | 2.1.4 | MIT | build/test only |
| `parse5` | 8.1.1 | MIT | build/test only |
| `parse5-parser-stream` | 8.1.1 | MIT | build/test only |
| `pbf` | 5.1.2 | BSD-3-Clause | browser bundle |
| `picocolors` | 1.1.1 | ISC | build/test only |
| `picomatch` | 4.0.7 | MIT | build/test only |
| `postcss` | 8.5.32 | MIT | build/test only |
| `potpack` | 2.1.0 | ISC | browser bundle |
| `protocol-buffers-schema` | 3.6.1 | MIT | browser bundle |
| `punycode` | 2.3.1 | MIT | build/test only |
| `quickselect` | 3.0.0 | ISC | browser bundle |
| `readdirp` | 4.1.2 | MIT | build/test only |
| `require-from-string` | 2.0.2 | MIT | browser bundle |
| `resolve-protobuf-schema` | 2.1.0 | MIT | browser bundle |
| `rolldown` | 1.2.16 | MIT | build/test only |
| `runed` | 0.35.1 | MIT | browser bundle |
| `sade` | 1.8.1 | MIT | build/test only |
| `saxes` | 6.0.0 | ISC | build/test only |
| `source-map-js` | 1.2.2 | BSD-3-Clause | build/test only |
| `std-env` | 4.3.0 | MIT | build/test only |
| `style-to-object` | 1.0.14 | MIT | browser bundle |
| `svelte` | 5.57.3 | MIT | browser bundle |
| `svelte-check` | 4.7.8 | MIT | build/test only |
| `svelte-toolbelt` | 0.10.7 | MIT (per LICENSE; not declared in manifest) | browser bundle |
| `tabbable` | 6.5.1 | MIT | browser bundle |
| `tailwind-merge` | 3.7.0 | MIT | browser bundle |
| `tailwind-variants` | 4.0.1 | MIT | browser bundle |
| `tailwindcss` | 4.3.5 | MIT | build/test only |
| `tapable` | 2.3.3 | MIT | build/test only |
| `terra-draw` | 1.39.0 | MIT | browser bundle |
| `terra-draw-maplibre-gl-adapter` | 1.5.0 | MIT | browser bundle |
| `tinybench` | 6.2.3 | MIT | build/test only |
| `tinyexec` | 1.3.1 | MIT | build/test only |
| `tinyglobby` | 0.2.17 | MIT | build/test only |
| `tinyqueue` | 3.0.0 | ISC | browser bundle |
| `tldts` | 7.4.10 | MIT | build/test only |
| `tldts-core` | 7.4.10 | MIT | build/test only |
| `tough-cookie` | 6.0.2 | BSD-3-Clause | build/test only |
| `tr46` | 7.0.0 | MIT | build/test only |
| `tslib` | 2.8.1 | 0BSD | browser bundle |
| `typescript` | 5.9.3 | Apache-2.0 | build/test only |
| `undici` | 8.12.4 | MIT | build/test only |
| `vite` | 8.3.6 | MIT | build/test only |
| `vitefu` | 1.1.3 | MIT | build/test only |
| `vitest` | 5.1.0 | MIT | build/test only |
| `w3c-xmlserializer` | 6.0.0 | MIT | build/test only |
| `webidl-conversions` | 8.0.1 | BSD-2-Clause | build/test only |
| `whatwg-mimetype` | 5.0.0 | MIT | build/test only |
| `whatwg-url` | 17.3.0 | MIT | build/test only |
| `why-is-node-running` | 3.2.1 | MIT | build/test only |
| `xml-name-validator` | 5.0.0 | Apache-2.0 | build/test only |
| `xmlchars` | 2.2.0 | MIT | build/test only |
| `zimmerframe` | 1.1.4 | MIT | browser bundle |
