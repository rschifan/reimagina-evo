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
| Python distributions | 91 |
| npm packages | 162 |
| of which ship in the production bundle | 64 |
| **Strong copyleft (GPL / AGPL / SSPL)** | **0** |
| Weak copyleft (LGPL / MPL / EPL / CDDL) | 8 |
| Undeclared | 0 |
| Permissive | 245 |

## Needing attention

Everything not in this section declares a permissive licence (MIT, BSD,
Apache-2.0, ISC and similar) that imposes no restriction beyond
attribution.

| Package | Version | Licence | Category | Distribution |
|---|---|---|---|---|
| `certifi` | 2026.2.25 | Mozilla Public License 2.0 (MPL 2.0) | weak-copyleft | server-side |
| `lightningcss` | 1.32.0 | MPL-2.0 | weak-copyleft | build/test only |
| `lightningcss-linux-x64-gnu` | 1.32.0 | MPL-2.0 | weak-copyleft | build/test only |
| `lightningcss-linux-x64-musl` | 1.32.0 | MPL-2.0 | weak-copyleft | build/test only |
| `pathspec` | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) | weak-copyleft | server-side |
| `psycopg` | 3.3.4 | LGPL-3.0-only | weak-copyleft | server-side |
| `psycopg-binary` | 3.3.4 | LGPL-3.0-only | weak-copyleft | server-side |
| `psycopg2-binary` | 2.9.11 | GNU Library or Lesser General Public License (LGPL) | weak-copyleft | server-side |

## Python

| Package | Version | Licence | Distribution |
|---|---|---|---|
| `affine` | 2.4.0 | BSD License | server-side |
| `alembic` | 1.18.4 | MIT | server-side |
| `annotated-doc` | 0.0.4 | MIT | server-side |
| `annotated-types` | 0.7.0 | MIT License | server-side |
| `anyio` | 4.13.0 | MIT | server-side |
| `apache-hamilton` | 1.90.0 | Apache-2.0 | server-side |
| `ast_serialize` | 0.8.0 | MIT | server-side |
| `attrs` | 26.1.0 | MIT | server-side |
| `cachetools` | 7.0.6 | MIT | server-side |
| `certifi` | 2026.2.25 | Mozilla Public License 2.0 (MPL 2.0) | server-side |
| `charset-normalizer` | 3.4.9 | MIT | server-side |
| `click` | 8.3.2 | BSD-3-Clause | server-side |
| `click-plugins` | 1.1.1.2 | BSD License | server-side |
| `cligj` | 0.7.2 | BSD | server-side |
| `color-operations` | 0.2.0 | MIT License | server-side |
| `coverage` | 7.13.5 | Apache-2.0 | server-side |
| `dbos` | 2.28.0 | MIT License | server-side |
| `exactextract` | 0.3.0 | Apache License
                            Version 2.0, January 2004
        ... | server-side |
| `fastapi` | 0.136.0 | MIT | server-side |
| `fiona` | 1.10.1 | BSD License | server-side |
| `geojson-pydantic` | 2.1.1 | MIT License | server-side |
| `geopandas` | 1.1.3 | BSD-3-Clause | server-side |
| `greenlet` | 3.4.0 | MIT AND PSF-2.0 | server-side |
| `h11` | 0.16.0 | MIT License | server-side |
| `httpcore` | 1.0.9 | BSD-3-Clause | server-side |
| `httptools` | 0.7.1 | MIT | server-side |
| `httpx` | 0.28.1 | BSD License | server-side |
| `idna` | 3.11 | BSD-3-Clause | server-side |
| `iniconfig` | 2.3.0 | MIT | server-side |
| `Jinja2` | 3.1.6 | BSD License | server-side |
| `librt` | 0.15.0 | MIT | server-side |
| `Mako` | 1.3.11 | MIT License | server-side |
| `mapbox-vector-tile` | 2.2.0 | MIT License | server-side |
| `markdown-it-py` | 4.2.0 | MIT License | server-side |
| `MarkupSafe` | 3.0.3 | BSD-3-Clause | server-side |
| `mdurl` | 0.1.2 | MIT License | server-side |
| `morecantile` | 7.0.3 | MIT License | server-side |
| `mypy` | 2.3.1 | MIT | server-side |
| `mypy_extensions` | 1.1.0 | MIT | server-side |
| `networkx` | 3.6.1 | BSD-3-Clause | server-side |
| `numexpr` | 2.14.1 | MIT | server-side |
| `numpy` | 2.4.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | server-side |
| `openlineage-python` | 1.51.0 | Apache-2.0 | server-side |
| `osmnx` | 2.1.1 | MIT | server-side |
| `packaging` | 26.1 | Apache-2.0 OR BSD-2-Clause | server-side |
| `pandas` | 3.0.2 | BSD License | server-side |
| `pandera` | 0.32.1 | MIT License | server-side |
| `pathspec` | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) | server-side |
| `pluggy` | 1.6.0 | MIT License | server-side |
| `protobuf` | 6.33.6 | 3-Clause BSD License | server-side |
| `psycopg` | 3.3.4 | LGPL-3.0-only | server-side |
| `psycopg-binary` | 3.3.4 | LGPL-3.0-only | server-side |
| `psycopg2-binary` | 2.9.11 | GNU Library or Lesser General Public License (LGPL) | server-side |
| `pyarrow` | 24.0.0 | Apache-2.0 | server-side |
| `pyclipper` | 1.4.0 | OSI Approved; MIT License | server-side |
| `pydantic` | 2.13.3 | MIT | server-side |
| `pydantic_core` | 2.46.3 | MIT | server-side |
| `Pygments` | 2.20.0 | BSD-2-Clause | server-side |
| `pyogrio` | 0.12.1 | MIT License | server-side |
| `pyparsing` | 3.3.2 | MIT | server-side |
| `pyproj` | 3.7.2 | MIT | server-side |
| `pystac` | 1.14.3 | Apache Software License | server-side |
| `pytest` | 9.0.3 | MIT | server-side |
| `pytest-cov` | 7.1.0 | MIT | server-side |
| `python-dateutil` | 2.9.0.post0 | BSD License; Apache Software License | server-side |
| `python-dotenv` | 1.2.2 | BSD-3-Clause | server-side |
| `PyYAML` | 6.0.3 | MIT License | server-side |
| `rasterio` | 1.5.0 | BSD-3-Clause | server-side |
| `requests` | 2.34.2 | Apache Software License | server-side |
| `rich` | 15.0.0 | MIT License | server-side |
| `rio-tiler` | 9.0.6 | BSD License | server-side |
| `ruff` | 0.6.9 | MIT License | server-side |
| `sf-hamilton` | 1.90.0 | Apache-2.0 | server-side |
| `shapely` | 2.1.2 | BSD License | server-side |
| `shellingham` | 1.5.4 | ISC License (ISCL) | server-side |
| `simplejson` | 4.0.1 | MIT OR AFL-2.1 | server-side |
| `six` | 1.17.0 | MIT License | server-side |
| `SQLAlchemy` | 2.0.49 | MIT | server-side |
| `starlette` | 1.0.0 | BSD-3-Clause | server-side |
| `titiler-core` | 2.0.1 | MIT License | server-side |
| `typeguard` | 4.5.2 | MIT | server-side |
| `typer` | 0.27.0 | MIT | server-side |
| `typer-slim` | 0.24.0 | MIT | server-side |
| `typing-inspect` | 0.9.0 | MIT License | server-side |
| `typing-inspection` | 0.4.2 | MIT | server-side |
| `typing_extensions` | 4.15.0 | PSF-2.0 | server-side |
| `urllib3` | 2.7.0 | MIT | server-side |
| `uvicorn` | 0.44.0 | BSD-3-Clause | server-side |
| `uvloop` | 0.22.1 | Apache Software License; MIT License | server-side |
| `watchfiles` | 1.1.1 | MIT License | server-side |
| `websockets` | 16.0 | BSD-3-Clause | server-side |

## npm

| Package | Version | Licence | Distribution |
|---|---|---|---|
| `@asamuzakjp/css-color` | 5.1.11 | MIT | build/test only |
| `@asamuzakjp/dom-selector` | 7.1.1 | MIT | build/test only |
| `@asamuzakjp/generational-cache` | 1.0.1 | MIT | build/test only |
| `@asamuzakjp/nwsapi` | 2.3.9 | MIT | build/test only |
| `@bramus/specificity` | 2.4.2 | MIT | build/test only |
| `@csstools/color-helpers` | 6.1.1 | MIT-0 | build/test only |
| `@csstools/css-calc` | 3.3.0 | MIT | build/test only |
| `@csstools/css-color-parser` | 4.2.0 | MIT | build/test only |
| `@csstools/css-parser-algorithms` | 4.0.0 | MIT | build/test only |
| `@csstools/css-syntax-patches-for-csstree` | 1.1.8 | MIT-0 | build/test only |
| `@csstools/css-tokenizer` | 4.0.0 | MIT | build/test only |
| `@exodus/bytes` | 1.15.1 | MIT | build/test only |
| `@floating-ui/core` | 1.8.0 | MIT | browser bundle |
| `@floating-ui/dom` | 1.8.0 | MIT | browser bundle |
| `@floating-ui/utils` | 0.2.12 | MIT | browser bundle |
| `@internationalized/date` | 3.12.3 | Apache-2.0 | browser bundle |
| `@jridgewell/gen-mapping` | 0.3.13 | MIT | browser bundle |
| `@jridgewell/remapping` | 2.3.5 | MIT | browser bundle |
| `@jridgewell/resolve-uri` | 3.1.2 | MIT | browser bundle |
| `@jridgewell/sourcemap-codec` | 1.5.5 | MIT | browser bundle |
| `@jridgewell/trace-mapping` | 0.3.31 | MIT | browser bundle |
| `@lucide/svelte` | 1.32.0 | ISC | browser bundle |
| `@mapbox/jsonlint-lines-primitives` | 2.0.3 | MIT | browser bundle |
| `@mapbox/point-geometry` | 1.1.0 | ISC | browser bundle |
| `@mapbox/tiny-sdf` | 2.3.0 | BSD-2-Clause | browser bundle |
| `@mapbox/unitbezier` | 1.0.0 | BSD-2-Clause | browser bundle |
| `@mapbox/vector-tile` | 3.0.0 | BSD-3-Clause | browser bundle |
| `@maplibre/geojson-vt` | 6.3.0 | ISC | browser bundle |
| `@maplibre/maplibre-gl-style-spec` | 26.5.1 | ISC | browser bundle |
| `@maplibre/mlt` | 1.3.0 | (MIT OR Apache-2.0) | browser bundle |
| `@maplibre/vt-pbf` | 4.3.3 | MIT | browser bundle |
| `@oxc-project/types` | 0.144.0 | MIT | build/test only |
| `@rolldown/binding-linux-x64-gnu` | 1.2.4 | MIT | build/test only |
| `@rolldown/binding-linux-x64-musl` | 1.2.4 | MIT | build/test only |
| `@rolldown/pluginutils` | 1.0.1 | MIT | build/test only |
| `@standard-schema/spec` | 1.1.0 | MIT | build/test only |
| `@sveltejs/acorn-typescript` | 1.0.13 | MIT | browser bundle |
| `@sveltejs/load-config` | 0.2.3 | MIT | build/test only |
| `@sveltejs/vite-plugin-svelte` | 7.3.0 | MIT | build/test only |
| `@swc/helpers` | 0.5.23 | Apache-2.0 | browser bundle |
| `@tailwindcss/node` | 4.3.3 | MIT | build/test only |
| `@tailwindcss/oxide` | 4.3.3 | MIT | build/test only |
| `@tailwindcss/oxide-linux-x64-gnu` | 4.3.3 | MIT | build/test only |
| `@tailwindcss/oxide-linux-x64-musl` | 4.3.3 | MIT | build/test only |
| `@tailwindcss/vite` | 4.3.3 | MIT | build/test only |
| `@types/chai` | 5.2.3 | MIT | build/test only |
| `@types/deep-eql` | 4.0.2 | MIT | build/test only |
| `@types/estree` | 1.0.8 | MIT | browser bundle |
| `@types/geojson` | 7946.0.16 | MIT | browser bundle |
| `@types/trusted-types` | 2.0.7 | MIT | browser bundle |
| `@vitest/expect` | 4.1.11 | MIT | build/test only |
| `@vitest/mocker` | 4.1.11 | MIT | build/test only |
| `@vitest/pretty-format` | 4.1.11 | MIT | build/test only |
| `@vitest/runner` | 4.1.11 | MIT | build/test only |
| `@vitest/snapshot` | 4.1.11 | MIT | build/test only |
| `@vitest/spy` | 4.1.11 | MIT | build/test only |
| `@vitest/utils` | 4.1.11 | MIT | build/test only |
| `acorn` | 8.18.0 | MIT | browser bundle |
| `aria-query` | 5.3.1 | Apache-2.0 | browser bundle |
| `assertion-error` | 2.0.1 | MIT | build/test only |
| `axobject-query` | 4.1.0 | Apache-2.0 | browser bundle |
| `bidi-js` | 1.1.0 | MIT | browser bundle |
| `bits-ui` | 2.18.1 | MIT | browser bundle |
| `chai` | 6.2.2 | MIT | build/test only |
| `chokidar` | 4.0.3 | MIT | build/test only |
| `clsx` | 2.1.1 | MIT | browser bundle |
| `convert-source-map` | 2.0.0 | MIT | build/test only |
| `css-tree` | 3.2.1 | MIT | build/test only |
| `data-urls` | 7.0.0 | MIT | build/test only |
| `decimal.js` | 10.6.0 | MIT | build/test only |
| `deepmerge` | 4.3.1 | MIT | build/test only |
| `dequal` | 2.0.3 | MIT | browser bundle |
| `detect-libc` | 2.1.2 | Apache-2.0 | build/test only |
| `devalue` | 5.9.0 | MIT | browser bundle |
| `earcut` | 3.2.4 | ISC | browser bundle |
| `enhanced-resolve` | 5.24.5 | MIT | build/test only |
| `entities` | 8.0.0 | BSD-2-Clause | build/test only |
| `es-module-lexer` | 2.3.2 | MIT | build/test only |
| `esm-env` | 1.2.2 | MIT | browser bundle |
| `esrap` | 2.3.4 | MIT | browser bundle |
| `estree-walker` | 3.0.3 | MIT | build/test only |
| `expect-type` | 1.3.0 | Apache-2.0 | build/test only |
| `fdir` | 6.5.0 | MIT | build/test only |
| `gl-matrix` | 3.4.4 | MIT | browser bundle |
| `graceful-fs` | 4.2.11 | ISC | build/test only |
| `html-encoding-sniffer` | 6.0.0 | MIT | build/test only |
| `inline-style-parser` | 0.2.7 | MIT | browser bundle |
| `is-potential-custom-element-name` | 1.0.1 | MIT | build/test only |
| `is-reference` | 3.0.3 | MIT | browser bundle |
| `jiti` | 2.7.0 | MIT | build/test only |
| `jsdom` | 29.1.1 | MIT | build/test only |
| `json-stringify-pretty-compact` | 4.0.0 | MIT | browser bundle |
| `kdbush` | 4.1.0 | ISC | browser bundle |
| `lightningcss` | 1.32.0 | MPL-2.0 | build/test only |
| `lightningcss-linux-x64-gnu` | 1.32.0 | MPL-2.0 | build/test only |
| `lightningcss-linux-x64-musl` | 1.32.0 | MPL-2.0 | build/test only |
| `locate-character` | 3.0.0 | MIT | browser bundle |
| `lru-cache` | 11.5.2 | BlueOak-1.0.0 | build/test only |
| `lz-string` | 1.5.0 | MIT | browser bundle |
| `magic-string` | 0.30.21 | MIT | browser bundle |
| `maplibre-gl` | 6.17.0 | BSD-3-Clause | browser bundle |
| `mdn-data` | 2.27.1 | CC0-1.0 | build/test only |
| `minimist` | 1.2.8 | MIT | browser bundle |
| `mri` | 1.2.0 | MIT | build/test only |
| `murmurhash-js` | 1.0.0 | MIT | browser bundle |
| `nanoid` | 3.3.18 | MIT | build/test only |
| `obug` | 2.1.4 | MIT | build/test only |
| `parse5` | 8.0.1 | MIT | build/test only |
| `pathe` | 2.0.3 | MIT | build/test only |
| `pbf` | 5.1.2 | BSD-3-Clause | browser bundle |
| `picocolors` | 1.1.1 | ISC | build/test only |
| `picomatch` | 4.0.5 | MIT | build/test only |
| `postcss` | 8.5.26 | MIT | build/test only |
| `potpack` | 2.1.0 | ISC | browser bundle |
| `protocol-buffers-schema` | 3.6.1 | MIT | browser bundle |
| `punycode` | 2.3.1 | MIT | build/test only |
| `quickselect` | 3.0.0 | ISC | browser bundle |
| `readdirp` | 4.1.2 | MIT | build/test only |
| `require-from-string` | 2.0.2 | MIT | browser bundle |
| `resolve-protobuf-schema` | 2.1.0 | MIT | browser bundle |
| `rolldown` | 1.2.4 | MIT | build/test only |
| `sade` | 1.8.1 | MIT | build/test only |
| `saxes` | 6.0.0 | ISC | build/test only |
| `siginfo` | 2.0.0 | ISC | build/test only |
| `source-map-js` | 1.2.1 | BSD-3-Clause | build/test only |
| `stackback` | 0.0.2 | MIT | build/test only |
| `std-env` | 4.2.0 | MIT | build/test only |
| `style-to-object` | 1.0.14 | MIT | browser bundle |
| `svelte` | 5.56.9 | MIT | browser bundle |
| `svelte-check` | 4.7.6 | MIT | build/test only |
| `svelte-toolbelt` | 0.10.6 | MIT (per LICENSE; not declared in manifest) | browser bundle |
| `symbol-tree` | 3.2.4 | MIT | build/test only |
| `tabbable` | 6.5.0 | MIT | browser bundle |
| `tailwind-merge` | 3.6.0 | MIT | browser bundle |
| `tailwind-variants` | 3.3.1 | MIT | browser bundle |
| `tailwindcss` | 4.3.3 | MIT | browser bundle |
| `tapable` | 2.3.3 | MIT | build/test only |
| `terra-draw` | 1.32.2 | MIT | browser bundle |
| `terra-draw-maplibre-gl-adapter` | 1.4.1 | MIT | browser bundle |
| `tinybench` | 2.9.0 | MIT | build/test only |
| `tinyexec` | 1.3.0 | MIT | build/test only |
| `tinyglobby` | 0.2.17 | MIT | build/test only |
| `tinyqueue` | 3.0.0 | ISC | browser bundle |
| `tinyrainbow` | 3.1.1 | MIT | build/test only |
| `tldts` | 7.4.10 | MIT | build/test only |
| `tldts-core` | 7.4.10 | MIT | build/test only |
| `tough-cookie` | 6.0.2 | BSD-3-Clause | build/test only |
| `tr46` | 6.0.0 | MIT | build/test only |
| `tslib` | 2.8.1 | 0BSD | browser bundle |
| `typescript` | 5.9.3 | Apache-2.0 | build/test only |
| `undici` | 7.29.0 | MIT | build/test only |
| `vite` | 8.2.1 | MIT | build/test only |
| `vitefu` | 1.1.3 | MIT | build/test only |
| `vitest` | 4.1.11 | MIT | build/test only |
| `w3c-xmlserializer` | 5.0.0 | MIT | build/test only |
| `webidl-conversions` | 8.0.1 | BSD-2-Clause | build/test only |
| `whatwg-mimetype` | 5.0.0 | MIT | build/test only |
| `whatwg-url` | 16.0.1 | MIT | build/test only |
| `why-is-node-running` | 2.3.0 | MIT | build/test only |
| `xml-name-validator` | 5.0.0 | Apache-2.0 | build/test only |
| `xmlchars` | 2.2.0 | MIT | build/test only |
| `zimmerframe` | 1.1.4 | MIT | browser bundle |
