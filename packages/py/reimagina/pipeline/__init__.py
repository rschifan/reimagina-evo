"""Bronze, silver, and curated ingestion flows."""

from .bronze import bronze_uri, land_file
from .loader import load_tabular_source, load_vector_source
from .overture import (
    MEXICO_CITY_BBOX,
    OVERTURE_BUILDINGS_ATTRIBUTES,
    OVERTURE_BUILDINGS_MEXICO_CITY,
    bootstrap_overture_buildings_subset,
    ensure_overture_buildings_source,
    extract_overture_buildings_subset,
    load_overture_buildings_subset,
    overture_buildings_extract_sql,
    overture_theme_type_glob,
    register_overture_buildings_source,
    stage_overture_buildings_subset,
    validate_overture_buildings_layer,
)
from .silver import clean_vector, read_tabular, read_vector, save_silver_tabular, save_silver_vector

__all__ = [
    "bronze_uri",
    "land_file",
    "load_tabular_source",
    "load_vector_source",
    "MEXICO_CITY_BBOX",
    "OVERTURE_BUILDINGS_ATTRIBUTES",
    "OVERTURE_BUILDINGS_MEXICO_CITY",
    "bootstrap_overture_buildings_subset",
    "ensure_overture_buildings_source",
    "extract_overture_buildings_subset",
    "load_overture_buildings_subset",
    "overture_buildings_extract_sql",
    "overture_theme_type_glob",
    "register_overture_buildings_source",
    "stage_overture_buildings_subset",
    "validate_overture_buildings_layer",
    "clean_vector",
    "read_tabular",
    "read_vector",
    "save_silver_tabular",
    "save_silver_vector",
]
