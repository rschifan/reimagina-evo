"""Unit tests for the Hamilton indicator dataflow — pure, no DB."""

from __future__ import annotations

from reimagina.indicators._hamilton_driver import build_indicator_driver


def test_compactness_result_frame_maps_kernel_over_rows():
    dr = build_indicator_driver()
    # area 100 m², perimeter 40 m → 4π·A/P² = 4π·100/1600 ≈ 0.785; degenerate → 0.
    rows = [("f1", 100.0, 40.0), ("f2", 0.0, 0.0)]
    frame = dr.execute(["compactness_result_frame"], inputs={"geodesic_metrics": rows})[
        "compactness_result_frame"
    ]
    values = dict(frame.itertuples(index=False, name=None))
    assert abs(values["f1"] - 0.7853981633974483) < 1e-9
    assert values["f2"] == 0.0


def test_density_result_frame_maps_kernel_over_rows():
    dr = build_indicator_driver()
    rows = [("b1", 5, 10_000.0), ("b2", 0, 10_000.0)]  # 5 buildings / 1 ha = 5.0
    frame = dr.execute(["density_result_frame"], inputs={"centroid_counts": rows})[
        "density_result_frame"
    ]
    values = dict(frame.itertuples(index=False, name=None))
    assert values["b1"] == 5.0
    assert values["b2"] == 0.0


def test_result_frame_has_the_write_path_columns():
    dr = build_indicator_driver()
    frame = dr.execute(["compactness_result_frame"], inputs={"geodesic_metrics": []})[
        "compactness_result_frame"
    ]
    assert list(frame.columns) == ["feature_id", "value_numeric"]
