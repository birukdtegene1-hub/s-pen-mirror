"""Geometry for cutting native PDF ink paths with a swept round eraser."""

import math


def _band(start, rate, low, high):
    if abs(rate) < 1e-9:
        return (0, 1) if low <= start <= high else (1, 0)
    first, last = sorted(((low - start) / rate, (high - start) / rate))
    return max(0, first), min(1, last)


def _circle(origin, vector, center, radius):
    x, y = origin[0] - center[0], origin[1] - center[1]
    dx, dy = vector
    a = dx * dx + dy * dy
    c = x * x + y * y - radius * radius
    if a < 1e-12:
        return (0, 1) if c <= 0 else (1, 0)
    b = x * dx + y * dy
    determinant = b * b - a * c
    if determinant < 0:
        return 1, 0
    half = math.sqrt(determinant)
    return max(0, (-b - half) / a), min(1, (-b + half) / a)


def _capsule(origin, vector, start, end, radius):
    yield _circle(origin, vector, start, radius)
    if start == end:
        return
    yield _circle(origin, vector, end, radius)
    sx, sy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(sx, sy)
    ux, uy = sx / length, sy / length
    x, y = origin[0] - start[0], origin[1] - start[1]
    dx, dy = vector
    along = _band(x * ux + y * uy, dx * ux + dy * uy, 0, length)
    across = _band(x * uy - y * ux, dx * uy - dy * ux, -radius, radius)
    yield max(along[0], across[0]), min(along[1], across[1])


def erase_paths(paths, sweeps, radius, whole=False):
    """Keep exact centerline segments outside the eraser and their stroke grouping."""
    cuts = []
    for start, end in sweeps:
        cuts.append((start, end, min(start[0], end[0]) - radius, min(start[1], end[1]) - radius,
                     max(start[0], end[0]) + radius, max(start[1], end[1]) + radius))
    output = []
    changed = False
    for path in paths:
        run = []
        for first, last in zip(path, path[1:] or path):
            dx, dy = last[0] - first[0], last[1] - first[1]
            left, right = sorted((first[0], last[0]))
            bottom, top = sorted((first[1], last[1]))
            intervals = sorted((lo, hi) for start, end, x0, y0, x1, y1 in cuts
                               if left <= x1 and right >= x0 and bottom <= y1 and top >= y0
                               for lo, hi in _capsule(first, (dx, dy), start, end, radius) if lo < hi)
            if whole and intervals:
                return ()
            changed |= bool(intervals)
            cursor = 0
            for lo, hi in [*intervals, (1, 1)]:
                if lo > cursor:
                    start = first if cursor == 0 else (first[0] + dx * cursor, first[1] + dy * cursor)
                    end = last if lo == 1 else (first[0] + dx * lo, first[1] + dy * lo)
                    if run and run[-1] != start:
                        output.append(tuple(run))
                        run = []
                    if not run:
                        run.append(start)
                    run.append(end)
                cursor = max(cursor, hi)
                if hi > lo and run:
                    output.append(tuple(run))
                    run = []
        if run:
            output.append(tuple(run))
    return tuple(output) if changed else paths
