"""Generate the third-party dependency and licence inventory.

A delivery of this platform has to be accompanied by a complete list of the
third-party and open-source components it incorporates, with each item's
licence terms, so that whoever receives it can see what they are taking on.
This script produces that list as ``docs/THIRD_PARTY_LICENSES.md``.

It is a script rather than a hand-written document on purpose, and the reason
is in this repository's own history: ``overridable_attributes()`` says a
hand-maintained list "is how ``poi_version`` rotted — it named an input nothing
produced and nothing validated, for six migrations". A licence inventory rots
the same way and the rot is less visible, because nothing fails when it goes
stale. Regenerate it; do not edit the output. A contract test runs
``--check``, so a lockfile change without a regenerated inventory fails.

What it reads, and why the output is the same on every machine:

- **Which packages, at which versions** — the lockfiles, never the local
  install. ``uv.lock`` is resolved for the delivered platform (Linux x86_64,
  CPython 3.12, glibc — what CI runs) by evaluating each dependency's
  environment marker, walking from the project's own ``dependencies``
  (runtime) and its ``dev`` extra (dev/test only). ``apps/web/package-lock.json``
  is walked the way ``npm ci`` would on that platform: optional packages whose
  ``os``/``cpu``/``libc`` exclude Linux x64 glibc are dropped with everything
  only they pull in, and the lockfile's ``dev`` flag separates build/test
  packages from the runtime tree.
- **Python licence metadata** — read from the installed distributions, because
  ``uv.lock`` does not carry it. Each one is matched to the lockfile by name
  *and version*, and a mismatch is an error, so an unsynced environment cannot
  silently produce a different document. The metadata of a release is the
  same on every platform.
- **npm licence metadata** — the ``license`` field the lockfile records.
- **System components** — image tags from ``infra/docker/docker-compose.yml``.

Licence strings are normalised to SPDX identifiers. Where a manifest is vague
(a "BSD License" classifier names no variant) the identifier was read off the
package's own licence file and is recorded in ``PACKAGE_LICENCE`` with that
evidence; the raw declaration is kept in the table. An identifier that is not
in ``GLOSSARY`` stops the script — it never guesses, because "probably MIT" is
not a licence statement.

Some findings cannot be read off any manifest — the native libraries a wheel
vendors, the licences of system services and of the data the platform loads.
They are recorded below as data with the evidence for each, and printed with
the package version they were checked at, so a later version bump shows
plainly where a re-check is due.

Usage::

    uv run python scripts/third_party_licences.py            # writes the document
    uv run python scripts/third_party_licences.py --check    # exit 1 if out of date
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]
UV_LOCK = ROOT / "uv.lock"
NPM_LOCK = ROOT / "apps" / "web" / "package-lock.json"
COMPOSE = ROOT / "infra" / "docker" / "docker-compose.yml"
OUTPUT = ROOT / "docs" / "THIRD_PARTY_LICENSES.md"

#: The delivered platform: CI's runner. Markers are evaluated against this,
#: not against whatever machine runs the script.
TARGET_ENV = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.0",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.12.0",
    "python_version": "3.12",
    "sys_platform": "linux",
}
NPM_OS, NPM_CPU, NPM_LIBC = "linux", "x64", "glibc"

SPDX = "https://spdx.org/licenses/{}.html"

#: Every licence identifier the document may use, with its canonical text.
GLOSSARY: dict[str, tuple[str, str]] = {
    i: (n, SPDX.format(i))
    for i, n in (
        ("0BSD", "BSD Zero Clause License"),
        ("AFL-2.1", "Academic Free License v2.1"),
        ("Apache-2.0", "Apache License 2.0"),
        ("BlueOak-1.0.0", "Blue Oak Model License 1.0.0"),
        ("BSD-2-Clause", 'BSD 2-Clause "Simplified" License'),
        ("BSD-3-Clause", 'BSD 3-Clause "New" or "Revised" License'),
        ("BSD-3-Clause-Open-MPI", "BSD 3-Clause Open MPI variant"),
        ("CC-BY-4.0", "Creative Commons Attribution 4.0 International"),
        ("CC0-1.0", "Creative Commons Zero v1.0 Universal"),
        ("CDLA-Permissive-2.0", "Community Data License Agreement Permissive 2.0"),
        ("GCC-exception-3.1", "GCC Runtime Library exception 3.1"),
        ("GPL-2.0-only", "GNU General Public License v2.0 only"),
        ("GPL-2.0-or-later", "GNU General Public License v2.0 or later"),
        ("GPL-3.0-or-later", "GNU General Public License v3.0 or later"),
        ("IJG", "Independent JPEG Group License"),
        ("ISC", "ISC License"),
        ("LGPL-2.1-only", "GNU Lesser General Public License v2.1 only"),
        ("LGPL-2.1-or-later", "GNU Lesser General Public License v2.1 or later"),
        ("LGPL-3.0-only", "GNU Lesser General Public License v3.0 only"),
        ("LGPL-3.0-or-later", "GNU Lesser General Public License v3.0 or later"),
        ("MIT", "MIT License"),
        ("MPL-2.0", "Mozilla Public License 2.0"),
        ("ODbL-1.0", "Open Data Commons Open Database License v1.0"),
        ("OLDAP-2.8", "Open LDAP Public License v2.8"),
        ("PCRE2-exception", "PCRE2 exception"),
        ("PostgreSQL", "PostgreSQL License"),
        ("PSF-2.0", "Python Software Foundation License 2.0"),
        ("Zlib", "zlib License"),
        ("blessing", "SQLite Blessing"),
        ("curl", "curl License"),
        ("libpng-2.0", "PNG Reference Library version 2"),
        ("libtiff", "libtiff License"),
    )
}

#: Trove classifiers that name exactly one SPDX licence. "BSD License",
#: "Apache Software License" and the LGPL classifier name a family, not a
#: licence, and are deliberately absent: those packages need an entry in
#: `PACKAGE_LICENCE`, read off their licence file.
CLASSIFIER_SPDX = {
    "MIT License": "MIT",
    "ISC License (ISCL)": "ISC",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}

#: Identifiers established by reading the package's own licence file, where
#: its metadata is a family name, free text, or absent. Value: (SPDX, evidence).
_PYSTAC = ("Apache-2.0", "classifier; pystac repository licence is Apache-2.0")
PACKAGE_LICENCE: dict[str, tuple[str, str]] = {
    "exactextract": ("Apache-2.0", "metadata holds the full Apache-2.0 text"),
    "httpx": ("BSD-3-Clause", "LICENSE.md"),
    "jinja2": ("BSD-3-Clause", "LICENSE.txt"),
    "protobuf": ("BSD-3-Clause", "LICENSE"),
    "psycopg2-binary": (
        "LGPL-3.0-or-later",
        "LICENSE: LGPL v3 or later, plus an exception permitting linking with OpenSSL",
    ),
    "pyclipper": ("MIT", "classifier"),
    "requests": ("Apache-2.0", "LICENSE"),
    "rio-tiler": ("BSD-3-Clause", "LICENSE"),
    "svelte-toolbelt": ("MIT", "LICENSE; manifest declares none"),
    "uvloop": ("MIT OR Apache-2.0", "LICENSE-MIT and LICENSE-APACHE"),
    "pystac": ("Apache-2.0", "LICENSE"),
    "pystac-core": _PYSTAC,
}

#: Per-package facts a reader needs next to the licence. Every copyleft
#: package must have one (the script stops otherwise): a copyleft licence
#: without a statement of how the component is used is half a disclosure.
NOTES: dict[str, str] = {
    "certifi": "ships Mozilla's CA bundle as a data file, used unmodified for TLS verification",
    "pathspec": "imported only by mypy (type checker)",
    "psycopg": "PostgreSQL driver imported by the DBOS library at run time; used unmodified, not linked into project code",
    "psycopg-binary": "binary build of psycopg (bundles libpq; see Native libraries)",
    "psycopg2-binary": "PostgreSQL driver imported by the API, storage and orchestration code; used unmodified (bundles libpq; see Native libraries)",
    "lightningcss": "CSS transformer run by the Tailwind CSS build; its output CSS is not covered by its licence",
    "lightningcss-linux-x64-gnu": "native binary of lightningcss, build only",
    "lightningcss-linux-x64-musl": "native binary of lightningcss, build only",
    "svelte": "compiled components embed the svelte runtime in the browser bundle",
    "maplibre-gl": "its distributed file pre-bundles its own dependencies",
}

STRONG = "strong copyleft"
WEAK = "weak copyleft"
PERM = "permissive"

WEAK_COPYLEFT = re.compile(r"\bLGPL|\bMPL-|\bEPL-|\bCDDL", re.I)
STRONG_COPYLEFT = re.compile(r"\bAGPL|\bGPL-|\bSSPL|\bOSL-|\bEUPL", re.I)


def classify(spdx: str) -> str:
    """Bucket a normalised licence expression.

    Weak copyleft is tested first: ``\\bGPL-`` cannot match inside "LGPL-"
    (no word boundary), but the order makes that independent of the regex.
    An ``OR`` expression offering a permissive choice is permissive.
    """
    if " OR " in spdx and any(
        classify(alt) == PERM for alt in spdx.strip("()").split(" OR ")
    ):
        return PERM
    if WEAK_COPYLEFT.search(spdx):
        return WEAK
    if STRONG_COPYLEFT.search(spdx):
        return STRONG
    return PERM


def spdx_ids(expression: str) -> list[str]:
    return [
        t
        for t in re.split(r"[\s()]+", expression)
        if t and t not in ("AND", "OR", "WITH")
    ]


def _one_line(raw: str, limit: int = 77) -> str:
    """Collapse whitespace, then truncate — so newlines never eat the budget.

    ``License:`` in Python core metadata is free text, and some distributions
    (``exactextract`` 0.3.0) put the entire licence document in it.
    """
    collapsed = " ".join(raw.split())
    if len(collapsed) <= limit + 3:
        return collapsed
    return collapsed[:limit].rstrip() + "..."


def _cell(value: str) -> str:
    """One table cell: one line, and a ``|`` that cannot open a column."""
    return _one_line(value, limit=120).replace("|", "\\|")


@dataclass(frozen=True)
class Entry:
    ecosystem: str
    name: str
    version: str
    licence: str  # normalised SPDX expression
    declared: str  # raw declaration, when it differs from `licence`
    runtime: bool

    @property
    def bucket(self) -> str:
        return classify(self.licence)

    @property
    def distribution(self) -> str:
        if self.ecosystem == "python":
            return "runtime (server)" if self.runtime else "dev/test only"
        return "runtime (browser)" if self.runtime else "build/test only"


def _normalise(name: str, declared: str, ecosystem: str) -> tuple[str, str]:
    """(SPDX expression, raw declaration if different). Raises on the unknown."""
    key = name.lower()
    if key in PACKAGE_LICENCE:
        spdx = PACKAGE_LICENCE[key][0]
    elif key.startswith("pystac-ext-"):
        spdx = _PYSTAC[0]
    else:
        spdx = CLASSIFIER_SPDX.get(declared, declared).strip()
        if spdx.startswith("(") and spdx.endswith(")"):
            declared = spdx = spdx[1:-1]
    unknown = [i for i in spdx_ids(spdx) if i not in GLOSSARY]
    if not spdx or unknown:
        raise SystemExit(
            f"{ecosystem} package {name!r} declares {declared!r}, which is not a "
            "known SPDX expression. Read its licence file and add it to "
            "PACKAGE_LICENCE (or the identifier to GLOSSARY)."
        )
    return spdx, ("" if declared == spdx else _one_line(declared) or "(none)")


# ── Python ──────────────────────────────────────────────────────────────────


def _uv_closure(lock: dict[str, Any], roots: list[dict[str, Any]]) -> dict[str, str]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pkg in lock["package"]:
        by_name[pkg["name"]].append(pkg)
    found: dict[str, str] = {}
    stack = list(roots)
    while stack:
        dep = stack.pop()
        marker = dep.get("marker")
        if marker and not Marker(marker).evaluate({**TARGET_ENV, "extra": ""}):
            continue
        candidates = [
            p
            for p in by_name[dep["name"]]
            if "version" not in dep or p["version"] == dep["version"]
        ]
        if len(candidates) != 1:
            raise SystemExit(f"uv.lock: cannot pick one {dep['name']!r}: {candidates}")
        pkg = candidates[0]
        extras = dep.get("extra", [])
        seen = f"{pkg['name']}[{','.join(extras)}]"
        if seen in found:
            continue
        found[seen] = pkg["version"]
        stack += pkg.get("dependencies", [])
        for extra in extras:
            stack += pkg.get("optional-dependencies", {}).get(extra, [])
    return {k.split("[")[0]: v for k, v in found.items()}


def _python_licence(name: str, version: str) -> str:
    try:
        meta = distribution(name).metadata
    except PackageNotFoundError:
        meta = None
    if meta is None or meta["Version"] != version:
        have = meta["Version"] if meta else "nothing"
        raise SystemExit(
            f"{name} {version} is in uv.lock but {have} is installed. Licence "
            "metadata is read from the environment: run `uv sync --locked --extra dev`."
        )
    if "License-Expression" in meta and meta["License-Expression"]:
        return str(meta["License-Expression"])
    classifiers = [
        str(c).split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if str(c).startswith("License ::")
        and str(c).strip() != "License :: OSI Approved"
    ]
    if classifiers:
        return "; ".join(classifiers)
    return str(meta["License"]) if "License" in meta else ""


def python_entries() -> list[Entry]:
    lock = tomllib.loads(UV_LOCK.read_text())
    root = next(
        p for p in lock["package"] if p.get("source", {}).get("editable") == "."
    )
    runtime = _uv_closure(lock, root.get("dependencies", []))
    dev = _uv_closure(lock, root.get("optional-dependencies", {}).get("dev", []))
    entries = []
    for name, version in {**dev, **runtime}.items():
        if name == root["name"]:
            continue
        declared = _python_licence(name, version)
        licence, raw = _normalise(name, declared, "python")
        entries.append(Entry("python", name, version, licence, raw, name in runtime))
    return entries


# ── npm ─────────────────────────────────────────────────────────────────────


def _npm_platform_ok(pkg: dict[str, Any]) -> bool:
    def ok(field: str, value: str) -> bool:
        allowed = pkg.get(field)
        if not allowed:
            return True
        if any(a == f"!{value}" for a in allowed):
            return False
        positives = [a for a in allowed if not a.startswith("!")]
        return not positives or value in positives

    return ok("os", NPM_OS) and ok("cpu", NPM_CPU) and ok("libc", NPM_LIBC)


def _npm_resolve(packages: dict[str, Any], parent: str, name: str) -> str | None:
    """Node's lookup: the nearest ``node_modules/<name>`` walking up from parent."""
    base = parent
    while True:
        key = f"{base}/node_modules/{name}" if base else f"node_modules/{name}"
        if key in packages:
            return key
        if not base:
            return None
        cut = base.rfind("/node_modules/")
        base = base[:cut] if cut >= 0 else ""


def npm_entries() -> list[Entry]:
    packages: dict[str, Any] = json.loads(NPM_LOCK.read_text())["packages"]
    installed: set[str] = set()
    stack = [""]
    while stack:
        path = stack.pop()
        pkg = packages[path]
        deps: dict[str, str] = {}
        for field in ("dependencies", "optionalDependencies", "peerDependencies"):
            deps |= pkg.get(field) or {}
        if path == "":
            deps |= pkg.get("devDependencies") or {}
        for dep in deps:
            key = _npm_resolve(packages, path, dep)
            if key is None or key in installed or not _npm_platform_ok(packages[key]):
                continue  # absent optional peer, or a binary for another platform
            installed.add(key)
            stack.append(key)

    merged: dict[tuple[str, str], Entry] = {}
    for key in installed:
        pkg = packages[key]
        name = key.rsplit("node_modules/", 1)[1]
        declared = str(pkg.get("license") or "")
        licence, raw = _normalise(name, declared, "npm")
        entry = Entry("npm", name, pkg["version"], licence, raw, not pkg.get("dev"))
        previous = merged.get((name, entry.version))
        if previous is None or entry.runtime:
            merged[(name, entry.version)] = entry
    return list(merged.values())


# ── Recorded findings ───────────────────────────────────────────────────────

#: Native libraries vendored inside the Linux x86_64 cp312 wheels, read from
#: the wheels uv.lock names (``*.libs/`` shared objects; libraries linked
#: statically into a shipped ``.so`` were found by their exported symbols).
#: (wheel, version checked, library, licence, licence reference if not SPDX).
BUNDLED: list[tuple[str, str, str, str, str]] = [
    ("shapely", "2.2.0", "GEOS 3.14.1", "LGPL-2.1-only", "wheel's LICENSE_GEOS"),
    ("exactextract", "0.3.0", "GEOS 3.14.1", "LGPL-2.1-only", "wheel's LICENSE_GEOS"),
    (
        "numpy",
        "2.5.4",
        "OpenBLAS (with LAPACK)",
        "BSD-3-Clause AND BSD-3-Clause-Open-MPI",
        "wheel's LICENSE.txt",
    ),
    (
        "numpy",
        "2.5.4",
        "libgfortran (GCC runtime)",
        "GPL-3.0-or-later WITH GCC-exception-3.1",
        "wheel's LICENSE.txt",
    ),
    ("numpy", "2.5.4", "libquadmath", "LGPL-2.1-or-later", "wheel's LICENSE.txt"),
    ("rasterio", "1.5.2", "GDAL 3.12.2", "MIT", ""),
    ("rasterio", "1.5.2", "GEOS 3.14.1", "LGPL-2.1-only", ""),
    ("rasterio", "1.5.2", "PROJ 9.8.1", "MIT", ""),
    ("rasterio", "1.5.2", "OpenSSL 3 (libssl, libcrypto)", "Apache-2.0", ""),
    ("rasterio", "1.5.2", "libcurl", "curl", ""),
    ("rasterio", "1.5.2", "nghttp2, json-c, libdeflate, giflib", "MIT", ""),
    ("rasterio", "1.5.2", "SQLite", "blessing", ""),
    ("rasterio", "1.5.2", "libtiff", "libtiff", ""),
    ("rasterio", "1.5.2", "libpng", "libpng-2.0", ""),
    ("rasterio", "1.5.2", "libjpeg-turbo", "IJG AND BSD-3-Clause AND Zlib", ""),
    ("rasterio", "1.5.2", "OpenJPEG, libaec (libsz)", "BSD-2-Clause", ""),
    (
        "rasterio",
        "1.5.2",
        "libwebp, libsharpyuv, netCDF-C, c-blosc",
        "BSD-3-Clause",
        "",
    ),
    ("rasterio", "1.5.2", "zstd", "BSD-3-Clause OR GPL-2.0-only", ""),
    ("rasterio", "1.5.2", "liblzma (XZ Utils)", "0BSD", ""),
    ("rasterio", "1.5.2", "PCRE2", "BSD-3-Clause WITH PCRE2-exception", ""),
    ("rasterio", "1.5.2", "LERC", "Apache-2.0", ""),
    (
        "rasterio",
        "1.5.2",
        "HDF5",
        "HDF5 licence (BSD-style)",
        "https://github.com/HDFGroup/hdf5/blob/develop/LICENSE",
    ),
    ("pyogrio", "0.13.0", "GDAL 3.12.4", "MIT", ""),
    ("pyogrio", "0.13.0", "GEOS (statically linked into libgdal)", "LGPL-2.1-only", ""),
    ("pyogrio", "0.13.0", "PROJ, expat, libxml2 (static)", "MIT", ""),
    (
        "pyogrio",
        "0.13.0",
        "SQLite, libcurl, OpenSSL, libtiff, libjpeg, libpng, zlib (static)",
        "blessing AND curl AND Apache-2.0 AND libtiff AND IJG AND libpng-2.0 AND Zlib",
        "",
    ),
    ("pyproj", "3.8.0", "PROJ 9.8.1", "MIT", "wheel's LICENSE_proj"),
    (
        "pyproj",
        "3.8.0",
        "libcurl, nghttp2, SQLite, libtiff",
        "curl AND MIT AND blessing AND libtiff",
        "",
    ),
    ("psycopg-binary", "3.3.8", "libpq 18", "PostgreSQL", ""),
    ("psycopg2-binary", "2.9.13", "libpq 17", "PostgreSQL", ""),
    (
        "psycopg-binary, psycopg2-binary",
        "3.3.8 / 2.9.13",
        "OpenSSL 3 (libssl, libcrypto)",
        "Apache-2.0",
        "",
    ),
    (
        "psycopg-binary, psycopg2-binary",
        "3.3.8 / 2.9.13",
        "MIT Kerberos (libkrb5, libgssapi_krb5, libk5crypto, libkrb5support, libcom_err)",
        "MIT Kerberos licence (permissive)",
        "https://web.mit.edu/kerberos/krb5-latest/doc/mitK5license.html",
    ),
    (
        "psycopg-binary, psycopg2-binary",
        "3.3.8 / 2.9.13",
        "OpenLDAP (libldap, liblber)",
        "OLDAP-2.8",
        "",
    ),
    (
        "psycopg-binary, psycopg2-binary",
        "3.3.8 / 2.9.13",
        "Cyrus SASL (libsasl2)",
        "Cyrus SASL licence (BSD-style)",
        "https://github.com/cyrusimap/cyrus-sasl/blob/master/COPYING",
    ),
    (
        "psycopg-binary, psycopg2-binary",
        "3.3.8 / 2.9.13",
        "libkeyutils, libcrypt",
        "LGPL-2.1-or-later",
        "",
    ),
    (
        "psycopg-binary, psycopg2-binary",
        "3.3.8 / 2.9.13",
        "PCRE (libpcre)",
        "BSD-3-Clause",
        "",
    ),
    (
        "psycopg-binary, psycopg2-binary",
        "3.3.8 / 2.9.13",
        "libselinux",
        "public domain",
        "https://github.com/SELinuxProject/selinux/blob/main/libselinux/LICENSE",
    ),
    (
        "pyarrow",
        "26.0.1",
        "Arrow C++ third-party code, statically linked (OpenSSL, libcurl, zlib, zstd, lz4, snappy, brotli, jemalloc, mimalloc, protobuf, abseil, gRPC, AWS and Google Cloud SDKs, xsimd, utf8proc)",
        "permissive; texts in the wheel's LICENSE.txt and NOTICE.txt",
        "",
    ),
    ("uvloop", "0.23.0", "libuv (statically linked)", "MIT", ""),
]

#: Software the platform runs beside the Works rather than importing. The
#: ``compose`` key is the docker-compose service whose image tag is printed.
#: (component, compose service or "", version if not from compose, licence, role)
SYSTEM: list[tuple[str, str, str, str, str]] = [
    ("PostgreSQL", "postgres", "", "PostgreSQL", "database server"),
    (
        "PostGIS",
        "postgres",
        "",
        "GPL-2.0-or-later",
        "spatial extension inside the database server",
    ),
    (
        "pgRouting",
        "postgres",
        "",
        "GPL-2.0-or-later",
        "routing extension inside the database server (`pgr_drivingDistance`, migration r037)",
    ),
    ("PgBouncer", "pgbouncer", "", "ISC", "connection pooler, separate process"),
    (
        "Martin",
        "martin",
        "",
        "MIT OR Apache-2.0",
        "vector tile server, separate process",
    ),
    (
        "nginx",
        "pmtiles_static",
        "",
        "BSD-2-Clause",
        "static file server for PMTiles snapshots (development stack)",
    ),
    (
        "GDAL command-line tools (`ogr2ogr`)",
        "",
        ">= 3.8",
        "MIT",
        "external program, used only by PMTiles export",
    ),
    ("CPython", "", "3.12", "PSF-2.0", "interpreter running the server code"),
    ("Node.js", "", "24 (LTS)", "MIT", "build only: compiles the web application"),
]

#: Data the platform ingests and displays. Not code, and not in the repository
#: (no data file is tracked in git); listed because its terms bind whoever
#: loads, displays or redistributes it. (source, used for, licence, attribution)
DATA: list[tuple[str, str, str, str]] = [
    (
        "OpenStreetMap (via osmnx / Overpass)",
        "street network for accessibility indicators",
        "ODbL-1.0",
        '"© OpenStreetMap contributors", shown on the map by `reimagina.data.attribution`; '
        "ODbL also requires a publicly used or distributed derivative database to be offered under ODbL",
    ),
    (
        "Overture Maps — `buildings` theme",
        "building footprints",
        "ODbL-1.0 (some sources CC-BY-4.0, per Overture's attribution page)",
        '"© OpenStreetMap contributors. Available under the Open Database License."',
    ),
    (
        "Overture Maps — `divisions` theme",
        "municipal boundary (area of interest)",
        "ODbL-1.0 (geoBoundaries and other sources CC-BY-4.0)",
        "as for buildings",
    ),
    (
        "Overture Maps — `places` theme",
        "park, recreation and cultural destinations",
        "per source: CDLA-Permissive-2.0 (most), Apache-2.0 (Foursquare), CC0-1.0 (AllThePlaces)",
        'per source, e.g. "Data from Meta. Available under CDLA Permissive 2.0."',
    ),
    (
        "INEGI — Marco Geoestadístico, Censo de Población 2020, DENUE",
        "municipalities, AGEBs, blocks, census attributes; DENUE within the amenity layers",
        "Términos de Libre Uso de la Información del INEGI (https://www.inegi.org.mx/inegi/terminos.html)",
        'credit INEGI and cite the product ("Fuente: INEGI, <producto>"); commercial use and adaptation permitted',
    ),
    (
        "Gobierno de la Ciudad de México — Colonias (datos.cdmx.gob.mx)",
        "neighbourhood boundaries",
        'terms published on the CDMX open-data portal (recorded in code as "Licencia CDMX Open Data")',
        "provider and licence, shown on the map",
    ),
    (
        "SIG-CDMX equipamientos (with DENUE)",
        "Tlalpan amenities",
        'CDMX open-data terms (recorded as "Datos abiertos de la CDMX")',
        "provider and licence, shown on the map",
    ),
    (
        "IMIP Mexicali (with DENUE)",
        "Mexicali amenities",
        'IMIP open data (recorded as "Datos abiertos del IMIP")',
        "provider and licence, shown on the map",
    ),
    (
        "WorldPop (`raster fetch-worldpop`)",
        "gridded population rasters",
        "CC-BY-4.0 (datasets derived from OSM or Microsoft footprints/roads: ODbL-1.0)",
        "credit WorldPop; the licence is recorded per raster at `raster ingest --license`",
    ),
]


def _compose_images() -> dict[str, str]:
    images: dict[str, str] = {}
    service = ""
    for line in COMPOSE.read_text().splitlines():
        if m := re.match(r"^  ([a-z_]+):\s*$", line):
            service = m.group(1)
        elif m := re.match(r"^\s+image:\s*(\S+)", line):
            images[service] = m.group(1)
    return images


def _system_versions() -> tuple[dict[str, str], dict[str, str]]:
    images = _compose_images()
    m = re.fullmatch(
        r"pgrouting/pgrouting:(\d+)-([\d.]+)-([\d.]+)", images.get("postgres", "")
    )
    if not m:
        raise SystemExit(
            f"unrecognised postgres image in {COMPOSE}: {images.get('postgres')}"
        )
    return {
        "PostgreSQL": m.group(1),
        "PostGIS": m.group(2),
        "pgRouting": m.group(3),
        **{
            name: images[svc].rsplit(":", 1)[1].lstrip("v")
            for name, svc, *_ in SYSTEM
            if svc and svc != "postgres"
        },
    }, images


# ── Rendering ───────────────────────────────────────────────────────────────


def _table(entries: list[Entry]) -> list[str]:
    lines = [
        "| Package | Version | Licence | Declared as | Distribution | Note |",
        "|---|---|---|---|---|---|",
    ]
    for e in sorted(entries, key=lambda x: (x.name.lower(), x.version)):
        lines.append(
            f"| `{e.name}` | {e.version} | {e.licence} | {_cell(e.declared)} "
            f"| {e.distribution} | {NOTES.get(e.name, '')} |"
        )
    return lines


def _count(entries: list[Entry], **where: object) -> int:
    return sum(all(getattr(e, k) == v for k, v in where.items()) for e in entries)


def render(entries: list[Entry]) -> str:
    python = [e for e in entries if e.ecosystem == "python"]
    npm = [e for e in entries if e.ecosystem == "npm"]
    copyleft = sorted(
        (e for e in entries if e.bucket != PERM), key=lambda e: (e.bucket, e.name)
    )
    missing = [e.name for e in copyleft if e.name not in NOTES]
    if missing:
        raise SystemExit(f"copyleft packages without a NOTES entry: {missing}")
    versions, images = _system_versions()
    locked = {e.name.lower(): e.version for e in python}

    def row(label: str, items: list[Entry]) -> str:
        rt, dv = _count(items, runtime=True), _count(items, runtime=False)
        return f"| {label} | {rt} | {dv} | {rt + dv} |"

    out = [
        "# Third-party dependencies and licences",
        "",
        "Complete inventory of the third-party and open-source components this",
        "platform incorporates or requires, with the licence terms of each:",
        "Python and npm packages, native libraries vendored inside Python wheels,",
        "system software the platform runs beside, and — listed separately, as",
        "data rather than code — the data sources it loads.",
        "",
        "> **Generated — do not edit.** Regenerate with",
        "> `uv run python scripts/third_party_licences.py`; a contract test runs it",
        "> with `--check`. Packages and versions come from `uv.lock` and",
        "> `apps/web/package-lock.json`, resolved for the delivered platform",
        "> (Linux x86_64, CPython 3.12, glibc), so the document is the same",
        "> whichever machine generates it.",
        "",
        "**Distribution.** *Runtime* Python packages are the closure of the",
        "project's `dependencies`; *dev/test only* are those reached only through",
        "the `dev` extra (test runner, type checker, linter). *Runtime (browser)*",
        "npm packages are those `package-lock.json` does not mark `dev`: the",
        "dependency tree of what the web application imports. The bundler then",
        "includes only modules actually imported, so this column can overstate",
        "what reaches a browser, never understate it. *Build/test only* npm",
        "packages compile or test the web application and are not shipped.",
        "",
        "## Summary",
        "",
        "| Packages | Runtime | Dev / build / test only | Total |",
        "|---|---|---|---|",
        row("Python (`uv.lock`)", python),
        row("npm (`package-lock.json`)", npm),
        row("**All packages**", entries),
        "",
        "| Licence family (packages) | Runtime | Dev / build / test only | Total |",
        "|---|---|---|---|",
    ]
    for bucket in (PERM, WEAK, STRONG):
        rt = _count(entries, bucket=bucket, runtime=True)
        dv = _count(entries, bucket=bucket, runtime=False)
        out.append(f"| {bucket} | {rt} | {dv} | {rt + dv} |")
    out += [
        "",
        f"Also listed: {len(BUNDLED)} native-library rows vendored in"
        f" {len({w.strip() for row in BUNDLED for w in row[0].split(',')})} Python wheels, {len(SYSTEM)} system components,"
        f" {len(DATA)} data sources.",
        "",
        "## Copyleft components",
        "",
        "Every component under a copyleft licence, wherever it appears in this",
        "document, and how the platform uses it. This is a factual description of",
        "use, not legal advice.",
        "",
        "**Packages**",
        "",
        "| Component | Version | Licence | Distribution | How it is used |",
        "|---|---|---|---|---|",
    ]
    for e in copyleft:
        out.append(
            f"| `{e.name}` ({e.ecosystem}) | {e.version} | {e.licence} | {e.distribution} | {NOTES[e.name]} |"
        )
    out += [
        "",
        "**Native libraries inside Python wheels** — shipped as separate shared",
        "objects (`<package>.libs/`) loaded at run time, unmodified, unless noted",
        "as statically linked:",
        "",
    ]
    for wheel, _, lib, licence, _ in BUNDLED:
        if classify(licence) != PERM:
            out.append(f"- {lib} — {licence} — in `{wheel}`")
    out += [
        "",
        "**System components** — PostGIS and pgRouting (GPL-2.0-or-later) are",
        "extensions loaded by the PostgreSQL server process. The Works reach them",
        "only by sending SQL over a database connection; they are not linked into,",
        "copied into or distributed with the Works, which reference the upstream",
        f"container image `{images['postgres']}` by tag. Base layers of the",
        "container images (Debian, Alpine) contain further GPL/LGPL programs that",
        "run inside those containers only.",
        "",
        "No package in either lockfile is under a strong-copyleft licence.",
        "",
        "## Licence glossary",
        "",
        "| Identifier | Licence | Canonical text |",
        "|---|---|---|",
    ]
    for ident, (title, url) in sorted(GLOSSARY.items(), key=lambda kv: kv[0].lower()):
        out.append(f"| `{ident}` | {title} | {url} |")
    out += [
        "",
        "## Native libraries bundled in Python wheels",
        "",
        "Read from the Linux x86_64 CPython 3.12 wheels `uv.lock` names. Package",
        "metadata names only the package's own licence (shapely says BSD-3-Clause",
        "while shipping GEOS), so these are listed separately. *Licence* is the",
        "library's upstream licence; where the wheel carries the text, *Source*",
        "says where.",
        "",
        "| Wheel | Checked at | Library | Licence | Source |",
        "|---|---|---|---|---|",
    ]
    for wheel, checked, lib, licence, src in BUNDLED:
        now = " / ".join(locked.get(w.strip(), "?") for w in wheel.split(","))
        stamp = checked if now == checked else f"{checked} (lock now {now}: re-check)"
        src = src or "upstream licence; text not in the wheel"
        out.append(f"| `{wheel}` | {stamp} | {lib} | {licence} | {src} |")
    out += [
        "",
        "## System components",
        "",
        "Required at run time but not installed by pip or npm. Versions of the",
        "containerised services are the image tags in",
        "`infra/docker/docker-compose.yml`.",
        "",
        "| Component | Version | Licence | Role |",
        "|---|---|---|---|",
    ]
    for name, svc, version, licence, role in SYSTEM:
        shown = versions.get(name, version)
        image = f" (`{images[svc]}`)" if svc else ""
        out.append(f"| {name} | {shown}{image} | {licence} | {role} |")
    out += [
        "",
        "## Data sources (data, not code)",
        "",
        "Data the platform's pipelines download, load and display. None of it is",
        "stored in the repository. The licence each loaded source carries is",
        "recorded in `curated_geo.data_source_catalog` and credited on the map",
        "(`GET /tile-sources/attribution`). For all three Overture themes the",
        'catalogue records the one string "ODbL-1.0 / Overture terms"; the',
        "per-theme terms below are Overture's own.",
        "",
        "| Source | Used for | Licence / terms | Attribution |",
        "|---|---|---|---|",
    ]
    out += [f"| {s} | {u} | {lic} | {a} |" for s, u, lic, a in DATA]
    out += ["", "## Python packages", ""] + _table(python)
    out += ["", "## npm packages", ""] + _table(npm)
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the committed document is out of date instead of writing it.",
    )
    args = parser.parse_args(argv)

    entries = python_entries() + npm_entries()
    document = render(entries)

    if args.check:
        current = OUTPUT.read_text() if OUTPUT.is_file() else ""
        if current != document:
            print(f"{OUTPUT.relative_to(ROOT)} is out of date; regenerate it.")
            return 1
        print(f"{OUTPUT.relative_to(ROOT)} is current.")
        return 0

    OUTPUT.write_text(document)
    print(f"wrote {OUTPUT.relative_to(ROOT)} — {len(entries)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
