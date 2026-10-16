"""Binary mask -> polygon, in numpy alone (no OpenCV, no SciPy).

A segmenter answers with a mask; the platform's `/interactive` contract wants
one polygon. That takes three steps: keep one connected region, trace its
outer boundary, and simplify the trace to a few dozen vertices. Holes are
dropped: a polygon annotation has none.
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np

#: 8-neighbourhood, clockwise on screen (y grows downwards), starting east.
_DIRECTIONS: Final = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
_DIRECTION_INDEX: Final = {delta: index for index, delta in enumerate(_DIRECTIONS)}


def _row_runs(row: np.ndarray) -> list[tuple[int, int]]:
    """`(start, end)` column spans of a boolean row, end exclusive."""
    padded = np.concatenate(([False], row, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(edges[i]), int(edges[i + 1])) for i in range(0, len(edges), 2)]


def select_region(mask: np.ndarray, prefer: tuple[int, int] | None = None) -> np.ndarray:
    """One 8-connected region of `mask`: the one holding `prefer` (x, y), else the largest.

    Labels row runs with a union-find instead of visiting pixels one by one,
    so it stays fast in pure Python at the sizes a click produces.
    """
    runs: list[tuple[int, int, int]] = []  # (row, start, end)
    parent: list[int] = []

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    previous: list[int] = []
    for y in range(mask.shape[0]):
        current: list[int] = []
        for start, end in _row_runs(mask[y]):
            index = len(runs)
            runs.append((y, start, end))
            parent.append(index)
            for other in previous:
                _, other_start, other_end = runs[other]
                # Diagonal neighbours touch too: spans overlap when widened by one.
                if other_start <= end and start <= other_end:
                    parent[find(index)] = find(other)
            current.append(index)
        previous = current

    if not runs:
        return np.zeros_like(mask, dtype=bool)
    areas: dict[int, int] = {}
    for index, (_, start, end) in enumerate(runs):
        root = find(index)
        areas[root] = areas.get(root, 0) + end - start
    chosen = max(areas, key=areas.__getitem__)
    if prefer is not None:
        px, py = prefer
        for index, (y, start, end) in enumerate(runs):
            if y == py and start <= px < end:
                chosen = find(index)
                break

    region = np.zeros_like(mask, dtype=bool)
    for index, (y, start, end) in enumerate(runs):
        if find(index) == chosen:
            region[y, start:end] = True
    return region


def trace_outline(region: np.ndarray) -> list[tuple[int, int]]:
    """The outer boundary pixels of one connected region, in order (Moore tracing)."""
    ys, xs = np.nonzero(region)
    if len(ys) == 0:
        return []
    # Pad by one so every neighbour lookup stays in bounds.
    grid = np.pad(region, 1)
    start = (int(xs[0]) + 1, int(ys[0]) + 1)  # topmost, then leftmost: on the outer edge
    outline = [start]
    current = start
    backtrack = 4  # entered from the west, which is background for the leftmost pixel
    second: tuple[int, int] | None = None
    # A boundary visits each pixel at most four times; past that, something is wrong.
    for _ in range(4 * int(region.sum()) + 8):
        found = None
        for step in range(1, 9):
            direction = (backtrack + step) % 8
            dx, dy = _DIRECTIONS[direction]
            candidate = (current[0] + dx, current[1] + dy)
            if grid[candidate[1], candidate[0]]:
                found = candidate
                # The last background pixel checked, seen from the new pixel.
                bx, by = _DIRECTIONS[(direction - 1) % 8]
                behind = (current[0] + bx - candidate[0], current[1] + by - candidate[1])
                backtrack = _DIRECTION_INDEX[behind]
                break
        if found is None:  # a single isolated pixel
            break
        if current == start and found == second:
            break  # Jacob's criterion: back at the start, about to repeat the loop
        if second is None:
            second = found
        current = found
        outline.append(current)
    if len(outline) > 1 and outline[-1] == start:
        outline.pop()
    return [(x - 1, y - 1) for x, y in outline]


def _distance_to_segment(
    point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    (px, py), (ax, ay), (bx, by) = point, a, b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def simplify(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker on a closed ring; keeps at least a triangle when it can."""
    if len(points) <= 3:
        return list(points)
    # Split the ring at the vertex farthest from the first, then simplify both chains.
    first = points[0]
    far = max(
        range(len(points)),
        key=lambda i: (points[i][0] - first[0]) ** 2 + (points[i][1] - first[1]) ** 2,
    )
    ring = [*points, first]
    keep = [False] * len(ring)
    keep[0] = keep[far] = keep[-1] = True
    stack = [(0, far), (far, len(ring) - 1)]
    while stack:
        lo, hi = stack.pop()
        best, best_distance = -1, epsilon
        for i in range(lo + 1, hi):
            distance = _distance_to_segment(ring[i], ring[lo], ring[hi])
            if distance > best_distance:
                best, best_distance = i, distance
        if best >= 0:
            keep[best] = True
            stack.extend(((lo, best), (best, hi)))
    kept = [ring[i] for i in range(len(points)) if keep[i]]
    if len(kept) < 3:
        # A sliver collapsed to a segment: keep the original's extreme points.
        third = max(
            range(len(points)),
            key=lambda i: _distance_to_segment(points[i], points[0], points[far]),
        )
        kept = [points[i] for i in sorted({0, far, third})]
    return kept


def mask_to_polygon(
    mask: np.ndarray,
    *,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    prefer: tuple[int, int] | None = None,
    max_points: int = 200,
) -> list[tuple[float, float]]:
    """One polygon for `mask`, in the coordinates `mask` pixels map to via the scales.

    Vertices sit on pixel centres. Tolerance starts at one mask pixel and
    doubles until the polygon has at most `max_points` vertices.
    """
    region = select_region(mask, prefer)
    outline = trace_outline(region)
    if len(outline) < 3:
        return []
    points = [((x + 0.5) * scale_x, (y + 0.5) * scale_y) for x, y in outline]
    epsilon = max(scale_x, scale_y)
    simplified = simplify(points, epsilon)
    while len(simplified) > max_points:
        epsilon *= 2
        simplified = simplify(points, epsilon)
    # A one-pixel line traces to a segment, which is no polygon.
    return simplified if len(simplified) >= 3 else []
