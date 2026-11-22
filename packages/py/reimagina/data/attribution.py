"""Who has to be credited for the data currently on the map.

Attribution is a licence obligation, not a nicety: OpenStreetMap is ODbL,
and displaying OSM-derived geometry without crediting it is a breach. This
delivery's reason for having no external basemap is licence hygiene —
`main.ts`: "the prototype avoids external tile dependencies and licensing
drift" — so publishing OSM streets without the credit would spend exactly
the thing that decision was protecting.

⚠ **Derived from the data, never hardcoded in the frontend.** The street
layer can be OSM in a real deployment and a generated grid in a demo. A
frontend that always printed "© OpenStreetMap contributors" would make a
false provenance claim on every synthetic territory — in a product whose
whole argument is that its provenance is checkable. So the credit comes
from `data_source_catalog`, which already records a licence per source, and
only for sources that actually have features loaded.

The one judgement call is the wording. A provider's required credit line is
not derivable from its name. OSM's is prescribed, so there is a lookup; INEGI's
terms ask for one line per product ("Fuente: INEGI, <producto>, <fecha>"), so
its loaders record that line in `data_source_catalog.credit`; everyone else
gets a plain provider-and-licence fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Credit lines a licence prescribes, keyed by `data_source_catalog.provider`.
#:
#: Short on purpose. An entry here is a claim that this exact wording is what
#: the licence asks for, and a wrong entry is worse than the generic
#: fallback, which at least cannot misquote anyone.
PRESCRIBED: dict[str, str] = {
    "openstreetmap": "© OpenStreetMap contributors",
}


@dataclass(frozen=True)
class Attribution:
    provider: str
    license: str | None
    #: What to show on the map.
    text: str


def attribution_text(
    provider: str, license: str | None, credit: str | None = None
) -> str:
    """The credit line for one source.

    A credit the source declares wins: it is the loader's claim about its
    own product, which a provider-keyed lookup cannot make. Otherwise falls back to provider and licence rather than to nothing: an
    unrecognised provider still has to be named, and naming it with its
    licence is both honest and the thing a reviewer needs in order to check
    it.
    """
    if credit:
        return credit
    prescribed = PRESCRIBED.get(provider.strip().lower())
    if prescribed:
        return prescribed
    if license:
        return f"{provider} ({license})"
    return provider


def attributions(conn) -> list[Attribution]:
    """Credit lines for every source with data on the map.

    "With data" rather than "registered": a source row that was created
    and never loaded has nothing on the map, and crediting it would claim
    the map shows data it does not. Data is features, or a published raster
    layer — a raster (WorldPop, CC BY 4.0) has no feature rows at all — or,
    for a source that declares a credit, observations: the INEGI census lands
    only as observations on blocks. Observation-only sources without a credit
    are our own derived rows (rollups, synthetic seeds), not anyone to credit.

    One line per distinct text, so the four Marco Geoestadístico layers credit
    the product once. Sorted, so the rendered string is stable between
    requests — an attribution line that reshuffles on refresh looks like a bug.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT s.provider, s.license, s.credit
            FROM curated_geo.data_source_catalog s
            WHERE EXISTS (
                SELECT 1 FROM curated_geo.feature f WHERE f.source_id = s.id
            ) OR EXISTS (
                SELECT 1 FROM curated_geo.raster_layer r
                WHERE r.source_id = s.id AND r.published
            ) OR (s.credit IS NOT NULL AND EXISTS (
                SELECT 1 FROM curated_geo.observation o
                WHERE o.source_id = s.id AND o.is_current
            ))
            """
        )
        rows = cur.fetchall()
    by_text: dict[str, Attribution] = {}
    for provider, license, credit in sorted(rows, key=lambda r: tuple(map(str, r))):
        text = attribution_text(provider, license, credit)
        by_text.setdefault(
            text, Attribution(provider=provider, license=license, text=text)
        )
    return sorted(by_text.values(), key=lambda a: (a.provider, a.text))
