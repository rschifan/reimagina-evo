# Third-party dependencies and licences

Complete inventory of the third-party and open-source components this
platform incorporates or requires, with the licence terms of each:
Python and npm packages, native libraries vendored inside Python wheels,
system software the platform runs beside, and — listed separately, as
data rather than code — the data sources it loads.

> **Generated — do not edit.** Regenerate with
> `uv run python scripts/third_party_licences.py`; a contract test runs it
> with `--check`. Packages and versions come from `uv.lock` and
> `apps/web/package-lock.json`, resolved for the delivered platform
> (Linux x86_64, CPython 3.12, glibc), so the document is the same
> whichever machine generates it.

**Distribution.** *Runtime* Python packages are the closure of the
project's `dependencies`; *dev/test only* are those reached only through
the `dev` extra (test runner, type checker, linter). *Runtime (browser)*
npm packages are those `package-lock.json` does not mark `dev`: the
dependency tree of what the web application imports. The bundler then
includes only modules actually imported, so this column can overstate
what reaches a browser, never understate it. *Build/test only* npm
packages compile or test the web application and are not shipped.

## Summary

| Packages | Runtime | Dev / build / test only | Total |
|---|---|---|---|
| Python (`uv.lock`) | 100 | 15 | 115 |
| npm (`package-lock.json`) | 63 | 88 | 151 |
| **All packages** | 163 | 103 | 266 |

| Licence family (packages) | Runtime | Dev / build / test only | Total |
|---|---|---|---|
| permissive | 159 | 99 | 258 |
| weak copyleft | 4 | 4 | 8 |
| strong copyleft | 0 | 0 | 0 |

Also listed: 39 native-library rows vendored in 10 Python wheels, 9 system components, 9 data sources.

## Copyleft components

Every component under a copyleft licence, wherever it appears in this
document, and how the platform uses it. This is a factual description of
use, not legal advice.

**Packages**

| Component | Version | Licence | Distribution | How it is used |
|---|---|---|---|---|
| `certifi` (python) | 2026.10.23 | MPL-2.0 | runtime (server) | ships Mozilla's CA bundle as a data file, used unmodified for TLS verification |
| `lightningcss` (npm) | 1.33.0 | MPL-2.0 | build/test only | CSS transformer run by the Tailwind CSS build; its output CSS is not covered by its licence |
| `lightningcss-linux-x64-gnu` (npm) | 1.33.0 | MPL-2.0 | build/test only | native binary of lightningcss, build only |
| `lightningcss-linux-x64-musl` (npm) | 1.33.0 | MPL-2.0 | build/test only | native binary of lightningcss, build only |
| `pathspec` (python) | 1.1.1 | MPL-2.0 | dev/test only | imported only by mypy (type checker) |
| `psycopg` (python) | 3.3.8 | LGPL-3.0-only | runtime (server) | PostgreSQL driver imported by the DBOS library at run time; used unmodified, not linked into project code |
| `psycopg-binary` (python) | 3.3.8 | LGPL-3.0-only | runtime (server) | binary build of psycopg (bundles libpq; see Native libraries) |
| `psycopg2-binary` (python) | 2.9.13 | LGPL-3.0-or-later | runtime (server) | PostgreSQL driver imported by the API, storage and orchestration code; used unmodified (bundles libpq; see Native libraries) |

**Native libraries inside Python wheels** — shipped as separate shared
objects (`<package>.libs/`) loaded at run time, unmodified, unless noted
as statically linked:

- GEOS 3.14.1 — LGPL-2.1-only — in `shapely`
- GEOS 3.14.1 — LGPL-2.1-only — in `exactextract`
- libgfortran (GCC runtime) — GPL-3.0-or-later WITH GCC-exception-3.1 — in `numpy`
- libquadmath — LGPL-2.1-or-later — in `numpy`
- GEOS 3.14.1 — LGPL-2.1-only — in `rasterio`
- GEOS (statically linked into libgdal) — LGPL-2.1-only — in `pyogrio`
- libkeyutils, libcrypt — LGPL-2.1-or-later — in `psycopg-binary, psycopg2-binary`

**System components** — PostGIS and pgRouting (GPL-2.0-or-later) are
extensions loaded by the PostgreSQL server process. The Works reach them
only by sending SQL over a database connection; they are not linked into,
copied into or distributed with the Works, which reference the upstream
container image `pgrouting/pgrouting:16-3.4-3.6.1` by tag. Base layers of the
container images (Debian, Alpine) contain further GPL/LGPL programs that
run inside those containers only.

No package in either lockfile is under a strong-copyleft licence.

## Licence glossary

| Identifier | Licence | Canonical text |
|---|---|---|
| `0BSD` | BSD Zero Clause License | https://spdx.org/licenses/0BSD.html |
| `AFL-2.1` | Academic Free License v2.1 | https://spdx.org/licenses/AFL-2.1.html |
| `Apache-2.0` | Apache License 2.0 | https://spdx.org/licenses/Apache-2.0.html |
| `blessing` | SQLite Blessing | https://spdx.org/licenses/blessing.html |
| `BlueOak-1.0.0` | Blue Oak Model License 1.0.0 | https://spdx.org/licenses/BlueOak-1.0.0.html |
| `BSD-2-Clause` | BSD 2-Clause "Simplified" License | https://spdx.org/licenses/BSD-2-Clause.html |
| `BSD-3-Clause` | BSD 3-Clause "New" or "Revised" License | https://spdx.org/licenses/BSD-3-Clause.html |
| `BSD-3-Clause-Open-MPI` | BSD 3-Clause Open MPI variant | https://spdx.org/licenses/BSD-3-Clause-Open-MPI.html |
| `CC-BY-4.0` | Creative Commons Attribution 4.0 International | https://spdx.org/licenses/CC-BY-4.0.html |
| `CC0-1.0` | Creative Commons Zero v1.0 Universal | https://spdx.org/licenses/CC0-1.0.html |
| `CDLA-Permissive-2.0` | Community Data License Agreement Permissive 2.0 | https://spdx.org/licenses/CDLA-Permissive-2.0.html |
| `curl` | curl License | https://spdx.org/licenses/curl.html |
| `GCC-exception-3.1` | GCC Runtime Library exception 3.1 | https://spdx.org/licenses/GCC-exception-3.1.html |
| `GPL-2.0-only` | GNU General Public License v2.0 only | https://spdx.org/licenses/GPL-2.0-only.html |
| `GPL-2.0-or-later` | GNU General Public License v2.0 or later | https://spdx.org/licenses/GPL-2.0-or-later.html |
| `GPL-3.0-or-later` | GNU General Public License v3.0 or later | https://spdx.org/licenses/GPL-3.0-or-later.html |
| `IJG` | Independent JPEG Group License | https://spdx.org/licenses/IJG.html |
| `ISC` | ISC License | https://spdx.org/licenses/ISC.html |
| `LGPL-2.1-only` | GNU Lesser General Public License v2.1 only | https://spdx.org/licenses/LGPL-2.1-only.html |
| `LGPL-2.1-or-later` | GNU Lesser General Public License v2.1 or later | https://spdx.org/licenses/LGPL-2.1-or-later.html |
| `LGPL-3.0-only` | GNU Lesser General Public License v3.0 only | https://spdx.org/licenses/LGPL-3.0-only.html |
| `LGPL-3.0-or-later` | GNU Lesser General Public License v3.0 or later | https://spdx.org/licenses/LGPL-3.0-or-later.html |
| `libpng-2.0` | PNG Reference Library version 2 | https://spdx.org/licenses/libpng-2.0.html |
| `libtiff` | libtiff License | https://spdx.org/licenses/libtiff.html |
| `MIT` | MIT License | https://spdx.org/licenses/MIT.html |
| `MPL-2.0` | Mozilla Public License 2.0 | https://spdx.org/licenses/MPL-2.0.html |
| `ODbL-1.0` | Open Data Commons Open Database License v1.0 | https://spdx.org/licenses/ODbL-1.0.html |
| `OLDAP-2.8` | Open LDAP Public License v2.8 | https://spdx.org/licenses/OLDAP-2.8.html |
| `PCRE2-exception` | PCRE2 exception | https://spdx.org/licenses/PCRE2-exception.html |
| `PostgreSQL` | PostgreSQL License | https://spdx.org/licenses/PostgreSQL.html |
| `PSF-2.0` | Python Software Foundation License 2.0 | https://spdx.org/licenses/PSF-2.0.html |
| `Zlib` | zlib License | https://spdx.org/licenses/Zlib.html |

## Native libraries bundled in Python wheels

Read from the Linux x86_64 CPython 3.12 wheels `uv.lock` names. Package
metadata names only the package's own licence (shapely says BSD-3-Clause
while shipping GEOS), so these are listed separately. *Licence* is the
library's upstream licence; where the wheel carries the text, *Source*
says where.

| Wheel | Checked at | Library | Licence | Source |
|---|---|---|---|---|
| `shapely` | 2.2.0 | GEOS 3.14.1 | LGPL-2.1-only | wheel's LICENSE_GEOS |
| `exactextract` | 0.3.0 | GEOS 3.14.1 | LGPL-2.1-only | wheel's LICENSE_GEOS |
| `numpy` | 2.5.4 | OpenBLAS (with LAPACK) | BSD-3-Clause AND BSD-3-Clause-Open-MPI | wheel's LICENSE.txt |
| `numpy` | 2.5.4 | libgfortran (GCC runtime) | GPL-3.0-or-later WITH GCC-exception-3.1 | wheel's LICENSE.txt |
| `numpy` | 2.5.4 | libquadmath | LGPL-2.1-or-later | wheel's LICENSE.txt |
| `rasterio` | 1.5.2 | GDAL 3.12.2 | MIT | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | GEOS 3.14.1 | LGPL-2.1-only | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | PROJ 9.8.1 | MIT | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | OpenSSL 3 (libssl, libcrypto) | Apache-2.0 | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | libcurl | curl | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | nghttp2, json-c, libdeflate, giflib | MIT | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | SQLite | blessing | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | libtiff | libtiff | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | libpng | libpng-2.0 | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | libjpeg-turbo | IJG AND BSD-3-Clause AND Zlib | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | OpenJPEG, libaec (libsz) | BSD-2-Clause | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | libwebp, libsharpyuv, netCDF-C, c-blosc | BSD-3-Clause | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | zstd | BSD-3-Clause OR GPL-2.0-only | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | liblzma (XZ Utils) | 0BSD | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | PCRE2 | BSD-3-Clause WITH PCRE2-exception | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | LERC | Apache-2.0 | upstream licence; text not in the wheel |
| `rasterio` | 1.5.2 | HDF5 | HDF5 licence (BSD-style) | https://github.com/HDFGroup/hdf5/blob/develop/LICENSE |
| `pyogrio` | 0.13.0 | GDAL 3.12.4 | MIT | upstream licence; text not in the wheel |
| `pyogrio` | 0.13.0 | GEOS (statically linked into libgdal) | LGPL-2.1-only | upstream licence; text not in the wheel |
| `pyogrio` | 0.13.0 | PROJ, expat, libxml2 (static) | MIT | upstream licence; text not in the wheel |
| `pyogrio` | 0.13.0 | SQLite, libcurl, OpenSSL, libtiff, libjpeg, libpng, zlib (static) | blessing AND curl AND Apache-2.0 AND libtiff AND IJG AND libpng-2.0 AND Zlib | upstream licence; text not in the wheel |
| `pyproj` | 3.8.0 | PROJ 9.8.1 | MIT | wheel's LICENSE_proj |
| `pyproj` | 3.8.0 | libcurl, nghttp2, SQLite, libtiff | curl AND MIT AND blessing AND libtiff | upstream licence; text not in the wheel |
| `psycopg-binary` | 3.3.8 | libpq 18 | PostgreSQL | upstream licence; text not in the wheel |
| `psycopg2-binary` | 2.9.13 | libpq 17 | PostgreSQL | upstream licence; text not in the wheel |
| `psycopg-binary, psycopg2-binary` | 3.3.8 / 2.9.13 | OpenSSL 3 (libssl, libcrypto) | Apache-2.0 | upstream licence; text not in the wheel |
| `psycopg-binary, psycopg2-binary` | 3.3.8 / 2.9.13 | MIT Kerberos (libkrb5, libgssapi_krb5, libk5crypto, libkrb5support, libcom_err) | MIT Kerberos licence (permissive) | https://web.mit.edu/kerberos/krb5-latest/doc/mitK5license.html |
| `psycopg-binary, psycopg2-binary` | 3.3.8 / 2.9.13 | OpenLDAP (libldap, liblber) | OLDAP-2.8 | upstream licence; text not in the wheel |
| `psycopg-binary, psycopg2-binary` | 3.3.8 / 2.9.13 | Cyrus SASL (libsasl2) | Cyrus SASL licence (BSD-style) | https://github.com/cyrusimap/cyrus-sasl/blob/master/COPYING |
| `psycopg-binary, psycopg2-binary` | 3.3.8 / 2.9.13 | libkeyutils, libcrypt | LGPL-2.1-or-later | upstream licence; text not in the wheel |
| `psycopg-binary, psycopg2-binary` | 3.3.8 / 2.9.13 | PCRE (libpcre) | BSD-3-Clause | upstream licence; text not in the wheel |
| `psycopg-binary, psycopg2-binary` | 3.3.8 / 2.9.13 | libselinux | public domain | https://github.com/SELinuxProject/selinux/blob/main/libselinux/LICENSE |
| `pyarrow` | 26.0.1 | Arrow C++ third-party code, statically linked (OpenSSL, libcurl, zlib, zstd, lz4, snappy, brotli, jemalloc, mimalloc, protobuf, abseil, gRPC, AWS and Google Cloud SDKs, xsimd, utf8proc) | permissive; texts in the wheel's LICENSE.txt and NOTICE.txt | upstream licence; text not in the wheel |
| `uvloop` | 0.23.0 | libuv (statically linked) | MIT | upstream licence; text not in the wheel |

## System components

Required at run time but not installed by pip or npm. Versions of the
containerised services are the image tags in
`infra/docker/docker-compose.yml`.

| Component | Version | Licence | Role |
|---|---|---|---|
| PostgreSQL | 16 (`pgrouting/pgrouting:16-3.4-3.6.1`) | PostgreSQL | database server |
| PostGIS | 3.4 (`pgrouting/pgrouting:16-3.4-3.6.1`) | GPL-2.0-or-later | spatial extension inside the database server |
| pgRouting | 3.6.1 (`pgrouting/pgrouting:16-3.4-3.6.1`) | GPL-2.0-or-later | routing extension inside the database server (`pgr_drivingDistance`, migration r037) |
| PgBouncer | 1.26.1-p0 (`edoburu/pgbouncer:v1.26.1-p0`) | ISC | connection pooler, separate process |
| Martin | 0.14.0 (`ghcr.io/maplibre/martin:v0.14.0`) | MIT OR Apache-2.0 | vector tile server, separate process |
| nginx | 1.27-alpine (`nginx:1.27-alpine`) | BSD-2-Clause | static file server for PMTiles snapshots (development stack) |
| GDAL command-line tools (`ogr2ogr`) | >= 3.8 | MIT | external program, used only by PMTiles export |
| CPython | 3.12 | PSF-2.0 | interpreter running the server code |
| Node.js | 24 (LTS) | MIT | build only: compiles the web application |

## Data sources (data, not code)

Data the platform's pipelines download, load and display. None of it is
stored in the repository. The licence each loaded source carries is
recorded in `curated_geo.data_source_catalog` and credited on the map
(`GET /tile-sources/attribution`). For all three Overture themes the
catalogue records the one string "ODbL-1.0 / Overture terms"; the
per-theme terms below are Overture's own.

| Source | Used for | Licence / terms | Attribution |
|---|---|---|---|
| OpenStreetMap (via osmnx / Overpass) | street network for accessibility indicators | ODbL-1.0 | "© OpenStreetMap contributors", shown on the map by `reimagina.data.attribution`; ODbL also requires a publicly used or distributed derivative database to be offered under ODbL |
| Overture Maps — `buildings` theme | building footprints | ODbL-1.0 (some sources CC-BY-4.0, per Overture's attribution page) | "© OpenStreetMap contributors. Available under the Open Database License." |
| Overture Maps — `divisions` theme | municipal boundary (area of interest) | ODbL-1.0 (geoBoundaries and other sources CC-BY-4.0) | as for buildings |
| Overture Maps — `places` theme | park, recreation and cultural destinations | per source: CDLA-Permissive-2.0 (most), Apache-2.0 (Foursquare), CC0-1.0 (AllThePlaces) | per source, e.g. "Data from Meta. Available under CDLA Permissive 2.0." |
| INEGI — Marco Geoestadístico, Censo de Población 2020, DENUE | municipalities, AGEBs, blocks, census attributes; DENUE within the amenity layers | Términos de Libre Uso de la Información del INEGI (https://www.inegi.org.mx/inegi/terminos.html) | credit INEGI and cite the product ("Fuente: INEGI, <producto>"); commercial use and adaptation permitted |
| Gobierno de la Ciudad de México — Colonias (datos.cdmx.gob.mx) | neighbourhood boundaries | terms published on the CDMX open-data portal (recorded in code as "Licencia CDMX Open Data") | provider and licence, shown on the map |
| SIG-CDMX equipamientos (with DENUE) | Tlalpan amenities | CDMX open-data terms (recorded as "Datos abiertos de la CDMX") | provider and licence, shown on the map |
| IMIP Mexicali (with DENUE) | Mexicali amenities | IMIP open data (recorded as "Datos abiertos del IMIP") | provider and licence, shown on the map |
| WorldPop (`raster fetch-worldpop`) | gridded population rasters | CC-BY-4.0 (datasets derived from OSM or Microsoft footprints/roads: ODbL-1.0) | credit WorldPop; the licence is recorded per raster at `raster ingest --license` |

## Python packages

| Package | Version | Licence | Declared as | Distribution | Note |
|---|---|---|---|---|---|
| `affine` | 3.0.1 | BSD-3-Clause |  | runtime (server) |  |
| `alembic` | 1.20.1 | MIT |  | runtime (server) |  |
| `annotated-doc` | 0.0.5 | MIT |  | runtime (server) |  |
| `annotated-types` | 0.8.0 | MIT |  | runtime (server) |  |
| `anyio` | 4.16.0 | MIT |  | runtime (server) |  |
| `apache-hamilton` | 1.90.0 | Apache-2.0 |  | runtime (server) |  |
| `ast-serialize` | 0.13.1 | MIT |  | dev/test only |  |
| `attrs` | 26.1.0 | MIT |  | runtime (server) |  |
| `cachetools` | 8.0.0 | MIT |  | runtime (server) |  |
| `certifi` | 2026.10.23 | MPL-2.0 | Mozilla Public License 2.0 (MPL 2.0) | runtime (server) | ships Mozilla's CA bundle as a data file, used unmodified for TLS verification |
| `charset-normalizer` | 3.5.2 | MIT |  | runtime (server) |  |
| `click` | 8.5.1 | BSD-3-Clause |  | runtime (server) |  |
| `color-operations` | 0.4.0 | MIT |  | runtime (server) |  |
| `coverage` | 7.17.2 | Apache-2.0 |  | dev/test only |  |
| `croniter` | 6.2.4 | MIT |  | runtime (server) |  |
| `dbos` | 3.5.0 | MIT | MIT License | runtime (server) |  |
| `exactextract` | 0.3.0 | Apache-2.0 | Apache License Version 2.0, January 2004 http://www.apache.org/licenses/ TERM... | runtime (server) |  |
| `fastapi` | 0.162.5 | MIT |  | runtime (server) |  |
| `geojson-pydantic` | 2.2.0 | MIT | MIT License | runtime (server) |  |
| `geopandas` | 1.2.0 | BSD-3-Clause |  | runtime (server) |  |
| `greenlet` | 3.5.6 | MIT AND PSF-2.0 |  | runtime (server) |  |
| `h11` | 0.16.0 | MIT | MIT License | runtime (server) |  |
| `httpcore` | 1.0.9 | BSD-3-Clause |  | dev/test only |  |
| `httpcore2` | 2.16.0 | BSD-3-Clause |  | runtime (server) |  |
| `httptools` | 0.9.0 | MIT |  | runtime (server) |  |
| `httpx` | 0.28.1 | BSD-3-Clause | BSD License | dev/test only |  |
| `httpx2` | 2.16.0 | BSD-3-Clause |  | runtime (server) |  |
| `idna` | 3.20 | BSD-3-Clause |  | runtime (server) |  |
| `iniconfig` | 2.3.1 | MIT |  | dev/test only |  |
| `jinja2` | 3.1.6 | BSD-3-Clause | BSD License | runtime (server) |  |
| `librt` | 0.16.0 | MIT |  | dev/test only |  |
| `mako` | 1.4.3 | MIT |  | runtime (server) |  |
| `mapbox-vector-tile` | 2.2.0 | MIT | MIT License | dev/test only |  |
| `markdown-it-py` | 4.2.0 | MIT | MIT License | runtime (server) |  |
| `markupsafe` | 3.0.4 | BSD-3-Clause |  | runtime (server) |  |
| `mdurl` | 0.1.2 | MIT | MIT License | runtime (server) |  |
| `morecantile` | 7.1.0 | MIT | MIT License | runtime (server) |  |
| `mypy` | 2.4.1 | MIT |  | dev/test only |  |
| `mypy-extensions` | 1.2.0 | MIT |  | runtime (server) |  |
| `networkx` | 3.7 | BSD-3-Clause |  | runtime (server) |  |
| `numexpr` | 2.14.2 | MIT |  | runtime (server) |  |
| `numpy` | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |  | runtime (server) |  |
| `openlineage-python` | 1.55.0 | Apache-2.0 |  | runtime (server) |  |
| `opentelemetry-api` | 1.45.2 | Apache-2.0 |  | runtime (server) |  |
| `osmnx` | 2.1.1 | MIT |  | runtime (server) |  |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause |  | runtime (server) |  |
| `pandas` | 3.1.1 | BSD-3-Clause |  | runtime (server) |  |
| `pandera` | 0.36.0 | MIT | MIT License | runtime (server) |  |
| `pathspec` | 1.1.1 | MPL-2.0 | Mozilla Public License 2.0 (MPL 2.0) | dev/test only | imported only by mypy (type checker) |
| `pluggy` | 1.7.0 | MIT | MIT License | dev/test only |  |
| `protobuf` | 6.33.6 | BSD-3-Clause | 3-Clause BSD License | dev/test only |  |
| `psycopg` | 3.3.8 | LGPL-3.0-only |  | runtime (server) | PostgreSQL driver imported by the DBOS library at run time; used unmodified, not linked into project code |
| `psycopg-binary` | 3.3.8 | LGPL-3.0-only |  | runtime (server) | binary build of psycopg (bundles libpq; see Native libraries) |
| `psycopg2-binary` | 2.9.13 | LGPL-3.0-or-later | GNU Library or Lesser General Public License (LGPL) | runtime (server) | PostgreSQL driver imported by the API, storage and orchestration code; used unmodified (bundles libpq; see Native libraries) |
| `pyarrow` | 26.0.1 | Apache-2.0 |  | runtime (server) |  |
| `pyclipper` | 1.4.0 | MIT | MIT License | dev/test only |  |
| `pydantic` | 2.14.3 | MIT |  | runtime (server) |  |
| `pydantic-core` | 2.50.4 | MIT |  | runtime (server) |  |
| `pygments` | 2.21.0 | BSD-2-Clause |  | runtime (server) |  |
| `pyogrio` | 0.13.0 | MIT | MIT License | runtime (server) |  |
| `pyparsing` | 3.3.3 | MIT |  | runtime (server) |  |
| `pyproj` | 3.8.0 | MIT |  | runtime (server) |  |
| `pystac` | 1.16.0 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-core` | 1.16.0 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-classification` | 2.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-datacube` | 2.2.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-eo` | 1.1.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-file` | 2.1.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-grid` | 1.1.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-item-assets` | 1.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-label` | 1.0.3 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-mgrs` | 1.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-mlm` | 1.4.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-pointcloud` | 1.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-projection` | 2.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-raster` | 1.1.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-render` | 2.0.1 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-sar` | 1.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-sat` | 1.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-scientific` | 1.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-storage` | 2.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-table` | 1.2.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-timestamps` | 1.1.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-version` | 1.2.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-view` | 1.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pystac-ext-xarray-assets` | 1.0.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `pytest` | 9.2.0 | MIT |  | dev/test only |  |
| `pytest-cov` | 7.1.0 | MIT |  | dev/test only |  |
| `python-dateutil` | 4.0.0 | BSD-3-Clause |  | runtime (server) |  |
| `python-dotenv` | 1.2.4 | BSD-3-Clause |  | runtime (server) |  |
| `pyyaml` | 6.0.3 | MIT | MIT License | runtime (server) |  |
| `rasterio` | 1.5.2 | BSD-3-Clause |  | runtime (server) |  |
| `requests` | 2.34.2 | Apache-2.0 | Apache Software License | runtime (server) |  |
| `rich` | 15.0.0 | MIT | MIT License | runtime (server) |  |
| `rio-tiler` | 9.4.13 | BSD-3-Clause | BSD License | runtime (server) |  |
| `ruff` | 0.17.5 | MIT |  | dev/test only |  |
| `sf-hamilton` | 1.90.0 | Apache-2.0 |  | runtime (server) |  |
| `shapely` | 2.2.0 | BSD-3-Clause |  | runtime (server) |  |
| `shellingham` | 1.5.4 | ISC | ISC License (ISCL) | runtime (server) |  |
| `simplejson` | 4.2.0 | MIT OR AFL-2.1 |  | runtime (server) |  |
| `sqlalchemy` | 2.1.8 | MIT |  | runtime (server) |  |
| `starlette` | 1.10.0 | BSD-3-Clause |  | runtime (server) |  |
| `titiler-core` | 2.4.0 | MIT | MIT License | runtime (server) |  |
| `truststore` | 0.10.5 | MIT |  | runtime (server) |  |
| `typeguard` | 4.6.0 | MIT |  | runtime (server) |  |
| `typer` | 0.29.1 | MIT |  | runtime (server) |  |
| `typing-extensions` | 4.16.0 | PSF-2.0 |  | runtime (server) |  |
| `typing-inspect` | 0.9.0 | MIT | MIT License | runtime (server) |  |
| `typing-inspection` | 0.4.5 | MIT |  | runtime (server) |  |
| `tzdata` | 2026.6 | Apache-2.0 |  | runtime (server) |  |
| `urllib3` | 2.8.0 | MIT |  | runtime (server) |  |
| `uvicorn` | 0.54.0 | BSD-3-Clause |  | runtime (server) |  |
| `uvloop` | 0.23.0 | MIT OR Apache-2.0 | Apache Software License; MIT License | runtime (server) |  |
| `watchfiles` | 1.3.0 | MIT | MIT License | runtime (server) |  |
| `websockets` | 17.4 | BSD-3-Clause |  | runtime (server) |  |

## npm packages

| Package | Version | Licence | Declared as | Distribution | Note |
|---|---|---|---|---|---|
| `@emnapi/core` | 1.11.3 | MIT |  | build/test only |  |
| `@emnapi/runtime` | 1.11.3 | MIT |  | build/test only |  |
| `@emnapi/wasi-threads` | 1.2.3 | MIT |  | build/test only |  |
| `@exodus/bytes` | 1.16.0 | MIT |  | build/test only |  |
| `@floating-ui/core` | 1.8.0 | MIT |  | runtime (browser) |  |
| `@floating-ui/dom` | 1.8.0 | MIT |  | runtime (browser) |  |
| `@floating-ui/utils` | 0.2.12 | MIT |  | runtime (browser) |  |
| `@internationalized/date` | 3.12.3 | Apache-2.0 |  | runtime (browser) |  |
| `@jridgewell/gen-mapping` | 0.3.13 | MIT |  | runtime (browser) |  |
| `@jridgewell/remapping` | 2.3.5 | MIT |  | runtime (browser) |  |
| `@jridgewell/resolve-uri` | 3.1.2 | MIT |  | runtime (browser) |  |
| `@jridgewell/sourcemap-codec` | 1.6.0 | MIT |  | runtime (browser) |  |
| `@jridgewell/trace-mapping` | 0.3.31 | MIT |  | runtime (browser) |  |
| `@lucide/svelte` | 1.72.0 | ISC |  | runtime (browser) |  |
| `@mapbox/jsonlint-lines-primitives` | 2.0.3 | MIT |  | runtime (browser) |  |
| `@mapbox/point-geometry` | 1.1.0 | ISC |  | runtime (browser) |  |
| `@mapbox/tiny-sdf` | 2.3.0 | BSD-2-Clause |  | runtime (browser) |  |
| `@mapbox/unitbezier` | 1.0.0 | BSD-2-Clause |  | runtime (browser) |  |
| `@mapbox/vector-tile` | 3.0.0 | BSD-3-Clause |  | runtime (browser) |  |
| `@maplibre/geojson-vt` | 6.3.0 | ISC |  | runtime (browser) |  |
| `@maplibre/maplibre-gl-style-spec` | 26.6.0 | ISC |  | runtime (browser) |  |
| `@maplibre/mlt` | 1.3.0 | MIT OR Apache-2.0 |  | runtime (browser) |  |
| `@maplibre/vt-pbf` | 4.3.3 | MIT |  | runtime (browser) |  |
| `@napi-rs/wasm-runtime` | 1.2.2 | MIT |  | build/test only |  |
| `@oxc-project/types` | 0.156.0 | MIT |  | build/test only |  |
| `@rolldown/binding-linux-x64-gnu` | 1.2.19 | MIT |  | build/test only |  |
| `@rolldown/pluginutils` | 1.1.0 | MIT |  | build/test only |  |
| `@sveltejs/acorn-typescript` | 1.0.13 | MIT |  | runtime (browser) |  |
| `@sveltejs/load-config` | 0.2.3 | MIT |  | build/test only |  |
| `@sveltejs/vite-plugin-svelte` | 7.3.1 | MIT |  | build/test only |  |
| `@swc/helpers` | 0.5.23 | Apache-2.0 |  | runtime (browser) |  |
| `@tailwindcss/node` | 4.3.5 | MIT |  | build/test only |  |
| `@tailwindcss/oxide` | 4.3.5 | MIT |  | build/test only |  |
| `@tailwindcss/oxide-linux-x64-gnu` | 4.3.5 | MIT |  | build/test only |  |
| `@tailwindcss/oxide-linux-x64-musl` | 4.3.5 | MIT |  | build/test only |  |
| `@tailwindcss/oxide-wasm32-wasi` | 4.3.5 | MIT |  | build/test only |  |
| `@tailwindcss/vite` | 4.3.5 | MIT |  | build/test only |  |
| `@tybys/wasm-util` | 0.10.3 | MIT |  | build/test only |  |
| `@types/chai` | 5.2.3 | MIT |  | build/test only |  |
| `@types/deep-eql` | 4.0.2 | MIT |  | build/test only |  |
| `@types/estree` | 1.0.10 | MIT |  | runtime (browser) |  |
| `@types/geojson` | 7946.0.16 | MIT |  | runtime (browser) |  |
| `@vitest/mocker` | 5.1.0 | MIT |  | build/test only |  |
| `@vitest/spy` | 5.1.0 | MIT |  | build/test only |  |
| `acorn` | 8.18.0 | MIT |  | runtime (browser) |  |
| `aria-query` | 5.3.1 | Apache-2.0 |  | runtime (browser) |  |
| `assertion-error` | 2.0.1 | MIT |  | build/test only |  |
| `axobject-query` | 4.1.0 | Apache-2.0 |  | runtime (browser) |  |
| `bcp-47` | 2.1.2 | MIT |  | build/test only |  |
| `bcp-47-match` | 2.0.3 | MIT |  | build/test only |  |
| `bidi-js` | 1.1.0 | MIT |  | runtime (browser) |  |
| `bits-ui` | 2.21.0 | MIT |  | runtime (browser) |  |
| `chai` | 6.3.0 | MIT |  | build/test only |  |
| `chokidar` | 4.0.3 | MIT |  | build/test only |  |
| `clsx` | 2.1.1 | MIT |  | runtime (browser) |  |
| `data-urls` | 8.0.0 | MIT |  | build/test only |  |
| `decimal.js` | 10.6.0 | MIT |  | build/test only |  |
| `deepmerge` | 4.3.1 | MIT |  | build/test only |  |
| `dequal` | 2.0.3 | MIT |  | runtime (browser) |  |
| `detect-libc` | 2.1.2 | Apache-2.0 |  | build/test only |  |
| `devalue` | 5.9.4 | MIT |  | runtime (browser) |  |
| `earcut` | 3.2.4 | ISC |  | runtime (browser) |  |
| `enhanced-resolve` | 5.26.1 | MIT |  | build/test only |  |
| `entities` | 9.0.0 | BSD-2-Clause |  | build/test only |  |
| `es-module-lexer` | 2.3.2 | MIT |  | build/test only |  |
| `esm-env` | 1.2.2 | MIT |  | runtime (browser) |  |
| `esrap` | 2.4.0 | MIT |  | runtime (browser) |  |
| `estree-walker` | 3.0.3 | MIT |  | build/test only |  |
| `expect-type` | 1.4.0 | Apache-2.0 |  | build/test only |  |
| `fdir` | 6.5.0 | MIT |  | build/test only |  |
| `gl-matrix` | 3.4.4 | MIT |  | runtime (browser) |  |
| `graceful-fs` | 4.2.11 | ISC |  | build/test only |  |
| `html-encoding-sniffer` | 7.0.0 | MIT |  | build/test only |  |
| `inline-style-parser` | 0.2.7 | MIT |  | runtime (browser) |  |
| `is-alphabetical` | 2.0.1 | MIT |  | build/test only |  |
| `is-alphanumerical` | 2.0.1 | MIT |  | build/test only |  |
| `is-decimal` | 2.0.1 | MIT |  | build/test only |  |
| `is-potential-custom-element-name` | 1.0.1 | MIT |  | build/test only |  |
| `is-reference` | 3.0.3 | MIT |  | runtime (browser) |  |
| `jiti` | 2.7.0 | MIT |  | build/test only |  |
| `jsdom` | 31.0.0 | MIT |  | build/test only |  |
| `json-stringify-pretty-compact` | 4.0.0 | MIT |  | runtime (browser) |  |
| `kdbush` | 4.1.0 | ISC |  | runtime (browser) |  |
| `lightningcss` | 1.33.0 | MPL-2.0 |  | build/test only | CSS transformer run by the Tailwind CSS build; its output CSS is not covered by its licence |
| `lightningcss-linux-x64-gnu` | 1.33.0 | MPL-2.0 |  | build/test only | native binary of lightningcss, build only |
| `lightningcss-linux-x64-musl` | 1.33.0 | MPL-2.0 |  | build/test only | native binary of lightningcss, build only |
| `locate-character` | 3.0.0 | MIT |  | runtime (browser) |  |
| `lru-cache` | 11.5.3 | BlueOak-1.0.0 |  | build/test only |  |
| `lz-string` | 1.5.0 | MIT |  | runtime (browser) |  |
| `magic-string` | 0.30.21 | MIT |  | runtime (browser) |  |
| `magic-string` | 1.2.0 | MIT |  | build/test only |  |
| `magic-string` | 1.4.5 | MIT |  | build/test only |  |
| `maplibre-gl` | 6.18.0 | BSD-3-Clause |  | runtime (browser) | its distributed file pre-bundles its own dependencies |
| `minimist` | 1.2.8 | MIT |  | runtime (browser) |  |
| `mri` | 1.2.0 | MIT |  | build/test only |  |
| `murmurhash-js` | 1.0.0 | MIT |  | runtime (browser) |  |
| `nanoid` | 3.3.20 | MIT |  | build/test only |  |
| `obug` | 2.1.4 | MIT |  | build/test only |  |
| `obug` | 3.0.0 | MIT |  | build/test only |  |
| `parse5` | 8.1.1 | MIT |  | build/test only |  |
| `parse5-parser-stream` | 8.1.1 | MIT |  | build/test only |  |
| `pbf` | 5.1.2 | BSD-3-Clause |  | runtime (browser) |  |
| `picocolors` | 1.1.1 | ISC |  | build/test only |  |
| `picomatch` | 4.0.7 | MIT |  | build/test only |  |
| `postcss` | 8.5.32 | MIT |  | build/test only |  |
| `potpack` | 2.1.0 | ISC |  | runtime (browser) |  |
| `protocol-buffers-schema` | 3.6.1 | MIT |  | runtime (browser) |  |
| `punycode` | 2.3.1 | MIT |  | build/test only |  |
| `quickselect` | 3.0.0 | ISC |  | runtime (browser) |  |
| `readdirp` | 4.1.2 | MIT |  | build/test only |  |
| `require-from-string` | 2.0.2 | MIT |  | runtime (browser) |  |
| `resolve-protobuf-schema` | 2.1.0 | MIT |  | runtime (browser) |  |
| `rolldown` | 1.2.19 | MIT |  | build/test only |  |
| `runed` | 0.35.1 | MIT |  | runtime (browser) |  |
| `sade` | 1.8.1 | MIT |  | build/test only |  |
| `saxes` | 6.0.0 | ISC |  | build/test only |  |
| `source-map-js` | 1.2.2 | BSD-3-Clause |  | build/test only |  |
| `std-env` | 4.3.0 | MIT |  | build/test only |  |
| `style-to-object` | 1.0.14 | MIT |  | runtime (browser) |  |
| `svelte` | 5.57.3 | MIT |  | runtime (browser) | compiled components embed the svelte runtime in the browser bundle |
| `svelte-check` | 4.7.8 | MIT |  | build/test only |  |
| `svelte-toolbelt` | 0.10.7 | MIT | (none) | runtime (browser) |  |
| `tabbable` | 6.5.1 | MIT |  | runtime (browser) |  |
| `tailwind-merge` | 3.7.0 | MIT |  | runtime (browser) |  |
| `tailwind-variants` | 4.0.2 | MIT |  | runtime (browser) |  |
| `tailwindcss` | 4.3.5 | MIT |  | build/test only |  |
| `tapable` | 2.3.3 | MIT |  | build/test only |  |
| `terra-draw` | 1.39.0 | MIT |  | runtime (browser) |  |
| `terra-draw-maplibre-gl-adapter` | 1.5.0 | MIT |  | runtime (browser) |  |
| `tinybench` | 6.2.3 | MIT |  | build/test only |  |
| `tinyexec` | 1.3.1 | MIT |  | build/test only |  |
| `tinyglobby` | 0.2.17 | MIT |  | build/test only |  |
| `tinyqueue` | 3.0.0 | ISC |  | runtime (browser) |  |
| `tldts` | 7.4.10 | MIT |  | build/test only |  |
| `tldts-core` | 7.4.10 | MIT |  | build/test only |  |
| `tough-cookie` | 6.0.2 | BSD-3-Clause |  | build/test only |  |
| `tr46` | 7.0.0 | MIT |  | build/test only |  |
| `tslib` | 2.8.1 | 0BSD |  | runtime (browser) |  |
| `typescript` | 5.9.3 | Apache-2.0 |  | build/test only |  |
| `undici` | 8.12.4 | MIT |  | build/test only |  |
| `vite` | 8.3.8 | MIT |  | build/test only |  |
| `vitefu` | 1.1.3 | MIT |  | build/test only |  |
| `vitest` | 5.1.0 | MIT |  | build/test only |  |
| `w3c-xmlserializer` | 6.0.0 | MIT |  | build/test only |  |
| `webidl-conversions` | 8.0.1 | BSD-2-Clause |  | build/test only |  |
| `whatwg-mimetype` | 5.0.0 | MIT |  | build/test only |  |
| `whatwg-url` | 17.3.0 | MIT |  | build/test only |  |
| `why-is-node-running` | 3.2.1 | MIT |  | build/test only |  |
| `xml-name-validator` | 5.0.0 | Apache-2.0 |  | build/test only |  |
| `xmlchars` | 2.2.0 | MIT |  | build/test only |  |
| `zimmerframe` | 1.1.4 | MIT |  | runtime (browser) |  |
