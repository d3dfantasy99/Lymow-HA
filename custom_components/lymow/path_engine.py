"""Lymow path engine — cut/planned split, session cut-track accumulation, and
deviation (obstacle-avoidance) detection.

The QUERY_PATH response is one flat point list (PbPoint x/y, ENU metres) split by
sentinel markers into segments. Verified on hardware (2026-06-01):
  * the small segment that tracks the mower and grows each pull = the CUT/ACTUAL path
  * the large static segments that span the zone = the PLANNED route (computed once)
Since planned is static, points where the ACTUAL path departs from it are where the
mower re-routed around an obstacle → deviation = obstacle avoidance.

Concept (per-zone history + heat-map) ported from Mortimer452/Lymow-One-MQTT (MIT).
"""
from __future__ import annotations

import heapq
import math
from typing import Any


def _tri_area(a, b, c) -> float:
    """Twice the triangle area (|cross|) — the Visvalingam "effective area"."""
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))


def simplify_path(points: list, budget: int) -> list:
    """Visvalingam–Whyatt curvature-aware simplification for RENDERING ONLY.

    Keeps the `budget` most significant points — the sharp turns / row-ends (large
    triangle area with their neighbours) — and drops the flattest straight-run points
    (~zero area) first. Unlike uniform [::step] decimation it never cuts a corner: a
    long straight stretch thins to just its endpoints while every direction change is
    preserved. Order-preserving; first and last points are always kept.

    Accepts (x,y) tuples OR telemetry-tagged breadcrumb dicts ({"x","y",...}); returns
    the SAME element objects (with telemetry intact), just fewer of them. This is a
    geometry-only thinning for drawing the path line — it must NOT be used as the source
    for telemetry heatmaps (those read the full, unthinned track and aggregate per cell).
    """
    n = len(points)
    if n <= budget or n < 3 or budget < 2:
        return list(points)

    def xy(p):
        return (p["x"], p["y"]) if isinstance(p, dict) else (p[0], p[1])

    prev = list(range(-1, n - 1))
    nxt = list(range(1, n + 1))
    nxt[-1] = -1
    alive = [True] * n
    ver = [0] * n

    def area_at(i):
        p, nx = prev[i], nxt[i]
        if p < 0 or nx < 0:
            return float("inf")  # endpoints are never removable
        return _tri_area(xy(points[p]), xy(points[i]), xy(points[nx]))

    heap = [(area_at(i), i, 0) for i in range(1, n - 1)]
    heapq.heapify(heap)
    remaining = n
    while remaining > budget and heap:
        a, i, v = heapq.heappop(heap)
        if not alive[i] or v != ver[i]:
            continue
        alive[i] = False
        remaining -= 1
        p, nx = prev[i], nxt[i]
        if p >= 0:
            nxt[p] = nx
        if nx >= 0:
            prev[nx] = p
        for j in (p, nx):
            if 0 < j < n - 1 and alive[j]:
                ver[j] += 1
                heapq.heappush(heap, (area_at(j), j, ver[j]))
    return [points[i] for i in range(n) if alive[i]]

# Planned-route segments are large (hundreds of points) and static; the live CUT
# delta in each QUERY_PATH pull is small (the recent actual-path points, stitched
# by CutAccumulator). Hardware (2026-06-01): chess plateau = [7, 575, 556] (cut=7,
# planned=575+556); single-pass = [575, …] planned. Marker type can't classify
# (all 444/444) and segment ORDER varies, so size is the reliable discriminator.
PLANNED_MIN_POINTS = 60


def _angdiff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def segment_rows(track: list, gap_m: float = 2.0, turn_deg: float = 55.0, min_pts: int = 4):
    """Split an ordered (x,y) coverage track into straight mowing ROWS.

    Breaks at real gaps (>gap_m between samples) AND at U-turns (local bearing swing
    >turn_deg), excluding the turn arcs so each row is a clean straight run. Bearing is
    derived from POSITIONS (no heading field needed — works on restored breadcrumb
    tracks too). Each row is tagged axis ('H'/'V' from its start→end displacement) and a
    direction sign. Returns (rows, pass1_axis) where pass1_axis is the temporally-first
    pass's axis (drawn thick for the checker). rows = [{"pts", "axis", "sign"}].
    """
    pts = [(float(p[0]), float(p[1])) for p in track]
    if len(pts) < 2 * min_pts:
        return [], "H"
    # gap split
    runs, cur = [], []
    g2 = gap_m * gap_m
    for p in pts:
        if cur and (p[0]-cur[-1][0])**2 + (p[1]-cur[-1][1])**2 > g2:
            runs.append(cur); cur = []
        cur.append(p)
    if cur:
        runs.append(cur)

    def bearing(run, i, w=3):
        a = max(0, i-w); b = min(len(run)-1, i+w)
        return math.degrees(math.atan2(run[b][1]-run[a][1], run[b][0]-run[a][0]))

    raw_rows = []
    for run in runs:
        if len(run) < min_pts:
            continue
        brg = [bearing(run, i) for i in range(len(run))]
        seg = []
        for i, p in enumerate(run):
            turning = _angdiff(brg[max(0, i-3)], brg[min(len(run)-1, i+3)]) > turn_deg
            if turning:
                if len(seg) >= min_pts: raw_rows.append(seg)
                seg = []
            else:
                seg.append(p)
        if len(seg) >= min_pts:
            raw_rows.append(seg)

    rows = []
    for row in raw_rows:
        dx = row[-1][0]-row[0][0]; dy = row[-1][1]-row[0][1]
        axis = "H" if abs(dx) >= abs(dy) else "V"
        rows.append({"pts": row, "axis": axis, "sign": (dx if axis == "H" else dy) >= 0})
    k = max(1, len(rows)//3)
    fa = [r["axis"] for r in rows[:k]]
    pass1 = max(set(fa), key=fa.count) if fa else "H"
    return rows, pass1


def _seg_fp(seg: list) -> tuple:
    """Fingerprint a segment for cross-pull staticity: length + rounded endpoints.
    The planned route is byte-identical pull-to-pull (same fp); the live cut delta
    changes every pull (different fp). Verified on hardware 2026-06-03: the 575/556
    planned segments held one fp across 55/27 pulls."""
    return (len(seg), round(seg[0][0], 1), round(seg[0][1], 1),
            round(seg[-1][0], 1), round(seg[-1][1], 1))


def classify_segments(
    segments: list[list], prev_large_fps: set
) -> tuple[list[list], list[list], set, bool]:
    """Staticity-based cut/planned split — replaces the size-only heuristic.

    The QUERY_PATH planned route streams in as LARGE segments that, once computed,
    are IDENTICAL every pull (static). The live CUT delta is a small segment that
    CHANGES every pull. So classify by staticity, not a fixed size threshold:

      planned  = large (>PLANNED_MIN_POINTS) AND static (fp present last pull)
      cut      = small (<=PLANNED_MIN_POINTS) segments = live actual-path delta
      building = large but NOT static = route still computing, OR a cut-delta spike

    `accumulate` is True only when a stable planned route is present and nothing is
    building/spiking. This kills the size-only heuristic's bug where a cut delta that
    momentarily grew past the threshold was reclassified as planned — inflating
    planned_points AND (because the accumulator was keyed on planned_total) wiping
    coverage to 0. With staticity the spike stays `building` (skipped, not reset).

    Returns (cut_segs, planned_segs, current_large_fps, accumulate). Pass
    current_large_fps back in as prev_large_fps on the next pull.
    """
    segs = [s for s in (segments or []) if isinstance(s, list) and len(s) >= 2]
    large = [s for s in segs if len(s) > PLANNED_MIN_POINTS]
    large_fps = {_seg_fp(s) for s in large}
    planned = [s for s in large if _seg_fp(s) in prev_large_fps]
    planned_fps = {_seg_fp(s) for s in planned}
    cut = [s for s in segs if len(s) <= PLANNED_MIN_POINTS]
    has_building = bool(large_fps - planned_fps)
    accumulate = bool(planned) and not has_building
    return cut, planned, large_fps, accumulate


def accumulate_mowed_area_polygons(existing: list, segments: list) -> list:
    """Merge one QUERY_PATH pull's drawable segments onto the running mowed-area
    polygon set for the session, instead of replacing it.

    Each pull only reports the segments live in that message, not the full
    session history, so the caller must carry `existing` forward pull-to-pull
    (reset it at session start). Overlapping polygons are not deduped here --
    visually they just harmlessly union.
    """
    new_polygons = [
        seg for seg in (segments or [])
        if isinstance(seg, list) and len(seg) >= 3
    ]
    return list(existing or []) + new_polygons


class CutAccumulator:
    """Stitches the per-pull CUT segment into a full session actual-path track.

    Each QUERY_PATH pull carries only the recent cut points (heavily overlapping
    the previous pull), so we append only points that are new (farther than
    `dedup_m` from the recently-added tail) and reset when a new mow begins.
    In-memory for now; a Store-backed version comes with persistence.
    """

    def __init__(self, dedup_m: float = 0.2) -> None:
        self._pts: list[tuple[float, float]] = []
        self._dedup2 = dedup_m * dedup_m
        self.session_key: Any = None

    def reset(self, session_key: Any = None) -> None:
        self._pts = []
        self.session_key = session_key

    @property
    def points(self) -> list[tuple[float, float]]:
        return self._pts

    def ingest(self, cut_segments: list[list], session_key: Any = None) -> None:
        """Add new cut points; reset first if the session changed."""
        if session_key is not None and session_key != self.session_key:
            self.reset(session_key)
        tail = self._pts[-50:]  # only dedup against the recent tail (cheap)
        for seg in cut_segments:
            for p in seg:
                px, py = round(float(p[0]), 3), round(float(p[1]), 3)
                if all((px - tx) ** 2 + (py - ty) ** 2 > self._dedup2 for tx, ty in tail):
                    self._pts.append((px, py))
                    tail.append((px, py))
                    tail = tail[-50:]

    # ── persistence (HA Store) ──────────────────────────────────
    def to_dict(self) -> dict:
        # session_key is now a scalar mow-session id (int); older saves used a
        # tuple. Serialise either without assuming it's iterable.
        sk = self.session_key
        return {"session_key": list(sk) if isinstance(sk, (list, tuple)) else sk,
                "points": self._pts}

    def load_dict(self, data: dict | None) -> None:
        if not data:
            return
        sk = data.get("session_key")
        self.session_key = tuple(sk) if isinstance(sk, list) else sk
        self._pts = [(float(x), float(y)) for x, y in data.get("points", [])]


class BreadcrumbAccumulator:
    """Time-ordered live-position track, sampled from the robot pose on every
    telemetry frame while mowing.

    This is the high-fidelity alternative to stitching sparse QUERY_PATH cut
    chunks: because points are appended in frame-arrival (time) order, there is
    no chunk-reordering scramble — the path is always drawn in the order it was
    actually driven. Each point optionally carries telemetry (RTK precision,
    position quality, GNSS confidence) so the same track feeds heatmap overlays.
    """

    def __init__(self, dedup_m: float = 0.3, max_points: int = 25000) -> None:
        self._pts: list[dict] = []
        self._dedup2 = dedup_m * dedup_m
        self._max = max_points
        self.session_key: Any = None

    def reset(self, session_key: Any = None) -> None:
        self._pts = []
        self.session_key = session_key

    @property
    def points(self) -> list[dict]:
        return self._pts

    def append(self, x: float, y: float, telemetry: dict | None = None,
               session_key: Any = None) -> bool:
        """Append a breadcrumb if the robot moved > dedup distance. Resets first
        if the mow session changed. Returns True if a point was added."""
        if session_key is not None and session_key != self.session_key:
            self.reset(session_key)
        px, py = round(float(x), 3), round(float(y), 3)
        if self._pts:
            lx, ly = self._pts[-1]["x"], self._pts[-1]["y"]
            if (px - lx) ** 2 + (py - ly) ** 2 <= self._dedup2:
                return False
        pt = {"x": px, "y": py}
        if telemetry:
            pt.update({k: v for k, v in telemetry.items() if v is not None})
        self._pts.append(pt)
        if len(self._pts) > self._max:
            self._pts = self._pts[-self._max:]
        return True

    def to_dict(self) -> dict:
        # session_key is a scalar mow-session id (int); serialize without assuming
        # it's iterable (older saves used a tuple).
        sk = self.session_key
        return {"session_key": list(sk) if isinstance(sk, (list, tuple)) else sk,
                "points": self._pts}

    def load_dict(self, data: dict | None) -> None:
        if not data:
            return
        sk = data.get("session_key")
        self.session_key = tuple(sk) if isinstance(sk, list) else sk
        self._pts = [p for p in data.get("points", []) if isinstance(p, dict)]
