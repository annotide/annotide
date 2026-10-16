"""Mask -> polygon for segmenters (ML-7), without OpenCV."""

from __future__ import annotations

import numpy as np
import pytest

from app.backends.masks import mask_to_polygon, select_region, simplify, trace_outline


def _disc(height: int, width: int, cx: int, cy: int, radius: int) -> np.ndarray:
    yy, xx = np.mgrid[:height, :width]
    return np.asarray((xx - cx) ** 2 + (yy - cy) ** 2 < radius**2)


def test_a_rectangle_becomes_its_four_corners() -> None:
    mask = np.zeros((10, 10), dtype=bool)
    mask[2:5, 3:7] = True
    assert mask_to_polygon(mask) == [(3.5, 2.5), (6.5, 2.5), (6.5, 4.5), (3.5, 4.5)]


def test_scales_map_mask_pixels_to_image_pixels() -> None:
    mask = np.zeros((10, 10), dtype=bool)
    mask[2:5, 3:7] = True
    polygon = mask_to_polygon(mask, scale_x=2.0, scale_y=4.0)
    assert polygon[0] == (7.0, 10.0)


def test_a_disc_is_simplified_and_bounded() -> None:
    polygon = mask_to_polygon(_disc(600, 800, 400, 300, 100))
    assert 8 <= len(polygon) <= 200
    xs, ys = [x for x, _ in polygon], [y for _, y in polygon]
    assert (min(xs), min(ys)) == pytest.approx((301.5, 201.5), abs=1)
    assert (max(xs), max(ys)) == pytest.approx((499.5, 399.5), abs=1)


def test_max_points_caps_the_vertex_count() -> None:
    assert len(mask_to_polygon(_disc(600, 800, 400, 300, 250), max_points=12)) <= 12


def test_holes_are_ignored() -> None:
    disc = _disc(200, 200, 100, 100, 80)
    ring = disc & ~_disc(200, 200, 100, 100, 40)
    assert mask_to_polygon(ring) == mask_to_polygon(disc)


def test_the_largest_region_wins_unless_one_is_preferred() -> None:
    mask = _disc(200, 200, 120, 120, 50)
    mask[5:15, 5:15] = True
    large = select_region(mask)
    assert large[120, 120] and not large[10, 10]
    small = select_region(mask, prefer=(10, 10))
    assert small[10, 10] and not small[120, 120]
    # A preference outside every region falls back to the largest.
    assert select_region(mask, prefer=(199, 0)).sum() == large.sum()


def test_diagonal_neighbours_are_one_region() -> None:
    assert select_region(np.eye(6, dtype=bool)).sum() == 6


def test_degenerate_masks_give_no_polygon() -> None:
    assert mask_to_polygon(np.zeros((5, 5), dtype=bool)) == []
    one = np.zeros((5, 5), dtype=bool)
    one[2, 2] = True
    assert trace_outline(one) == [(2, 2)]
    assert mask_to_polygon(one) == []
    line = np.zeros((5, 9), dtype=bool)
    line[2, 1:8] = True
    assert mask_to_polygon(line) == []


def test_simplify_keeps_corners_and_drops_collinear_points() -> None:
    square = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert simplify(square, 0.5) == [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
