"""Read administrative boundaries from Overture's ``divisions`` theme.

Every AOI constructor in this repository is INEGI-derived:
``aoi_from_alcaldia`` looks up a ``MEX:municipality`` feature by
``(CVE_ENT, CVE_MUN)``, ``aoi_from_state`` by ``CVE_ENT``. Both need the
Marco Geoestadístico mirror on disk, and both return
``missing_municipality`` without it. That makes the boundary the hard
precondition for a second pilot city — harder than the buildings, because
``get_tile`` scopes every request by AOI through an ``EXISTS`` against
``curated_geo.aoi_feature``. Features in no AOI are correctly typed,
correctly zoomed, present in ``serving_geo`` — and return
``204 No Content`` on every tile.

Overture's ``divisions`` theme carries those boundaries, in the same S3
release as the buildings and under the same licence. For Mexicali it
holds ``county · MX · Municipio de Mexicali``, which is a real municipal
polygon obtained without INEGI, without a credential and without
widening any network allowlist.

**Why matching needs all three of subtype, country and name.** The
Mexicali bbox returns 16 divisions spanning two countries, and
``US · Imperial County`` and ``MX · Municipio de Mexicali`` are both
``county``. Selecting the first ``county`` row would be a coin flip
between the two sides of the border, and the failure would be silent: a
valid polygon, a successful load, and a map of the wrong country.

**Why a miss returns None.** ``names.primary`` is upstream data and can be
retitled between releases, so "no match" is the expected failure rather
than an exceptional one. The caller gets the candidates back and can
print what the release actually calls things, instead of a traceback that
leaves the reader guessing whether the name, the bbox or the release was
wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow.compute as pc
import pyarrow.dataset as pads
from pyarrow import fs as pafs

from .overture import (
    OVERTURE_DEFAULT_RELEASE,
    OVERTURE_RELEASE_S3_ROOT,
    OVERTURE_S3_REGION,
    available_overture_releases,
)

#: Overture theme/type holding administrative *areas* (polygons). The sibling
#: ``division`` type holds points and ``division_boundary`` holds the shared
#: border lines; neither is a usable AOI geometry.
DIVISIONS_THEME = "divisions"
DIVISIONS_AREA_TYPE = "division_area"

#: Columns the selection needs. Named explicitly because the theme carries
#: `geometry` as WKB and reading every column of every candidate would fetch
#: the polygons of all 16 divisions to pick one.
_SELECT_COLUMNS = ("id", "subtype", "class", "country", "region", "names")

#: Overture licence terms, matching :mod:`reimagina.pipeline.overture`.
OVERTURE_PROVIDER = "Overture Maps Foundation"
OVERTURE_LICENSE = "ODbL-1.0 / Overture terms"


@dataclass(frozen=True)
class DivisionCandidate:
    """One division in the bbox, without its geometry."""

    overture_id: str
    subtype: str | None
    division_class: str | None
    country: str | None
    region: str | None
    name: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "overture_id": self.overture_id,
            "subtype": self.subtype,
            "class": self.division_class,
            "country": self.country,
            "region": self.region,
            "name": self.name,
        }


@dataclass(frozen=True)
class DivisionMatch:
    """A selected division, with the geometry an AOI needs."""

    candidate: DivisionCandidate
    geometry_wkt: str
    release: str

    @property
    def overture_id(self) -> str:
        return self.candidate.overture_id

    @property
    def name(self) -> str | None:
        return self.candidate.name


def _primary_name(names: Any) -> str | None:
    """Overture stores names as a struct with a ``primary`` field.

    Tolerant of a bare string because the struct's shape is upstream and
    has changed shape before; a release that flattens it should degrade to
    a working match rather than to a ``TypeError`` deep in a filter.
    """
    if isinstance(names, dict):
        primary = names.get("primary")
        return str(primary) if primary is not None else None
    if isinstance(names, str):
        return names
    return None


def _bbox_filter(bbox: tuple[float, float, float, float]):
    """Overlap predicate on the GeoParquet ``bbox`` struct columns.

    Filtering on these rather than on the geometry is what lets pyarrow
    prune non-overlapping row groups from their statistics without
    fetching them — the same cloud-native pushdown the buildings extract
    relies on.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return (
        (pc.field("bbox", "xmax") >= min_lon)
        & (pc.field("bbox", "xmin") <= max_lon)
        & (pc.field("bbox", "ymax") >= min_lat)
        & (pc.field("bbox", "ymin") <= max_lat)
    )


def _divisions_dataset(release: str):
    prefix = (
        f"{OVERTURE_RELEASE_S3_ROOT.removeprefix('s3://')}/{release}/"
        f"theme={DIVISIONS_THEME}/type={DIVISIONS_AREA_TYPE}"
    )
    s3 = pafs.S3FileSystem(anonymous=True, region=OVERTURE_S3_REGION)
    try:
        return pads.dataset(prefix, filesystem=s3, format="parquet")
    except FileNotFoundError as exc:
        # Same treatment the buildings extract gives a rotated release: say
        # what S3 actually has, because the bare error names a path and
        # leaves the reader to guess whether it is a typo, a permissions
        # problem, or Overture having deleted the release.
        raise FileNotFoundError(
            f"Overture release not found: {exc}. Overture keeps only its most "
            f"recent releases, so a pin older than a few weeks stops "
            f"resolving. Available now: "
            f"{', '.join(available_overture_releases()) or '(none)'}. "
            f"Set REIMAGINA_OVERTURE_RELEASE, or bump "
            f"OVERTURE_DEFAULT_RELEASE in pipeline/overture.py."
        ) from exc


def list_divisions(
    *,
    bbox: tuple[float, float, float, float],
    release: str = OVERTURE_DEFAULT_RELEASE,
) -> list[DivisionCandidate]:
    """Every division overlapping ``bbox``, geometry excluded.

    This is the command that makes :func:`find_division`'s ``name``
    argument checkable rather than guessable.
    """
    dataset = _divisions_dataset(release)
    columns = [c for c in _SELECT_COLUMNS if c in dataset.schema.names]
    table = dataset.to_table(filter=_bbox_filter(bbox), columns=columns)
    return [
        DivisionCandidate(
            overture_id=str(row.get("id")),
            subtype=row.get("subtype"),
            division_class=row.get("class"),
            country=row.get("country"),
            region=row.get("region"),
            name=_primary_name(row.get("names")),
        )
        for row in table.to_pylist()
    ]


def matches(
    candidates: list[DivisionCandidate],
    *,
    subtype: str | None,
    country: str | None,
    name: str | None,
) -> list[DivisionCandidate]:
    """Candidates satisfying every supplied criterion.

    ⚠ Separated from the S3 read so the selection rule is testable without
    a network call — and so the rule that
    ``US · Imperial County`` can never be returned for ``country='MX'``
    has a unit test rather than a comment.

    A ``None`` criterion is "don't care". Name comparison is
    case-insensitive and whitespace-trimmed; country is upper-cased, since
    Overture uses ISO-3166 alpha-2.
    """

    def ok(c: DivisionCandidate) -> bool:
        if subtype is not None and (c.subtype or "").lower() != subtype.lower():
            return False
        if country is not None and (c.country or "").upper() != country.upper():
            return False
        if name is not None:
            if (c.name or "").strip().lower() != name.strip().lower():
                return False
        return True

    return [c for c in candidates if ok(c)]


def find_division(
    *,
    bbox: tuple[float, float, float, float],
    release: str = OVERTURE_DEFAULT_RELEASE,
    subtype: str | None = None,
    country: str | None = None,
    name: str | None = None,
) -> tuple[DivisionMatch | None, list[DivisionCandidate]]:
    """Select one division and read its polygon as WKT.

    Returns ``(match, candidates)``. ``candidates`` is everything the bbox
    held, always — so a caller that got ``None`` can report what the
    release actually contains instead of only that it found nothing.

    More than one match is an error rather than a silent first-row pick:
    two boundaries sharing subtype, country and name means the criteria do
    not identify a place, and loading either one would be a guess.
    """
    candidates = list_divisions(bbox=bbox, release=release)
    selected = matches(candidates, subtype=subtype, country=country, name=name)
    if not selected:
        return None, candidates
    if len(selected) > 1:
        raise ValueError(
            f"{len(selected)} divisions match subtype={subtype!r} "
            f"country={country!r} name={name!r} in release {release}: "
            f"{[c.overture_id for c in selected]}. Narrow the criteria — "
            f"loading either one would be a guess."
        )
    wanted = selected[0]

    # Second pass, for the geometry alone. The first read deliberately
    # excluded it so that picking one of 16 divisions did not fetch 16
    # polygons; now that the choice is made, fetch exactly one.
    import shapely.wkb

    dataset = _divisions_dataset(release)
    table = dataset.to_table(
        filter=_bbox_filter(bbox) & (pc.field("id") == wanted.overture_id),
        columns=["id", "geometry"],
    )
    rows = table.to_pylist()
    if not rows:
        # The release changed under us between the two reads.
        return None, candidates
    geometry_wkt = shapely.wkb.loads(rows[0]["geometry"]).wkt
    return (
        DivisionMatch(candidate=wanted, geometry_wkt=geometry_wkt, release=release),
        candidates,
    )
