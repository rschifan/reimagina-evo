"""Each INEGI product registers the credit line INEGI's terms ask for:
"Fuente: INEGI, <producto>, <fecha>" — the map shows it once per product."""

from __future__ import annotations

from reimagina.data.attribution import attribution_text
from reimagina.pipeline import cdmx, inegi_censo, inegi_marco, points


def test_inegi_products_carry_their_own_credit_line() -> None:
    assert inegi_marco.INEGI_MARCO_CREDIT == "Fuente: INEGI, Marco Geoestadístico 2024"
    assert (
        inegi_censo.CENSO_CREDIT == "Fuente: INEGI, Censo de Población y Vivienda 2020"
    )
    assert points.MEXICALI_AMENITIES.credit == (
        "Fuente: INEGI, Directorio Estadístico Nacional de Unidades Económicas "
        "(DENUE), 05/2025"
    )
    assert cdmx.CDMX_MARCO_CREDIT == "Fuente: INEGI, Marco Geoestadístico 2020"


def test_a_declared_credit_replaces_the_provider_fallback() -> None:
    credit = "Fuente: INEGI, Marco Geoestadístico 2024"
    assert attribution_text("INEGI", "Libre uso", credit) == credit


def test_no_credit_is_invented_for_other_providers() -> None:
    assert points.TLALPAN_AMENITIES.credit is None
    assert attribution_text("SIG-CDMX", "Datos abiertos de la CDMX") == (
        "SIG-CDMX (Datos abiertos de la CDMX)"
    )
