"""The Overture amenity vocabulary, and the rules that keep it honest.

These tests exist because every failure this module guards against is silent.
A category mapped to nothing produces a flat choropleth that reads as a
measurement; a category mapped too widely produces a varied choropleth that is
wrong by construction. Neither raises.
"""

from __future__ import annotations

import pytest

from reimagina.data import amenities as amen


class TestAllowlistsAreDisjointFromExclusions:
    def test_no_category_is_both_counted_and_excluded(self):
        """The two lists must never name the same category.

        They were written in one sitting from the same inventory, so the risk
        is not a typo — it is someone later adding ``beach`` to the park list
        without noticing it is already recorded as deliberately excluded,
        leaving the repository with two contradictory statements and no error.
        """
        counted = set(amen.overture_categories_to_load())
        excluded = set(amen.OVERTURE_EXCLUDED_LOOKALIKES)
        assert counted & excluded == set()

    def test_park_and_culture_do_not_overlap(self):
        """A destination counted twice would make two indicators correlated
        for a reason that is an authoring slip rather than a fact about the
        city."""
        park = set(amen.OVERTURE_PARK_CATEGORIES)
        culture = set(amen.OVERTURE_CULTURE_CATEGORIES)
        assert park & culture == set()

    def test_every_exclusion_carries_a_reason(self):
        for category, reason in amen.OVERTURE_EXCLUDED_LOOKALIKES.items():
            assert reason.strip(), f"{category} is excluded with no reason given"

    def test_categories_to_load_is_the_union(self):
        assert set(amen.overture_categories_to_load()) == set(
            amen.OVERTURE_PARK_CATEGORIES
        ) | set(amen.OVERTURE_CULTURE_CATEGORIES)

    def test_categories_to_load_is_sorted_and_deduplicated(self):
        loaded = amen.overture_categories_to_load()
        assert list(loaded) == sorted(set(loaded))


class TestNameExclusions:
    # Every name here appeared in the real Mexicali extract at release
    # 2026-10-21.0 and was tagged with a counted category.
    @pytest.mark.parametrize(
        "name",
        [
            "Parque Industrial el Dorado",
            'Parque Industrial "El Sahuaro"',
            "Valle de Puebla Parque Industrial",
            "Sahuaro Industrial Park",
            "Dhl Parque Industrial Nelzon",
            "Parque Industrial Mexicali, S.A. de C.V.",
        ],
    )
    def test_industrial_estates_are_dropped(self, name):
        """An industrial estate tagged ``park`` is not noise spread evenly —
        it plants a fake destination in the middle of the districts whose park
        access is the question, and reports them as well served."""
        assert amen.excluded_by_name(name) is not None

    @pytest.mark.parametrize(
        "name",
        [
            'Jardin de eventos "El Rancho"',
            "Jardín de eventos Verdugo",
            "Jardín para eventos Lyn",
            "Las 5 Flores jardin de eventos",
            "Salon de Eventos Rancho Viejo",
            "El Rincón del Caudal jardín de eventos",
        ],
    )
    def test_event_rental_venues_are_dropped(self, name):
        """Private, ticketed and closed most days — not public open space.
        Note the accented and unaccented spellings both appear upstream."""
        assert amen.excluded_by_name(name) is not None

    def test_cemeteries_are_dropped(self):
        assert amen.excluded_by_name("El Angel Funeraria y Parque Memorial")

    # The other half of the contract, and the more important one: an exclusion
    # rule that quietly eats real parks is worse than no rule.
    @pytest.mark.parametrize(
        "name",
        [
            "Parque Acuatico Chorilandia",
            "El Barco Parque Acuatico",
            "Los Delfines Parque Acuatico",
            "Parque Acuatico Los Delfines Splash",
            'Parque Infantil "arcoiris"',
            "Parque Infantil Orquídeas",
            "Parque Infantil Disney",
            "Parque Infantil Bugambilias",
            "AQUA PARK",
            "Ejido Hidalgo Park",
            "Hacienda Del Bosque Public Park",
            "John F. Kennedy Park",
            "Centro Acuático Ciudad Deportiva",
            "Complejo Acuático UABC",
        ],
    )
    def test_real_recreation_sites_survive(self, name):
        assert amen.excluded_by_name(name) is None, (
            f"{name!r} is a real recreation site and the exclusion rules ate it"
        )

    def test_reason_is_returned_not_a_boolean(self):
        """The ingest logs the reason, so a wrong rule is visible by name
        rather than hidden in a count."""
        reason = amen.excluded_by_name("Parque Industrial Nicoya")
        assert isinstance(reason, str)
        assert "industrial" in reason.lower()

    @pytest.mark.parametrize("name", ["", None, "   "])
    def test_blank_names_are_not_excluded(self, name):
        assert amen.excluded_by_name(name) is None


class TestFoldName:
    def test_strips_accents_and_lowercases(self):
        assert amen.fold_name("Jardín Público") == "jardin publico"

    def test_handles_none(self):
        assert amen.fold_name(None) == ""

    def test_both_spellings_fold_together(self):
        """The whole reason folding exists: two contributors, one venue."""
        assert amen.fold_name("Jardín de Eventos") == amen.fold_name(
            "Jardin de eventos"
        )
