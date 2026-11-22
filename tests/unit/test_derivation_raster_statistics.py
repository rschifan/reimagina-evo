"""Which zonal statistics a raster recipe asks for."""

from __future__ import annotations

import pytest

from reimagina.jobs.derivations import raster_statistics


def test_a_pixel_method_implies_its_statistic():
    assert raster_statistics("sum", None) == ("sum",)


def test_explicit_stats_win():
    assert raster_statistics("weighted_mean", {"stats": ["mean", "count"]}) == (
        "mean",
        "count",
    )


def test_weighted_mean_without_stats_is_refused_with_a_reason():
    """It used to surface as a bare ``KeyError: 'weighted_mean'``."""
    with pytest.raises(ValueError, match="params.stats"):
        raster_statistics("weighted_mean", {})
