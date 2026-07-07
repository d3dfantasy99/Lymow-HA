"""Double-coverage / single-pass analysis for checkerboard mows.

A crosshatch ("chess") mow should cover every spot TWICE — once per infill axis.
Where the interior is covered by only ONE infill axis, Lymow's planner abandoned the
perpendicular return pass there (a real, submittable path-planning bug).

Key design (learned the hard way 2026-06-04): measure double-coverage on the two INFILL
axes ONLY. Perimeter-following laps are inherently one-directional, so if we counted them
the whole boundary ring reads "single-pass". By excluding perimeter rows, the ring falls
out as "covered by neither infill axis" and is correctly ignored — with NO erosion, so
narrow tails/appendages (where real give-ups also happen) survive.
"""
from __future__ import annotations

import math

from .map_tuning import (
    MISSED_CELL_M, MISSED_RING_MIN_M2, MOWED_FRAC, SINGLE_RING_MIN_M2, TURNAROUND_EXCL_M,
)


def _pip(px, py, poly) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def _dist_pt_seg(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    if d2 <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / d2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _seg_d2(px, py, ax, ay, bx, by) -> float:
    """SQUARED point-to-segment distance (no sqrt) — for threshold comparisons."""
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    if d2 <= 1e-12:
        ex, ey = px - ax, py - ay
        return ex * ex + ey * ey
    t = ((px - ax) * dx + (py - ay) * dy) / d2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    ex, ey = px - (ax + t * dx), py - (ay + t * dy)
    return ex * ex + ey * ey


def _poly_bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _near_boundary(pt, polys_bb, dist) -> bool:
    """True if pt is within `dist` of any zone edge. `polys_bb` = [(poly, bbox), …] with
    PRE-COMPUTED bboxes so far-away zones are skipped before touching their edges, and the
    comparison is on squared distance (no per-edge sqrt). Identical result to the naive
    all-edges scan, just O(nearby-edges) instead of O(all-edges-of-all-zones)."""
    px, py = pt
    d2 = dist * dist
    for poly, (x0, y0, x1, y1) in polys_bb:
        if px < x0 - dist or px > x1 + dist or py < y0 - dist or py > y1 + dist:
            continue                                  # point can't be within dist of this zone
        n = len(poly)
        for i in range(n):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % n]
            if _seg_d2(px, py, ax, ay, bx, by) <= d2:
                return True
    return False


def tag_perimeter_infill(rows, zone_polys, perim_dist: float = 0.7, frac: float = 0.6):
    """Tag each row dict with kind = 'perimeter' | 'infill'.

    A row is 'perimeter' if most (>= frac) of its points hug a zone boundary edge
    (within perim_dist metres) — i.e. it traces the edge rather than crossing the
    interior in a straight boustrophedon line.
    """
    polys_bb = [(p, _poly_bbox(p)) for p in zone_polys if len(p) >= 2]
    for row in rows:
        pts = row.get("pts") or []
        if not pts:
            row["kind"] = "infill"
            continue
        near = sum(1 for p in pts if _near_boundary(p, polys_bb, perim_dist))
        row["kind"] = "perimeter" if near >= frac * len(pts) else "infill"
    return rows


def _mark_row(cells, pts, cell, dil):
    """Rasterise a row's swath into the cell set (dense interpolation + square dilation)."""
    step = cell * 0.5
    for k in range(1, len(pts)):
        (x0, y0), (x1, y1) = pts[k - 1], pts[k]
        seg = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(seg / step))
        for s in range(n + 1):
            x = x0 + (x1 - x0) * s / n
            y = y0 + (y1 - y0) * s / n
            cx0 = int(math.floor(x / cell))
            cy0 = int(math.floor(y / cell))
            for dx in range(-dil, dil + 1):
                for dy in range(-dil, dil + 1):
                    cells.add((cx0 + dx, cy0 + dy))


def _turnaround_points(track, w: int = 4, rev_deg: float = 150.0):
    """Points where the trail REVERSES (a U-turn at a row end). The mower lays overlapping
    swaths through the pivot, so a single-pass 'skip' or an un-mowed sliver detected right
    at one is a heading-classification artifact, not a real miss. (Validated 2026-06-05 vs
    Nate's ground truth: the false 6.2 m² corner skip sits 0.7 m from a turn, the real
    2.2 m² interior skip 1.56 m away.)"""
    out = []
    n = len(track)
    th = math.radians(rev_deg)
    for i in range(w, n - w):
        h0 = math.atan2(track[i][1] - track[i - w][1], track[i][0] - track[i - w][0])
        h1 = math.atan2(track[i + w][1] - track[i][1], track[i + w][0] - track[i][0])
        d = abs(h0 - h1) % (2 * math.pi)
        if min(d, 2 * math.pi - d) > th:
            out.append(track[i])
    return out


def _heading_axis(track, i, w):
    """Local heading axis at point i: 'H' or 'V' from the displacement across a
    ±w window. Using the raw trail (NOT segment_rows) means turn arcs are kept and,
    because the heading rotates through a U-turn, the turn patch contributes to BOTH
    axes — so it reads as double-covered instead of a false single-pass give-up."""
    a = max(0, i - w)
    b = min(len(track) - 1, i + w)
    dx = track[b][0] - track[a][0]
    dy = track[b][1] - track[a][1]
    return "H" if abs(dx) >= abs(dy) else "V"


def analyze_pass_coverage(track, zone_polys, nogo_polys, obstacles=None, double_polys=None,
                          cell: float = 0.25, swath: float = 0.5,
                          min_cluster_m2: float = SINGLE_RING_MIN_M2, heading_w: int = 5,
                          max_clusters: int = 30):
    """Returns {double_pct, infill_area_m2, single_pass_m2, clusters:[{center,area_m2,covered_by}]}.

    RAW-HEADING method: classify every breadcrumb point by its local heading (turn arcs
    included), build the two axis-coverage masks from the full trail, and call a cell
    single-pass only where exactly one axis reached it. This avoids the segment_rows
    turn-arc trimming that manufactured false give-ups at every turnaround.
    Detected obstacles are excluded — the mower routes AROUND them, that isn't a give-up.
    """
    if not track or len(track) < 20 or not zone_polys:
        return None
    track = [(float(p[0]), float(p[1])) for p in track]
    axes = [_heading_axis(track, i, heading_w) for i in range(len(track))]
    pass1 = "H" if axes.count("H") >= len(axes) - axes.count("H") else "V"
    # Single-pass / double-coverage uses the COARSE footprint (~0.75 m) — the right scale for
    # "did the two passes overlap". MISSED detection below uses its own FINER grid (~0.45 m,
    # matching the render swath) so it surfaces missing-pass gaps without making single-pass
    # noisy. (Two questions, two footprints.)
    dil = max(1, int(round((swath * 0.5) / cell)))
    A, B = set(), set()
    for (x, y), ax in zip(track, axes):
        cset = A if ax == pass1 else B
        cx0 = int(math.floor(x / cell)); cy0 = int(math.floor(y / cell))
        for dx in range(-dil, dil + 1):
            for dy in range(-dil, dil + 1):
                cset.add((cx0 + dx, cy0 + dy))

    # Per-zone interior (COARSE grid) — keep only zones actually MOWED (>=30% covered), and
    # remember which polys were mowed so the MISSED pass (finer grid below) only looks inside
    # them (a deliberately-skipped zone isn't one giant miss).
    AB = A | B
    inside = set()
    mowed_polys = []
    for poly in zone_polys:
        if len(poly) < 3:
            continue
        zin = set()
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        for cx in range(int(math.floor(min(xs) / cell)), int(math.ceil(max(xs) / cell)) + 1):
            for cy in range(int(math.floor(min(ys) / cell)), int(math.ceil(max(ys) / cell)) + 1):
                px, py = (cx + 0.5) * cell, (cy + 0.5) * cell
                if _pip(px, py, poly) and not any(_pip(px, py, ng) for ng in nogo_polys):
                    zin.add((cx, cy))
        if zin and len(zin & AB) / len(zin) >= MOWED_FRAC:   # zone was actually mowed
            inside |= zin
            mowed_polys.append(poly)

    if not inside:
        return None
    covered = inside & AB
    both = inside & A & B
    single = covered - both        # one pass, not the cross-pass (COARSE footprint)
    if not covered:
        return None

    # MISSED on a FINER grid (~0.45 m, matching the render swath) so a single missing pass
    # shows up instead of being bridged by the coarse footprint. Only inside MOWED zones.
    MCELL, MDIL = MISSED_CELL_M, 1
    # Rasterise the trail as interpolated LINES (like the render swath), NOT discrete points —
    # otherwise the gaps between breadcrumb samples read as a scatter of false misses. _mark_row
    # densifies each segment so along-pass coverage is continuous and only real gaps remain.
    cov_fine = set()
    _mark_row(cov_fine, track, MCELL, MDIL)
    missed = set()
    for poly in mowed_polys:
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        for cx in range(int(math.floor(min(xs) / MCELL)), int(math.ceil(max(xs) / MCELL)) + 1):
            for cy in range(int(math.floor(min(ys) / MCELL)), int(math.ceil(max(ys) / MCELL)) + 1):
                if (cx, cy) in cov_fine:
                    continue
                px, py = (cx + 0.5) * MCELL, (cy + 0.5) * MCELL
                if _pip(px, py, poly) and not any(_pip(px, py, ng) for ng in nogo_polys):
                    missed.add((cx, cy))

    # Exclude detected-obstacle footprints (mower routed around them) from BOTH grids.
    for o in (obstacles or []):
        ctr = o.get("center") or (); fp = o.get("footprint_m") or ()
        if len(ctr) != 2 or len(fp) != 2:
            continue
        ocx, ocy = ctr; fw, fh = fp
        for cx in range(int(math.floor((ocx - fw) / cell)), int(math.ceil((ocx + fw) / cell)) + 1):
            for cy in range(int(math.floor((ocy - fh) / cell)), int(math.ceil((ocy + fh) / cell)) + 1):
                single.discard((cx, cy))
        for cx in range(int(math.floor((ocx - fw) / MCELL)), int(math.ceil((ocx + fw) / MCELL)) + 1):
            for cy in range(int(math.floor((ocy - fh) / MCELL)), int(math.ceil((ocy + fh) / MCELL)) + 1):
                missed.discard((cx, cy))

    # Single-pass is only a DEFECT in CHESS/double zones (a cross pass was expected there). If we
    # know which zones are double (from the per-zone cleanMode), restrict single-pass flags to
    # them — single-pass zones (ZIGZAG) intentionally make one pass, so only MISSED applies there.
    # Fall back to the global heading-ratio guard only when cleanMode wasn't supplied.
    if double_polys is not None:
        double_cells = set()
        for poly in double_polys:
            if len(poly) < 3:
                continue
            xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
            for cx in range(int(math.floor(min(xs) / cell)), int(math.ceil(max(xs) / cell)) + 1):
                for cy in range(int(math.floor(min(ys) / cell)), int(math.ceil(max(ys) / cell)) + 1):
                    if _pip((cx + 0.5) * cell, (cy + 0.5) * cell, poly):
                        double_cells.add((cx, cy))
        single &= double_cells
        single_dir = not double_polys      # no double zones at all = effectively single-pass mow
        # double_pct over ONLY the checker zones: of the area meant to get a 2nd pass, how much
        # did. 100% = perfect cross-cut; <100% = real Lymow planner misses. Single-pass zones
        # (intentional) no longer drag it down.
        _chess_cov = covered & double_cells
        double_pct = round(100.0 * len(both & double_cells) / len(_chess_cov), 1) if _chess_cov else None
    else:
        double_pct = round(100.0 * len(both) / len(covered), 1) if covered else None
        a_area = len(inside & A); b_area = len(inside & B)
        minor, major = sorted((a_area, b_area))
        single_dir = (major == 0 or (minor / major) < 0.20)
        if single_dir:
            single = set()

    turns = _turnaround_points(track)
    TURN_EXCL_M2 = TURNAROUND_EXCL_M ** 2

    def _cluster(cells, kind, csize, min_area, drop_turn):
        out = []
        rem = set(cells)
        while rem:
            start = rem.pop(); comp = [start]; stk = [start]
            while stk:
                c = stk.pop()
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nb = (c[0] + dx, c[1] + dy)
                        if nb in rem:
                            rem.discard(nb); comp.append(nb); stk.append(nb)
            area = len(comp) * csize * csize
            if area < min_area:
                continue
            gx = sum(c[0] for c in comp) / len(comp) * csize + csize / 2
            gy = sum(c[1] for c in comp) / len(comp) * csize + csize / 2
            # Single-pass clusters at a U-turn pivot are heading artifacts → drop. MISSED
            # (un-covered) clusters are real regardless, so they are NOT dropped at turns.
            if drop_turn and any((gx - tx) ** 2 + (gy - ty) ** 2 < TURN_EXCL_M2 for tx, ty in turns):
                continue
            if kind == "single":
                sample = comp[len(comp) // 2]
                cov = pass1 if sample in A else ("V" if pass1 == "H" else "H")
            else:
                cov = "none"
            out.append({"center": (round(gx, 2), round(gy, 2)),
                        "area_m2": round(area, 1), "covered_by": cov, "kind": kind})
        return out

    # MISSED (fine grid, the truth) first, then single-pass (coarse, under-covered) secondary.
    clusters = (_cluster(missed, "missed", MCELL, MISSED_RING_MIN_M2, drop_turn=False)
                + _cluster(single, "single", cell, min_cluster_m2, drop_turn=True))
    clusters.sort(key=lambda c: -c["area_m2"])
    missed_clusters = [c for c in clusters if c["kind"] == "missed"]
    return {
        "double_pct": double_pct,   # over CHESS zones only (when cleanMode known); see above
        "single_direction": single_dir,
        "infill_area_m2": round(len(covered) * cell * cell, 1),
        "single_pass_m2": round(len(single) * cell * cell, 1),
        "missed_m2": round(len(missed) * MCELL * MCELL, 1),
        "missed_count": len(missed_clusters),                      # distinct missed patches (>=0.8 m²)
        "clusters": clusters[:max_clusters],
    }
