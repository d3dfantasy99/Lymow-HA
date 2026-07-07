"""Lymow zone stats — attribute coverage and obstacle events to zones.

There is no per-zone breakdown in the cloud telemetry (cleanZoneIds/mowOrder are
NOT present), so attribution is GEOMETRIC: a cut point or obstacle-event center is
assigned to whichever zone polygon contains it (ray-casting point-in-polygon, ENU
metres). This gives per-zone coverage and obstacle counts that the sticky-zone /
completion model (zone_stats stage 2) and per-zone entities build on.

Honest limits (see ZONE_ANALYTICS_DESIGN.md): coverage = accumulated cut points,
not a ground-truth swath; overlapping zones attribute a point to the first match.
"""
from __future__ import annotations

from typing import Any


def point_in_polygon(px: float, py: float, poly: list) -> bool:
    """Ray-casting point-in-polygon test. `poly` = list of (x, y) in ENU metres."""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        # Does the horizontal ray from (px,py) cross edge (i,j)?
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _zone_poly(zone: dict) -> list:
    """Extract a clean [(x, y), …] polygon from a zone dict, or [] if unusable."""
    out: list[tuple[float, float]] = []
    for p in (zone.get("points") or []):
        try:
            if isinstance(p, dict):
                out.append((float(p["x"]), float(p["y"])))
            else:
                out.append((float(p[0]), float(p[1])))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def _zone_key(zone: dict, idx: int) -> str:
    return zone.get("hashId") or zone.get("name") or f"zone_{idx}"


def _build_polys(zones: list[dict]) -> list:
    """[(key, name, polygon), …] for zones with a usable (≥3 pt) polygon."""
    out = []
    for idx, z in enumerate(zones):
        poly = _zone_poly(z)
        if len(poly) >= 3:
            out.append((_zone_key(z, idx), z.get("name") or _zone_key(z, idx), poly))
    return out


# NOTE: the live sticky CURRENT-ZONE model already exists (state.derive_current_zone
# + coordinator currentZone + the current_zone sensor), and per-zone COMPLETION
# history already exists (coordinator cleanReport → zone_history, keyed on the real
# cleanZoneIds at cleanReport.cleanInfo.areaInfo, mowEndType decoded). This module's
# only addition is the geometric per-zone COVERAGE/OBSTACLE density below, which
# those don't provide.


def assign_to_zones(
    zones: list[dict],
    cut_points: list,
    obstacle_events: list[dict] | None = None,
) -> dict[str, dict[str, Any]]:
    """Attribute cut points and obstacle events to zones by point-in-polygon.

    Returns {zone_key: {name, coverage_points, obstacle_count, covered_m2}}.
    covered_m2 = the real per-zone mowed footprint: the pose-trail points inside the zone
    rasterised to a swath-dilated cell grid (unique cells × cell²). This is per-zone and
    differs by zone — unlike the cloud cleanArea, which is a single session total.
    A point/event outside every zone is dropped (transit through channels).
    """
    obstacle_events = obstacle_events or []
    polys = _build_polys(zones)
    # Pre-compute each zone's bbox so a point far from a zone is rejected with 4 comparisons
    # instead of a full point-in-polygon ray-cast. Identical result (a point inside the
    # polygon is always inside its bbox); this is the per-pull hot path over the breadcrumb.
    polys_bb = [(key, name, poly,
                 min(p[0] for p in poly), min(p[1] for p in poly),
                 max(p[0] for p in poly), max(p[1] for p in poly))
                for key, name, poly in polys]

    # Cell grid for per-zone area: 0.25 m cells, ±1 cell dilation ≈ the 16 in (0.41 m) cut.
    import math
    CELL = 0.25
    DIL = 1
    cells: dict[str, set] = {key: set() for key, _n, _p in polys}

    stats: dict[str, dict[str, Any]] = {
        key: {"name": name, "coverage_points": 0, "obstacle_count": 0}
        for key, name, _poly in polys
    }

    for pt in cut_points:
        try:
            px, py = float(pt[0]), float(pt[1])
        except (IndexError, TypeError, ValueError):
            continue
        for key, _name, poly, x0, y0, x1, y1 in polys_bb:
            if px < x0 or px > x1 or py < y0 or py > y1:
                continue
            if point_in_polygon(px, py, poly):
                stats[key]["coverage_points"] += 1
                cx0 = int(math.floor(px / CELL)); cy0 = int(math.floor(py / CELL))
                zc = cells[key]
                for dx in range(-DIL, DIL + 1):
                    for dy in range(-DIL, DIL + 1):
                        zc.add((cx0 + dx, cy0 + dy))
                break  # first containing zone wins (overlap → arbitrary but stable)

    for ev in obstacle_events:
        center = ev.get("center")
        if not center:
            continue
        try:
            cx, cy = float(center[0]), float(center[1])
        except (IndexError, TypeError, ValueError):
            continue
        for key, _name, poly in polys:
            if point_in_polygon(cx, cy, poly):
                stats[key]["obstacle_count"] += 1
                break

    for key, _n, _p in polys:
        stats[key]["covered_m2"] = round(len(cells[key]) * CELL * CELL, 1)

    return stats
