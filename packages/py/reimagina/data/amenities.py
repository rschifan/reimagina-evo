"""Which Overture place categories count as which kind of amenity.

**Why this exists.** The Mexicali pilot's amenity layer comes from IMIP and
carries five categories — ``Consultorio medico``, ``Farmacia``, ``Hospital``,
``Guarderia``, ``Cine``. Three map to ``health``, one to ``school``, and
**none** is park-like. So ``accessibility_block_park_network_m`` ran over an
empty destination set: the multi-source Dijkstra had no start vertices, every
one of 19,058 blocks reported the 1,500 m cutoff, and the map drew a flat
choropleth that reads as a real and uniformly bad accessibility surface. The
absence was the finding, and it was indistinguishable from a measurement.

Overture's ``places`` theme carries the missing destinations in the same S3
release, under the same licence, as the buildings already ingested — no new
credential and no new allowlist entry. This module is the vocabulary that says
which of its 221 categories count.

**Why the exclusions are written down.** Selecting park categories by keyword
is actively dangerous here, and the first pass at it proved the point: a
substring match on ``park``/``garden``/``sport`` pulls in
``hardware_home_and_garden_store`` (907 places in the Mexicali bbox — a
hardware shop), ``rv_park`` and ``mobile_home_park`` (housing),
``parking_lot``, ``sporting_goods_store`` and ``sports_bar``. Nine hundred
shops would have swamped 309 actual parks and produced a confident,
wrong-by-construction accessibility surface — the same failure as the flat
layer, only harder to see because the numbers would vary. So the allowlist is
explicit, the near-misses are listed beside it, and
``tests/unit/test_amenity_categories.py`` asserts the two never overlap.

⚠ **These are category *names*, not a claim about what is on the ground.**
Overture's categories come from its own sources; a place tagged ``park`` may be
a traffic island and a ``gym`` may have closed. The indicator measures distance
to the nearest thing *so tagged*, which is what its catalog description says.
"""

from __future__ import annotations

import re
import unicodedata

#: Public open space and sports/recreation facilities.
#
# Scope note, because the composition matters more than the total. Of the 1,001
# park destinations actually loaded for Mexicali — after the country and name
# filters, which is the number that reaches the indicator:
#
#   commercial fitness    570   gym 304, sport_or_fitness_facility 205, studio 61
#   other sport / rec     253   clubs, fields, stadiums, pools, courts
#   public open space      178   park 151, plazas, playgrounds, gardens
#
# They are all here because the indicator's declared scope has always included
# private clubs: the IMIP vocabulary it was written against lists
# ``Clubs_deportivos_y_acondicionamiento_fisico`` alongside
# ``Parques_recreativos``, and ACCESSIBILITY_LABELS['park'] says so. But a
# reader who takes "distance to park" as public green space will be wrong about
# this layer by a factor of five, so the catalog description states the split.
# Narrowing to public open space alone is a defensible *different* indicator —
# it is not a correction to this one — and would mean dropping the fitness and
# club entries below.
OVERTURE_PARK_CATEGORIES: tuple[str, ...] = (
    # Public open space.
    "park",
    "national_park",
    "nature_reserve",
    "public_plaza",
    "playground",
    "garden",
    "skate_park",
    # Sports facilities open to the public.
    "sport_field",
    "sport_court",
    "stadium_arena",
    "swimming_pool",
    "skating_rink",
    "golf_course",
    # Recreation venues.
    "amusement_park",
    "sports_and_recreation",
    # Private sports and fitness clubs — declared scope, see the note above.
    "sport_or_recreation_club",
    "sport_or_fitness_facility",
    "fitness_studio",
    "gym",
)


#: Cultural and entertainment venues.
#
# Exists because IMIP's ``Cine`` (10 cinemas in Mexicali) matched no category
# at all and was silently ingested as an establishment nothing measured. Folding
# cinemas into ``park`` to make that layer non-flat would have manufactured a
# finding out of 10 points; they get their own category instead.
OVERTURE_CULTURE_CATEGORIES: tuple[str, ...] = (
    "movie_theater",
    "theatre_venue",
    "performing_arts_venue",
    "comedy_club",
    "museum",
    "art_gallery",
    "cultural_center",
    "library",
    "arcade",
    "zoo",
    "aquarium",
)


#: Categories deliberately NOT counted, and why.
#
# Kept as data rather than prose so the exclusions are reviewable and testable.
# Every entry here was a candidate — either a keyword match on a park/recreation
# term, or a plausible-looking cultural venue — and was rejected on the stated
# ground. Counts are places in the Mexicali bbox at release 2026-10-21.0.
OVERTURE_EXCLUDED_LOOKALIKES: dict[str, str] = {
    # Shops that merely mention a park/garden/sport word.
    "hardware_home_and_garden_store": "a hardware shop (907 places — the single"
    " biggest false positive a keyword match produces here)",
    "nursery_and_gardening_store": "a shop that sells plants",
    "sporting_goods_store": "a shop that sells sports equipment",
    "sportswear_store": "a clothing shop",
    "musical_instrument_and_pro_audio_store": "a shop, not a venue",
    "toys_and_games_store": "a shop, not a play space",
    # Housing and vehicle storage that carry 'park' in the name.
    "rv_park": "housing for recreational vehicles, not a park",
    "mobile_home_park": "housing, not a park",
    "parking": "vehicle storage",
    "parking_lot": "vehicle storage",
    "parking_garage": "vehicle storage",
    # Organisations rather than places someone walks to.
    "sport_league": "an organisation with no public premises to reach",
    "sport_team": "an organisation with no public premises to reach",
    "amateur_sport_league": "an organisation, not a destination",
    "amateur_sport_team": "an organisation, not a destination",
    "fitness_trainer": "a person's practice, not a facility",
    "sports_psychology": "a clinical practice",
    # Drinking venues with recreation words in the category name.
    "sports_bar": "a bar",
    "beer_garden": "a bar",
    "dance_club": "nightlife, not a cultural venue",
    "casino": "gambling, not recreation infrastructure",
    "adult_entertainment_venue": "not a civic amenity",
    # Plausible but too noisy or too marginal to count.
    "music_venue": "132 places, dominated by bars with live music rather than"
    " concert halls — including them would double the culture count with"
    " drinking venues",
    "historic_site": "200 places; heritage markers and plaques are not venues"
    " a resident travels to for recreation",
    "arts_and_entertainment": "an umbrella category whose 104 members are"
    " unknowable without inspecting each one",
    "beach": "16 places in an inland desert municipio — almost certainly"
    " mislabelled, and counting them would invent destinations",
    "campground": "4 places, and camping is not everyday park access",
    "hot_springs": "3 places, a regional day trip rather than local recreation",
    "community_center": "civic, not recreational — belongs to a services"
    " indicator this catalog does not yet have",
    "public_fountain": "street furniture, not a destination",
    "lake": "a natural feature with no implied public access point",
}


#: Name patterns that disqualify a place whatever its category says, and why.
#
# ⚠ **These are not fussiness — they remove destinations that are wrong.**
# Overture's ``park`` category is assigned partly from the name, so in a Spanish
# -speaking industrial city it collects things that are not parks at all. In the
# Mexicali bbox it returns 16 ``Parque Industrial`` entries — industrial estates,
# the opposite of recreational open space, and concentrated in exactly the
# districts where a worker's walk to a real park is the question being asked.
# Leaving them in does not add noise evenly; it puts a fake destination in the
# middle of every industrial zone and reports that zone as well served.
#
# The ``jardín de eventos`` pattern is the same failure in a different dress:
# a rental venue for weddings and quinceañeras, tagged ``park``,
# ``amusement_park`` or even ``swimming_pool``. It is private, ticketed and
# closed most days.
#
# Patterns are matched against the **accent-folded, lower-cased** name, so
# ``Jardín`` and ``Jardin`` both hit. Verified against the real extract: these
# drop 28 of 1,303 places and leave every ``Parque Acuático``,
# ``Parque Infantil`` and aquatic centre standing —
# ``tests/unit/test_amenity_categories.py`` pins both halves of that.
PLACE_NAME_EXCLUSIONS: dict[str, str] = {
    r"parque industrial|industrial park|parque logistico": (
        "an industrial estate, not recreational open space"
    ),
    r"jardin (?:de|para) eventos?|salon de eventos?": (
        "a private event-rental venue, not public open space"
    ),
    r"funeraria|parque memorial|panteon|cementerio|crematorio": (
        "a cemetery or funeral home"
    ),
}


def fold_name(name: str | None) -> str:
    """Lower-case and strip accents, so one pattern matches both spellings.

    Mexicali's place names arrive both accented and not — ``Jardín`` and
    ``Jardin`` are the same venue typed by two contributors — so matching the
    raw string would need every pattern written twice and would still miss the
    third spelling.
    """
    decomposed = unicodedata.normalize("NFKD", name or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


#: One compiled alternation over :data:`PLACE_NAME_EXCLUSIONS`.
_EXCLUSION_RE = re.compile("|".join(PLACE_NAME_EXCLUSIONS), re.IGNORECASE)


def excluded_by_name(name: str | None) -> str | None:
    """The reason ``name`` is disqualified, or ``None`` if it is fine.

    Returns the reason rather than a boolean so the ingest can report *what*
    it dropped and why. A count alone ("28 places excluded") is unauditable;
    the reason is what makes a wrong rule visible.
    """
    folded = fold_name(name)
    for pattern, reason in PLACE_NAME_EXCLUSIONS.items():
        if re.search(pattern, folded, re.IGNORECASE):
            return reason
    return None


#: accessibility category -> the Overture place categories that feed it.
OVERTURE_AMENITY_CATEGORIES: dict[str, tuple[str, ...]] = {
    "park": OVERTURE_PARK_CATEGORIES,
    "culture": OVERTURE_CULTURE_CATEGORIES,
}


def overture_categories_to_load() -> tuple[str, ...]:
    """Every Overture category the ingest should keep, deduplicated and sorted.

    The ingest filters on this rather than loading the theme wholesale: the
    Mexicali bbox holds 43,037 places, of which 4,727 are restaurants. Loading
    all of them would put tens of thousands of rows into
    ``curated_geo.feature`` as ``global:establishment`` that no indicator reads,
    inflating the layer a planner inspects and the tiles it serves, to add
    nothing. A category that later earns an indicator gets added here with it.
    """
    seen = {c for cats in OVERTURE_AMENITY_CATEGORIES.values() for c in cats}
    return tuple(sorted(seen))
