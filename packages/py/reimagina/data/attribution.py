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
not derivable from its name, and OSM's is prescribed, so there is a lookup
with a plain fallback for anyone not in it.
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


def attribution_text(provider: str, license: str | None) -> str:
    """The credit line for one source.

    Falls back to provider and licence rather than to nothing: an
    unrecognised provider still has to be named, and naming it with its
    licence is both honest and the thing a reviewer needs in order to check
    it.
    """
    prescribed = PRESCRIBED.get(provider.strip().lower())
    if prescribed:
        return prescribed
    if license:
        return f"{provider} ({license})"
    return provider


def attributions(conn) -> list[Attribution]:
    """Credit lines for every source with features in the database.

    "With features" rather than "registered": a source row that was created
    and never loaded has nothing on the map, and crediting it would claim
    the map shows data it does not.

    Sorted, so the rendered string is stable between requests — an
    attribution line that reshuffles on refresh looks like a bug.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT s.provider, s.license
            FROM curated_geo.data_source_catalog s
            WHERE EXISTS (
                SELECT 1 FROM curated_geo.feature f WHERE f.source_id = s.id
            )
            ORDER BY s.provider, s.license
            """
        )
        rows = cur.fetchall()
    return [
        Attribution(
            provider=provider, license=license, text=attribution_text(provider, license)
        )
        for provider, license in rows
    ]
