#!/usr/bin/env python
"""Build, or verify, the Mexicali source bundle.

The bundle is every input ``scripts/mexicali_run.sh --bundle`` reads, in one
directory, so the Mexicali pilot can be rebuilt offline: no network and no
access to wherever the inputs first came from.

    python scripts/build_mexicali_bundle.py build --out DIR \\
        --inegi-marco      <local copy of INEGI Marco Geoestadístico 2024, parquet>
        --inegi-censo      <local copy of INEGI Censo 2020 by block, parquet>
        --denue-amenities  <the Mexicali DENUE amenity GeoParquet>
        --overture-buildings <`overture extract` output> \\
        --overture-places  <`overture places` extract> --overture-release REL \\
        --osm-network      <directory written by `network bootstrap --save-graph`>
    python scripts/build_mexicali_bundle.py verify DIR

The INEGI inputs are parquet copies of INEGI's public products, hive-
partitioned the way the loaders read them (``<layer>.parquet/CVE_ENT=NN/
CVE_MUN=NNN/`` for the Marco Geoestadístico, ``entidad=N/`` for the census).
``build`` keeps only Baja California (``--cve-ent``) and only the columns the
loaders read; the Overture extracts, the DENUE layer and the OSM graph are
copied unchanged. Same inputs give the same bytes, so SHA256SUMS is
reproducible. ``verify`` checks every file against SHA256SUMS and fails on a
mismatch, a missing file or a file SHA256SUMS does not list.

ATTRIBUTION.md carries the credits each licence asks for; read it before
redistributing the bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

#: Bundle layout. scripts/mexicali_run.sh reads the same paths.
MARCO_DIR = "inegi/marco_geoestadistico_2024"
CENSO_DIR = "inegi/censo_2020/manzanas_2020.parquet"
DENUE_FILE = "inegi/denue/equipamientos_mexicali.parquet"
SUMS = "SHA256SUMS"

INEGI_LICENCE = "INEGI Términos de Libre Uso de la Información"
INEGI_TERMS_URL = "https://www.inegi.org.mx/inegi/terminos.html"


# ── checksums ────────────────────────────────────────────────────────────────


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_files(root: Path) -> list[str]:
    """Every file in the bundle except SHA256SUMS, as sorted posix paths."""
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != SUMS and p.name != ".DS_Store"
    )


def write_sums(root: Path) -> None:
    lines = [f"{sha256(root / rel)}  {rel}\n" for rel in bundle_files(root)]
    (root / SUMS).write_text("".join(lines))


def verify(root: Path) -> list[str]:
    """Problems with ``root`` against its SHA256SUMS; empty when it is intact."""
    sums = root / SUMS
    if not sums.is_file():
        return [f"{sums} is missing"]
    listed: dict[str, str] = {}
    for line in sums.read_text().splitlines():
        digest, _, rel = line.partition("  ")
        listed[rel] = digest
    if not listed:
        return [f"{sums} lists no files"]
    problems = []
    for rel, digest in listed.items():
        path = root / rel
        if not path.is_file():
            problems.append(f"missing: {rel}")
        elif sha256(path) != digest:
            problems.append(f"checksum mismatch: {rel}")
    problems += [
        f"not in {SUMS}: {rel}" for rel in bundle_files(root) if rel not in listed
    ]
    return problems


# ── subsetting ───────────────────────────────────────────────────────────────


def _write(table, path: Path) -> None:
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    # No pandas/arrow schema metadata: it is not data, and dropping it keeps
    # the bytes a function of the rows alone.
    pq.write_table(table.replace_schema_metadata(None), path)


def _concat(files: list[Path], columns: list[str]):
    import pyarrow as pa
    import pyarrow.parquet as pq

    tables = []
    for f in files:
        names = pq.read_schema(f).names
        tables.append(pq.read_table(f, columns=[c for c in columns if c in names]))
    return pa.concat_tables(tables, promote_options="permissive")


def subset_marco(src: Path, out: Path, cve_ent: str) -> dict[str, int]:
    """One entidad of each Marco Geoestadístico layer, loader columns only.

    The CVE_ENT/CVE_MUN partition directories are kept: they are where the
    loader reads those two codes from.
    """
    from reimagina.pipeline import inegi_marco

    rows: dict[str, int] = {}
    for key in inegi_marco.DEFAULT_LOAD_ORDER:
        spec = inegi_marco.LAYER_SPECS[key]
        columns = [spec.source_record_ref_col, *spec.attributes_cols, "geometry"]
        if spec.name_col:
            columns.insert(1, spec.name_col)
        columns = [c for c in dict.fromkeys(columns) if c not in spec.partition_cols]
        layer_src = src / spec.parquet_subdir / f"CVE_ENT={cve_ent}"
        files = _parquet_files(layer_src) if layer_src.is_dir() else []
        if not files:
            raise FileNotFoundError(f"no parquet under {layer_src}")
        total = 0
        for part_dir in sorted({f.parent for f in files}):
            table = _concat([f for f in files if f.parent == part_dir], columns)
            rel = part_dir.relative_to(src)
            _write(table, out / MARCO_DIR / rel / "part-0.parquet")
            total += table.num_rows
        rows[key] = total
    return rows


def subset_censo(src: Path, out: Path, cve_ent: str) -> int:
    """One entidad of the 2020 census by block, loader columns only."""
    from reimagina.pipeline import inegi_censo

    entidad = str(int(cve_ent))
    columns = [
        "nom_mun",
        "loc",
        "ageb",
        "mza",
        *inegi_censo.HEADLINE_COLUMNS,
        *inegi_censo.SCENARIO_INPUT_COLUMNS,
    ]
    part = src / f"entidad={entidad}"
    files = _parquet_files(part) if part.is_dir() else []
    if not files:
        raise FileNotFoundError(f"no parquet under {src / f'entidad={entidad}'}")
    table = _concat(files, columns)
    _write(table, out / CENSO_DIR / f"entidad={entidad}" / "part-0.parquet")
    return table.num_rows


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _rows(path: Path) -> int:
    import pyarrow.parquet as pq

    return sum(pq.ParquetFile(f).metadata.num_rows for f in _parquet_files(path))


def _parquet_files(path: Path) -> list[Path]:
    # Filtered to files: a hive layer directory is itself named "*.parquet".
    if path.is_dir():
        return sorted(p for p in path.rglob("*.parquet") if p.is_file())
    return [path]


# ── manifest and attribution ─────────────────────────────────────────────────


ATTRIBUTION = """\
# Mexicali source bundle — attribution and licences

This bundle holds the inputs `scripts/mexicali_run.sh --bundle` reads to build
the Mexicali pilot. Each dataset keeps its own licence; MANIFEST.json lists
every file with its rows, SHA-256, source product, vintage and licence.

## INEGI (Instituto Nacional de Estadística y Geografía)

Licence: {inegi_licence} — {inegi_terms_url}. The terms allow copying,
redistribution, transformation and commercial use, provided INEGI is credited
as the source, users are told about any transformation, and no endorsement is
implied.

- Fuente: INEGI, Marco Geoestadístico 2024 (version recorded as 2024.01).
  https://www.inegi.org.mx/temas/mg/
- Fuente: INEGI, Censo de Población y Vivienda 2020, resultados por AGEB y
  manzana urbana. https://www.inegi.org.mx/programas/ccpv/2020/
- Fuente: INEGI, Directorio Estadístico Nacional de Unidades Económicas
  (DENUE), mayo 2025. https://www.inegi.org.mx/app/mapa/denue/

**Transformations made by this project, not by INEGI:**

- Marco Geoestadístico: kept Baja California only (CVE_ENT={cve_ent}) and the
  layers areas_estatales, areas_municipales, agebs_urbanas and
  manzanas_urbanas; kept only the columns the loaders read (CVEGEO, NOMGEO,
  CVE_LOC, CVE_AGEB, CVE_MZA, AMBITO, TIPOMZA, geometry); converted to
  hive-partitioned parquet (CVE_ENT/CVE_MUN directories), geometry as WKB.
  Geometries and values are otherwise unchanged.
- Censo 2020: kept Baja California only (entidad={entidad}) and the columns
  nom_mun, loc, ageb, mza, pobtot, pobmas, pobfem, p_15ymas, p_18ymas, vivtot
  and prom_ocup; converted to parquet. Values are unchanged.
- DENUE: a selection of {denue_rows:,} Mexicali establishments in five categories
  (Consultorio medico, Farmacia, Hospital, Guarderia, Cine) with columns id,
  entidad, category, capacity, fuente, fecha and point geometry, as GeoParquet.

The parquet copies do not carry INEGI's original metadata files; the product
names, versions and pages above stand in for them.

INEGI has not reviewed, approved or endorsed this bundle, the transformations
above, or any result computed from them.

## Overture Maps Foundation

- Buildings, release {release}: licence ODbL 1.0
  (https://opendatacommons.org/licenses/odbl/1-0/). © Overture Maps
  Foundation; includes © OpenStreetMap contributors and the other sources
  listed at https://docs.overturemaps.org/attribution/.
  Extract of the Mexicali box -115.70,32.40,-115.20,32.80, unchanged.
- Places, release {release}: licence CDLA-Permissive-2.0
  (https://cdla.dev/permissive-2-0/); individual records may carry Apache-2.0
  or CC0-1.0 per source. © Overture Maps Foundation. Park, recreation and
  cultural places in the same box, Mexico only, unchanged.

## OpenStreetMap

- Pedestrian street network fetched {osm_fetched} through the Overpass API with
  osmnx (network type "walk", simplified), stored as nodes.parquet +
  edges.parquet. © OpenStreetMap contributors,
  https://www.openstreetmap.org/copyright.

## ODbL notice (Overture buildings and the OSM network)

These two datasets are made available under the Open Database License 1.0. You
may copy, distribute and adapt them provided you attribute them as above, and
any adapted database you distribute must also be offered under the ODbL. Maps
and other produced works made from them must carry the attribution.
"""


def manifest_entries(out: Path, datasets: list[dict]) -> list[dict]:
    """One entry per file: the dataset's description plus rows and sha256."""
    entries = []
    for ds in datasets:
        for f in _parquet_files(out / ds["path"]):
            entries.append(
                {
                    "file": f.relative_to(out).as_posix(),
                    "rows": _rows(f),
                    "bytes": f.stat().st_size,
                    "sha256": sha256(f),
                    **{k: v for k, v in ds.items() if k != "path"},
                }
            )
    return entries


def build(args: argparse.Namespace) -> dict:
    from reimagina.pipeline.osm_network import read_graph

    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; build into a new directory")
    out.mkdir(parents=True, exist_ok=True)
    release = args.overture_release
    _nodes, _edges, osm_fetched = read_graph(args.osm_network)
    osm_dir = f"osm/walk_network_{osm_fetched}"

    marco_rows = subset_marco(Path(args.inegi_marco), out, args.cve_ent)
    censo_rows = subset_censo(Path(args.inegi_censo), out, args.cve_ent)
    _copy(Path(args.denue_amenities), out / DENUE_FILE)
    _copy(Path(args.overture_buildings), out / f"overture/buildings_{release}.parquet")
    _copy(Path(args.overture_places), out / f"overture/places_{release}.parquet")
    for name in ("nodes.parquet", "edges.parquet"):
        _copy(Path(args.osm_network) / name, out / osm_dir / name)

    subset = f"Baja California (CVE_ENT={args.cve_ent}); loader columns only"
    datasets = [
        {
            "path": MARCO_DIR,
            "source": "INEGI, Marco Geoestadístico 2024",
            "vintage": "2024.01",
            "licence": INEGI_LICENCE,
            "produced": f"subset of INEGI's product: {subset}",
        },
        {
            "path": CENSO_DIR,
            "source": "INEGI, Censo de Población y Vivienda 2020, by block",
            "vintage": "2020",
            "licence": INEGI_LICENCE,
            "produced": f"subset of INEGI's product: {subset}",
        },
        {
            "path": DENUE_FILE,
            "source": "INEGI, DENUE (Mexicali amenity selection)",
            "vintage": "2025-05",
            "licence": INEGI_LICENCE,
            "produced": "copied unchanged",
        },
        {
            "path": f"overture/buildings_{release}.parquet",
            "source": "Overture Maps Foundation, buildings",
            "vintage": release,
            "licence": "ODbL-1.0",
            "produced": "`overture extract --bbox -115.70,32.40,-115.20,32.80`, "
            "copied unchanged",
        },
        {
            "path": f"overture/places_{release}.parquet",
            "source": "Overture Maps Foundation, places",
            "vintage": release,
            "licence": "CDLA-Permissive-2.0",
            "produced": "`overture places --country MX` extract of the same "
            "box, copied unchanged",
        },
        {
            "path": osm_dir,
            "source": "OpenStreetMap via Overpass (osmnx walk network)",
            "vintage": osm_fetched,
            "licence": "ODbL-1.0",
            "produced": "`network bootstrap --clip-bbox -115.70,32.40,-115.20,"
            "32.80 --save-graph`, copied unchanged",
        },
    ]
    manifest = {
        "bundle": "reimagina Mexicali source bundle",
        "builder": "scripts/build_mexicali_bundle.py",
        "cve_ent": args.cve_ent,
        "overture_release": release,
        "osm_fetched": osm_fetched,
        "files": manifest_entries(out, datasets),
    }
    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    (out / "ATTRIBUTION.md").write_text(
        ATTRIBUTION.format(
            inegi_licence=INEGI_LICENCE,
            inegi_terms_url=INEGI_TERMS_URL,
            cve_ent=args.cve_ent,
            entidad=int(args.cve_ent),
            release=release,
            osm_fetched=osm_fetched,
            denue_rows=_rows(out / DENUE_FILE),
        )
    )
    write_sums(out)
    return {"out": str(out), "marco_rows": marco_rows, "censo_rows": censo_rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build a bundle into a new directory")
    b.add_argument("--out", required=True)
    b.add_argument("--inegi-marco", required=True)
    b.add_argument("--inegi-censo", required=True)
    b.add_argument("--denue-amenities", required=True)
    b.add_argument("--overture-buildings", required=True)
    b.add_argument("--overture-places", required=True)
    b.add_argument("--overture-release", required=True)
    b.add_argument("--osm-network", required=True)
    b.add_argument("--cve-ent", default="02")
    v = sub.add_parser("verify", help="check a bundle against its SHA256SUMS")
    v.add_argument("bundle")
    args = parser.parse_args(argv)

    if args.command == "verify":
        problems = verify(Path(args.bundle))
        for p in problems:
            print(p, file=sys.stderr)
        if not problems:
            print(f"{args.bundle}: all files match {SUMS}")
        return 1 if problems else 0
    print(json.dumps(build(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
