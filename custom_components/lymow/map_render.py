"""Diagnostic map rendering for the Lymow integration.

Split out of camera.py so this large, self-contained renderer no longer shares a file
with the live-stream camera — keeps camera.py close to upstream and stops the two
subsystems colliding on edits. The LymowMapCamera entity (camera.py) calls build_map_png().
"""
from __future__ import annotations

import io
import logging
import math
import time
from typing import Any

from .const import (
    DEFAULT_MOW_INTERVAL_DAYS,
    F_IS_CHARGING,
    WORK_STATUS_ERROR, WORK_STATUS_EMERGENCY_STOP,
    WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL, WORK_STATUS_DOCKING,
    MOWER_SIZE_MULT, MOWER_SIZE_DEFAULT, MOWING_STATUSES,
    MAP_RESOLUTION_PX, MAP_RESOLUTION_DEFAULT,
)
from .path_engine import simplify_path, segment_rows
from .heatmap import LAYERS as HM_LAYERS, render_heatmap, render_categorical, CONN_COLORS
from .zone_stats import point_in_polygon
from .pass_coverage import tag_perimeter_infill
from .map_tuning import SWATH_DRAW_M, CUT_WIDTH_M, CHANNEL_RIBBON_HALFWIDTH_M, COMPLETE_FRAC
from .state import corridor_ribbon, polygon_area

_LOGGER = logging.getLogger(__name__)


try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except Exception as exc:
    Image = ImageDraw = ImageFont = ImageFilter = None
    _PIL_ERROR = repr(exc)
else:
    _PIL_ERROR = None

W = H = 800          # default canvas edge (px); per-render value comes from the
                     # Map Resolution setting (see build_map_png).
PAD = 40
HEADER_H = 95        # px reserved at the top for the title / layer / counts / battery
                     # readout, so the map is fitted + centered in the area BELOW it.
FOOTER_H = 90        # px reserved at the bottom for the colour + icon legends, so a
                     # diagonal/corner-filling yard never renders zone labels UNDER them.
                     # Fixed px, so it shrinks to irrelevance at higher resolutions.
# All colours as RGBA tuples (image is created in RGBA mode and converted to RGB at save time)
BG          = (17,  24,  39, 255)
GREEN       = (34, 197,  94, 110)
GREEN_LINE  = (134, 239, 172, 230)
WHITE       = (255, 255, 255, 255)
GREY        = (156, 163, 175, 255)
DONE_GREEN  = ( 16, 185, 129, 230)   # bold emerald — "this zone is DONE" (Paths Off status view)
NODATA_GREY = (148, 163, 184,  80)   # faint — not mowed this session / no data
ACTIVITY_ZONE_FILL = (30, 41, 59, 150)   # dim slate base for the Activity path overlay

# Zone Age style (#2): tint each zone by how long since it was last COMPLETED mow.
# Green-only, "darker = older" — matches reality (long grass is darker/denser) and so it
# never clashes in HUE with the Grass Stripes greens (it's a separate style, not overlaid).
_AGE_FRESH = (140, 224, 120)         # just mowed — bright lawn
_AGE_OLD   = (18,  74,  36)          # at/over the mow interval — deep, dull, overgrown
_AGE_NEVER = (148, 163, 184, 70)     # never mowed = UNKNOWN (neutral grey), NOT "oldest"


def _place_label_xy(cx, cy, poly, label_w, nogo_boxes):
    """Pick a label anchor that (a) sits inside the zone polygon with the label's full
    horizontal span inside it, and (b) is clear of every no-go bounding box (their orange
    fill is always drawn and would otherwise cover the text). Tries the centroid first,
    then a widening ring search; falls back to the centroid if nothing fits (tiny zone
    fully occupied by a no-go). Fixes labels half-covered by a no-go and labels that drift
    off an irregular zone's edge."""
    import math
    hw, hh = label_w / 2.0, 7.0

    def _hits_nogo(lx, ly):
        for nx0, ny0, nx1, ny1 in nogo_boxes:
            if not (lx + hw < nx0 or lx - hw > nx1 or ly + hh < ny0 or ly - hh > ny1):
                return True
        return False

    def _ok(lx, ly):
        return (point_in_polygon(lx, ly, poly)
                and point_in_polygon(lx - hw, ly, poly)
                and point_in_polygon(lx + hw, ly, poly)
                and not _hits_nogo(lx, ly))

    if _ok(cx, cy):
        return cx, cy
    for r in range(12, 220, 11):
        for ang in range(0, 360, 30):
            lx = cx + r * math.cos(math.radians(ang))
            ly = cy + r * math.sin(math.radians(ang))
            if _ok(lx, ly):
                return lx, ly
    return cx, cy


# Dim-by-age modifier: a deep green-black veil whose opacity grows with mow-age, so it
# DARKENS the stripe styles (keeping the green hue) instead of replacing them.
_DIM_VEIL = (10, 38, 18)
_DIM_MAX_ALPHA = 130


def _dim_alpha(last_mowed, now_ts: float, interval_days: float) -> int:
    """Veil opacity (0.._DIM_MAX_ALPHA) to dim a zone by mow-age. 0 if never mowed
    (unknown — don't dim) or freshly mowed; ramps to max at/over the mow interval."""
    if not last_mowed:
        return 0
    age_days = max(0.0, (now_ts - float(last_mowed)) / 86400.0)
    t = min(1.0, age_days / max(1.0, interval_days))
    return int(t * _DIM_MAX_ALPHA)


def _age_color(last_mowed, now_ts: float, interval_days: float) -> tuple:
    """RGBA tint for a zone by mow-age. None/0 last_mowed -> neutral grey (unknown, not
    overdue). Otherwise lerp bright->deep green by age/interval (clamped), so a fresh zone
    glows and an overdue one goes dark green."""
    if not last_mowed:
        return _AGE_NEVER
    age_days = max(0.0, (now_ts - float(last_mowed)) / 86400.0)
    t = min(1.0, age_days / max(1.0, interval_days))
    return (
        int(_AGE_FRESH[0] + (_AGE_OLD[0] - _AGE_FRESH[0]) * t),
        int(_AGE_FRESH[1] + (_AGE_OLD[1] - _AGE_FRESH[1]) * t),
        int(_AGE_FRESH[2] + (_AGE_OLD[2] - _AGE_FRESH[2]) * t),
        170,
    )


def _render_persistent_masks(masks, now_ts, interval_days, sx, sy, scale, size,
                             cell_default: float = 0.25, clip_polys=None, nogo_polys=None):
    """Draw retained per-zone coverage from the persistent occupancy masks
    (state["zone_coverage_history"]). Unlike the live breadcrumb, these survive new tasks AND
    restarts — so a previously-mowed zone keeps its coverage on the map, and a partially-mowed
    (e.g. rained-out) zone shows ONLY its covered cells instead of reading as fully done. Each
    zone is tinted by mow-age (dimmer = older); a zone that has coverage but no recorded
    completion renders fresh-green rather than the grey "unknown" tint. The live breadcrumb
    swath composites on top afterwards for the zone being actively mowed. [Nate 2026-06-21]"""
    ov = Image.new("RGBA", size, (0, 0, 0, 0))
    if not masks:
        return ov
    d = ImageDraw.Draw(ov, "RGBA")
    _fresh = (_AGE_FRESH[0], _AGE_FRESH[1], _AGE_FRESH[2], 170)
    _cm = cell_default
    for m in masks.values():
        if not isinstance(m, dict):
            continue
        cells = m.get("cells") or []
        if not cells:
            continue
        cm = m.get("cell_m") or cell_default
        _cm = cm
        lm = m.get("last_mowed")
        col = _age_color(lm, now_ts, interval_days) if lm else _fresh
        s = max(1.0, scale * cm * 0.95)   # near-full cell (slight overlap closes grid seams)
        for c in cells:
            try:
                px, py = sx(c[0] * cm), sy(c[1] * cm)
            except (IndexError, TypeError, ValueError):
                continue
            d.rectangle((px - s, py - s, px + s, py + s), fill=col)
    # Smooth the blocky 0.25 m cell grid into organic coverage: a sub-cell GaussianBlur rounds
    # the square corners, then a steep alpha rescale (clamped) re-solidifies the body so the
    # edge reads as a clean anti-aliased boundary rather than a hazy glow. Blur scales with the
    # cell's pixel size so it smooths the same at any map resolution. [Nate 2026-06-21]
    if ImageFilter is not None:
        ov = ov.filter(ImageFilter.GaussianBlur(max(2.0, scale * _cm * 0.8)))
        ov.putalpha(ov.getchannel("A").point(lambda v: min(200, int(v * 5.0))))
    # Clip the smoothed coverage to the zone polygons so the blur never bleeds past a
    # perimeter into the channels/background — coverage shows ONLY over real mowable ground,
    # giving clean perimeter-aligned edges instead of a slapped-on halo. No-go zones are
    # punched back out so coverage never shows over them, keeping each zone sharp. [Nate]
    if clip_polys:
        clip = Image.new("L", size, 0)
        cd = ImageDraw.Draw(clip)
        for poly in clip_polys:
            if len(poly) >= 3:
                cd.polygon(poly, fill=255)
        for poly in (nogo_polys or ()):
            if len(poly) >= 3:
                cd.polygon(poly, fill=0)
        ov = Image.composite(ov, Image.new("RGBA", size, (0, 0, 0, 0)), clip)
    return ov


ORANGE      = (249, 115,  22, 255)
YELLOW      = (251, 191,  36, 255)
RED         = (248, 113, 113, 255)
CYAN        = ( 34, 211, 238, 255)
BLACK       = (  0,   0,   0, 255)

# Cached top-down Lymow mower glyph (front = up). Built once; rotated per render.
_MOWER_ICON: dict = {}


def mower_icon(led=(70, 210, 90, 255), size: int = 120, headlights_on: bool = True):
    """Top-down Lymow mower glyph: full-width black deck, gray body sides, black top,
    front LED strips (status-coloured) + stereo cameras + headlights, rear display
    + red E-Stop, black side tracks. Nose points UP (= heading 0). headlights_on
    lights the two front bulbs warm-white; off = dim/unlit."""
    key = (led, size, headlights_on)
    if key in _MOWER_ICON:
        return _MOWER_ICON[key]
    S = size
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im, "RGBA")
    cx = S / 2
    TRK = (20, 20, 22, 255); TREAD = (70, 70, 74, 255); DECK = (17, 17, 19, 255)
    BODYTOP = (34, 34, 38, 255); GRAY = (150, 153, 158, 255); SCREEN = (60, 66, 72, 255)
    ESTOP = (205, 45, 45, 255); CAM = (15, 15, 17, 255)
    # Headlights: warm-white when lit (manual switch OR auto-on while docking), else a
    # dim unlit bulb so the "off" state reads clearly.
    HEAD = (245, 242, 200, 255) if headlights_on else (58, 57, 50, 255)
    HEAD_RING = (120, 120, 90, 255) if headlights_on else (40, 40, 36, 255)
    tw = S * 0.19
    for side in (-1, 1):
        txc = cx + side * S * 0.325; top = S * 0.33; bot = S * 0.95
        d.rounded_rectangle((txc - tw / 2, top, txc + tw / 2, bot), radius=tw / 2, fill=TRK)
        yy = top + 6
        while yy < bot - 6:
            d.line((txc - tw / 2 + 3, yy, txc + tw / 2 - 3, yy), fill=TREAD, width=2); yy += 7
    dw = S * 0.90
    d.rounded_rectangle((cx - dw / 2, S * 0.03, cx + dw / 2, S * 0.39), radius=15, fill=DECK)
    d.rounded_rectangle((cx - dw / 2 + 4, S * 0.03, cx + dw / 2 - 4, S * 0.10), radius=7, fill=(30, 30, 33, 255))
    for side in (-1, 1):
        gx = cx + side * S * 0.205
        d.rounded_rectangle((gx - S * 0.03, S * 0.41, gx + S * 0.03, S * 0.91), radius=5, fill=GRAY)
    bw = S * 0.40
    d.rounded_rectangle((cx - bw / 2, S * 0.40, cx + bw / 2, S * 0.91), radius=10, fill=BODYTOP)
    for side in (-1, 1):
        lx = cx + side * S * 0.135
        d.rounded_rectangle((lx - S * 0.018, S * 0.47, lx + S * 0.018, S * 0.59), radius=4, fill=led)
    for side in (-1, 1):
        hx = cx + side * S * 0.05
        d.ellipse((hx - S * 0.018, S * 0.465, hx + S * 0.018, S * 0.50), fill=HEAD, outline=HEAD_RING)
    d.rounded_rectangle((cx - S * 0.075, S * 0.51, cx + S * 0.075, S * 0.60), radius=4, fill=CAM)
    d.ellipse((cx - S * 0.05, S * 0.535, cx - S * 0.018, S * 0.57), fill=(80, 90, 110, 255))
    d.ellipse((cx + S * 0.018, S * 0.535, cx + S * 0.05, S * 0.57), fill=(80, 90, 110, 255))
    d.rounded_rectangle((cx - S * 0.075, S * 0.71, cx + S * 0.075, S * 0.80), radius=4, fill=SCREEN, outline=(20, 20, 22, 255), width=2)
    d.rounded_rectangle((cx - S * 0.065, S * 0.82, cx + S * 0.065, S * 0.875), radius=5, fill=ESTOP)
    _MOWER_ICON[key] = im
    return im


FALLBACK_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ── Rendering helpers ────────────────────────────────────────────────────────

_CHECKER_MEMO: dict = {}
_STRIPE_MEMO: dict = {}
_PASS_MEMO: dict = {}
_CLEAN_MODE_CHESS = 3   # zoneConfig.cleanMode == CHESS_BOARD_MODE = cross pattern / DOUBLE pass


def _stripe_alpha(coverage, drawable):
    """Per-point pass-2 alpha for the Green-Checker striping, from the mower's OWN per-zone
    setting (no GPS guessing): 130 (translucent plaid, lets the red zone-background show through)
    when a point sits inside a CHECKER zone (zoneConfig.cleanMode == 3), else 255 (solid) — so a
    single-pass zone draws solid and an incidental crossover strip can't render as a faded line
    that false-bleeds red. Memoised; point-in-polygon over the trail is heavy."""
    sig = (len(coverage), coverage[0] if coverage else None, coverage[-1] if coverage else None,
           tuple((z.get("hashId"), (z.get("zoneConfig") or {}).get("cleanMode")) for z in drawable))
    if _STRIPE_MEMO.get("sig") == sig:
        return _STRIPE_MEMO["data"]
    chess = [p for p in (safe_points(z.get("points") or []) for z in drawable
                         if (z.get("zoneConfig") or {}).get("cleanMode") == _CLEAN_MODE_CHESS)
             if len(p) >= 3]
    out = [130 if any(point_in_polygon(px, py, cp) for cp in chess) else 255 for (px, py) in coverage]
    _STRIPE_MEMO["sig"] = sig
    _STRIPE_MEMO["data"] = out
    return out


def _checker_compute(coverage, drawable):
    """Memoised segment_rows + perimeter tagging — the heavy per-frame work for Green
    Checker. tag_perimeter_infill is O(row-points × zone-edges) and was recomputed on
    EVERY render (~10 s, timed out snapshots). Key on a cheap trail+zone signature so
    repeated renders of the SAME trail (idle / picture-card refresh) reuse the result;
    it only recomputes when the breadcrumb trail grows or the zones change.
    Returns (rows, pass1, perim_pts)."""
    sig = (len(coverage),
           coverage[0] if coverage else None,
           coverage[-1] if coverage else None,
           tuple(z.get("hashId") for z in drawable))
    if _CHECKER_MEMO.get("sig") == sig:
        return _CHECKER_MEMO["data"]
    rows, pass1 = segment_rows(coverage)
    zone_polys = [p for p in (safe_points(z.get("points") or []) for z in drawable) if len(p) >= 3]
    if zone_polys:
        tag_perimeter_infill(rows, zone_polys)
    perim_pts = {(round(px, 2), round(py, 2))
                 for r in rows if r.get("kind") == "perimeter"
                 for px, py in r["pts"]}
    data = (rows, pass1, perim_pts)
    _CHECKER_MEMO["sig"] = sig
    _CHECKER_MEMO["data"] = data
    return data


def _classify_passes(coverage, drawable):
    """Config-driven PASS classification (Nate's 3-class model). Returns a per-point class list:
    '1' = pass-1 (blue), '2' = pass-2 / cross cut (orange), 'T' = traversal / channel (grey).

    Per zone:
      • single  (cleanMode != 3) → every point is pass-1.
      • cross   (cleanMode == 3) → find the zone's two pass axes (the two heading peaks, separated
        by the configured cross-angle `relativeCleanDir` — NOT assumed 90°). pass-1 = the axis whose
        points come earlier in time (the 1st cut); pass-2 = the other. A point whose smoothed heading
        matches NEITHER axis (> ~22°) = traversal.
    Points outside every zone (transit between areas) = traversal. Memoised by trail+zone signature.
    """
    n = len(coverage)
    sig = (n, coverage[0] if n else None, coverage[-1] if n else None,
           tuple((z.get("hashId"), (z.get("zoneConfig") or {}).get("cleanMode"),
                  (z.get("zoneConfig") or {}).get("relativeCleanDir")) for z in drawable))
    if _PASS_MEMO.get("sig") == sig:
        return _PASS_MEMO["data"]

    # 1. smoothed per-point heading axis (doubled-angle circular mean over a window)
    WIN = 12
    cs2 = [0.0] * n; sn2 = [0.0] * n           # doubled-angle unit vec for segment i (pt i-1..i)
    for i in range(1, n):
        dx = coverage[i][0] - coverage[i - 1][0]; dy = coverage[i][1] - coverage[i - 1][1]
        if dx or dy:
            a = math.atan2(dy, dx); cs2[i] = math.cos(2 * a); sn2[i] = math.sin(2 * a)
    rc = 0.0; rs = 0.0                          # running window sums
    pax = [0.0] * n
    for i in range(n):
        if i == 0:
            a = 0; b = min(n, WIN + 1); rc = sum(cs2[a:b]); rs = sum(sn2[a:b])
        else:
            add = i + WIN
            if add < n: rc += cs2[add]; rs += sn2[add]
            drop = i - WIN - 1
            if drop >= 0: rc -= cs2[drop]; rs -= sn2[drop]
        pax[i] = 0.5 * math.atan2(rs, rc)      # axis (radians, mod π)

    # 2. assign each point to a zone (bbox pre-filter, then PIP on a downsampled poly)
    zinfo = []
    for zi, z in enumerate(drawable):
        pts = safe_points(z.get("points") or [])
        if len(pts) >= 3:
            if len(pts) > 150:
                step = max(1, len(pts) // 150); pts = pts[::step]
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            zinfo.append((zi, pts, (min(xs), max(xs), min(ys), max(ys)), z.get("zoneConfig") or {}))
    zone_of = [-1] * n
    for i in range(n):
        x, y = coverage[i]
        for zi, poly, (x0, x1, y0, y1), _cfg in zinfo:
            if x0 <= x <= x1 and y0 <= y <= y1 and point_in_polygon(x, y, poly):
                zone_of[i] = zi; break

    # 3. classify per zone
    cls = ['T'] * n
    by_zone: dict = {}
    for i in range(n):
        if zone_of[i] >= 0:
            by_zone.setdefault(zone_of[i], []).append(i)
    cfg_of = {zi: cfg for zi, _p, _b, cfg in zinfo}

    def _adiff(a, b):
        d = abs(a - b) % math.pi
        return min(d, math.pi - d)

    for zi, idxs in by_zone.items():
        cfg = cfg_of[zi]
        if cfg.get("cleanMode") != 3:           # single → all pass-1
            for i in idxs:
                cls[i] = '1'
            continue
        # cross: histogram axes (5° bins, 0..180), find two peaks ~cross-angle apart
        cross = float(cfg.get("relativeCleanDir") or 90)
        BN = 36
        h = [0.0] * BN
        for i in idxs:
            h[int((math.degrees(pax[i]) % 180) // 5) % BN] += 1
        if sum(h) < 12:                          # too sparse → treat as single
            for i in idxs:
                cls[i] = '1'
            continue
        peak1 = max(range(BN), key=lambda b: h[b])
        cb = int(round(cross / 5))
        peak2 = max([(peak1 + cb) % BN, (peak1 - cb) % BN],
                    key=lambda b: sum(h[(b + d) % BN] for d in (-1, 0, 1)))
        ax1 = math.radians(peak1 * 5 + 2.5); ax2 = math.radians(peak2 * 5 + 2.5)
        # pass-1 = earlier-in-time axis (the 1st directional cut)
        t1 = [i for i in idxs if _adiff(pax[i], ax1) <= _adiff(pax[i], ax2)]
        t2 = [i for i in idxs if i not in t1]
        if t1 and t2 and (sum(t1) / len(t1) > sum(t2) / len(t2)):
            ax1, ax2 = ax2, ax1
        trav = math.radians(22)
        for i in idxs:
            d1 = _adiff(pax[i], ax1); d2 = _adiff(pax[i], ax2)
            if min(d1, d2) > trav:
                cls[i] = 'T'
            else:
                cls[i] = '1' if d1 <= d2 else '2'
    _PASS_MEMO["sig"] = sig
    _PASS_MEMO["data"] = cls
    return cls


def _fmt_area(m2, imperial: bool) -> str:
    """Area label in the HA unit system: ft² for imperial/US-customary, else m²."""
    if m2 is None:
        return "—"
    return f"{m2 * 10.7639:.1f} ft2" if imperial else f"{m2:.1f} m2"


def build_map_png(data: dict, imperial: bool = False, device_name: str = "Lymow") -> tuple:
    if Image is None or ImageDraw is None:
        raise RuntimeError(f"Pillow not available: {_PIL_ERROR}")

    # Parse zones EARLY so the canvas can be sized to the yard's actual shape.
    btmap    = data.get("btMap") or {}
    zones    = btmap.get("zones") or []
    drawable = [
        z for z in zones
        if z.get("points") and len(z.get("points") or []) >= 2
    ]
    nogo_zones = btmap.get("nogoZones") or []
    drawable_nogo = [
        z for z in nogo_zones
        if z.get("points") and len(z.get("points") or []) >= 3
    ]

    all_pts: list[tuple[float, float]] = []
    for z in drawable:
        all_pts.extend(safe_points(z.get("points") or []))

    # Map Resolution = the LONGER canvas edge (px); the shorter edge follows the yard's
    # aspect ratio, so a wide property gets a landscape canvas that fills the frame
    # instead of wasting half of a square one. The map area sits below a header band.
    long_edge = MAP_RESOLUTION_PX.get(
        data.get("map_resolution"), MAP_RESOLUTION_PX[MAP_RESOLUTION_DEFAULT]
    )
    if all_pts:
        min_x = min(x for x, y in all_pts); max_x = max(x for x, y in all_pts)
        min_y = min(y for x, y in all_pts); max_y = max(y for x, y in all_pts)
        span_x = (max_x - min_x) or 1.0;    span_y = (max_y - min_y) or 1.0
        aspect = max(0.34, min(3.0, span_x / span_y))   # clamp absurd long/thin strips (≤3:1)
        if aspect >= 1.0:                                # landscape yard
            map_w = float(long_edge); map_h = long_edge / aspect
        else:                                            # portrait yard
            map_h = float(long_edge); map_w = long_edge * aspect
        W = int(round(map_w)) + PAD * 2
        H = int(round(map_h)) + HEADER_H + FOOTER_H
        # Fit the TRUE yard aspect into the map area + centre (fills exactly unless the
        # aspect was clamped, in which case small balanced bands remain).
        scale = min(map_w / span_x, map_h / span_y)
        off_x = PAD + (map_w - span_x * scale) / 2
        off_y = HEADER_H + (map_h - span_y * scale) / 2
    else:
        W = H = long_edge                                # no geometry yet → square msg
        min_x = max_x = min_y = max_y = 0.0
        scale = 1.0; off_x = float(PAD); off_y = float(HEADER_H)

    # Use RGBA throughout; convert to RGB only when saving.
    img  = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img, "RGBA")

    dbg = {
        "zones":        len(zones),
        "drawable":     len(drawable),
        "first_points": len(zones[0].get("points") or []) if zones else 0,
        "channels":     len((btmap.get("channels") or [])),
        "nogo_zones":   len(nogo_zones),
        "drawable_nogo": len(drawable_nogo),
        # DEBUG: per-zone settings from zoneConfig — verify cleanMode (single/double) is present
        "zone_cfg": {(z.get("name") or z.get("hashId")):
                     {"cleanMode": (z.get("zoneConfig") or {}).get("cleanMode"),
                      "pathSpacing": (z.get("zoneConfig") or {}).get("pathSpacing"),
                      "cleanDir": (z.get("zoneConfig") or {}).get("cleanDir"),
                      "has_cfg": bool(z.get("zoneConfig"))}
                     for z in zones},
    }

    # Diagnostic header (all grey): what's shown + the coverage-quality counts.
    _pc = data.get("pass_coverage") or {}
    _layer = data.get("map_layer") or "Coverage"
    _n_obs = len(data.get("obstacle_events") or [])
    _n_missed = _pc.get("missed_count") or 0
    _n_single = sum(1 for c in (_pc.get("clusters") or []) if c.get("kind") == "single")
    _dpct = _pc.get("double_pct")
    _style = data.get("coverage_style") or "Green Checker"
    draw_text(draw, f"{device_name} Map", 20, 14, 20, WHITE)
    _batt = data.get("battery")
    if isinstance(_batt, (int, float)):
        # Colour thresholds are RELATIVE to the mower's Auto Recharge setpoint, not fixed:
        #   <= recharge+5  -> RED (about to be forced to dock)
        #   <= recharge+30 -> YELLOW (getting low)
        #   else           -> grey. Falls back to 15% if the recharge % isn't known yet.
        _rb = data.get("auto_recharge_battery")
        _rb = _rb if isinstance(_rb, (int, float)) else 15
        if _batt <= _rb + 5:
            _bcol = (235, 70, 60)        # red
        elif _batt <= _rb + 30:
            _bcol = (240, 200, 70)       # yellow
        else:
            _bcol = GREY
        draw_text(draw, f"{int(_batt)}% battery", 215, 21, 13, _bcol)
    # Session progress + duration while mowing or heading home — coverage-based % (robust to
    # the cloud's comms-gap undercount) and elapsed mow minutes.
    _sp = data.get("session_percent_display")
    _ws_h = data.get("workStatus")
    if _sp is not None and (_ws_h in MOWING_STATUSES or _ws_h == WORK_STATUS_DOCKING
                            or data.get("robotStatus") == WORK_STATUS_DOCKING):
        _dur = data.get("cleanTime")
        _stxt = f"Session {int(_sp)}%"
        if isinstance(_dur, (int, float)) and _dur:
            _stxt += f"  ·  {int(_dur)} min"
        draw_text(draw, _stxt, 215, 39, 13, (134, 222, 134))
    draw_text(draw, f"{_layer}  ·  {_style}", 20, 40, 13, GREY)
    _counts = [f"{len(drawable)} zones",
               f"{_n_obs} obstacle{'' if _n_obs == 1 else 's'}",
               f"{_n_missed} missed"]
    if _dpct is not None:        # only meaningful when at least one checker zone was mown
        _counts.append(f"{_n_single} skipped-2nd")
    draw_text(draw, "  ·  ".join(_counts), 20, 57, 13, GREY)
    _l4 = []
    if _dpct is not None:
        _l4.append(f"2nd-pass {_dpct}%")   # of the CHECKER-zone area, how much got both passes
    if _pc.get("missed_m2"):
        _l4.append(f"missed {_fmt_area(_pc.get('missed_m2'), imperial)}")
    if _l4:
        draw_text(draw, "  ·  ".join(_l4), 20, 74, 13, GREY)

    if not drawable:
        draw_text(draw, "No drawable polygons in state", 20, 100, 18, RED)
        return png_bytes(img.convert("RGB")), dbg

    if not all_pts:
        draw_text(draw, "Drawable zones exist but points failed to parse", 20, 100, 18, RED)
        return png_bytes(img.convert("RGB")), dbg

    dbg.update({"min_x": min_x, "max_x": max_x, "min_y": min_y, "max_y": max_y,
                "canvas_w": W, "canvas_h": H})

    # scale / off_x / off_y were computed up top (canvas sized to the yard's aspect).
    def sx(x: float) -> float: return (x - min_x) * scale + off_x
    def sy(y: float) -> float: return off_y + (max_y - y) * scale

    # Channels / corridors are drawn AFTER the zones (as a corridor ribbon) so they're visible.

    # Pass Coverage layer: RED-fill the MOWED zones as the background. The swath (drawn OPAQUE
    # below for this layer) covers it, so red shines through ONLY where no swath was actually
    # laid = the un-mowed truth, marked naturally — no per-cell guessing. Mowed = a zone with
    # substantial coverage; skipped/barely-touched zones stay green.
    _pc_layer = (data.get("map_layer") or "Coverage") == "Pass Coverage"
    _paths_off = _style in ("Paths Off", "No Coverage")     # "No Coverage" = legacy alias
    _activity_style = _style == "Activity"
    _zone_age_style = _style == "Zone Age"                   # #2: per-zone mow-age tint
    _dim_by_age = bool(data.get("dim_by_age"))               # #2: dim stripe styles by age
    _zone_ages = data.get("zone_last_mowed") or {}           # {zone_key: epoch | None}
    _mow_interval = float(data.get("mow_interval_days") or DEFAULT_MOW_INTERVAL_DAYS)
    _now_ts = time.time()
    _flag = set(data.get("flaggable_zone_keys") or [])
    # Never red-flag a zone that isn't on the CURRENT task list — kills a stale flag lingering
    # from a finished task (e.g. last task's zone showing red) before the coordinator's next
    # recompute clears it. Empty task list (between tasks) leaves _flag as-is. [Nate 2026-06-21]
    _task_keys = set(data.get("cleanZoneIds") or [])
    if _task_keys:
        _flag &= _task_keys
    _zstats = data.get("zone_stats") or {}
    _mowed_idx = set()
    if _pc_layer:
        # Red-fill ONLY the zones the coordinator deemed FLAGGABLE (on the task list, finished,
        # not the actively-mowed zone) — the exact same gate used for obstacles/missed/single, so
        # the red background, the rings, and the obstacle markers all agree. In-progress, left-
        # incomplete (recharge), and merely-transited zones stay green.
        for _i, _z in enumerate(drawable):
            if (_z.get("hashId") or _z.get("name")) in _flag:
                _mowed_idx.add(_i)
    MISSED_RED = (225, 50, 50, 255)

    def _zone_fill(zone, idx):
        """Zone background. Paths Off = a clean go/no-go view: green if the zone was
        substantially mowed, faint grey otherwise. NO red here — flagged/missed-area
        detail lives in the path overlays (with the rings for context); a red zone in
        this context-free view just reads as 'something went wrong'. Other styles keep
        the original behaviour (red only on the Pass Coverage layer, paths draw on top)."""
        key = zone.get("hashId") or zone.get("name")
        if _zone_age_style:
            return _age_color(_zone_ages.get(key), _now_ts, _mow_interval)
        if _paths_off:
            poly = safe_points(zone.get("points") or [])
            area = polygon_area(poly) if len(poly) >= 3 else 0.0
            cov = (_zstats.get(key) or {}).get("covered_m2") or 0.0
            if area > 0 and cov / area >= COMPLETE_FRAC:
                return DONE_GREEN                                  # mowed past threshold: DONE
            return NODATA_GREY                                     # not (substantially) mowed / no data
        if _activity_style:
            return ACTIVITY_ZONE_FILL          # dim neutral so the colour-coded path reads clearly
        return MISSED_RED if (_pc_layer and idx in _mowed_idx) else GREEN

    # Map name labels — user-toggleable (yards with many no-go zones get cluttered).
    _labels = data.get("map_labels", "Both")
    _show_zone_labels = _labels in ("Both", "Zone Names")
    _show_nogo_labels = _labels in ("Both", "No-Go Names")

    # No-go bounding boxes (pixel space) — a no-go sitting inside a zone draws its label at
    # ~the same spot as the parent zone's label and they clash (MasterCATZ). We keep the
    # no-go label put and nudge the PARENT label clear of the no-go BELOW. [Nate 2026-06-20]
    _nogo_boxes = []
    for _z in drawable_nogo:
        _p = safe_points(_z.get("points") or [])
        if len(_p) >= 3:
            _pxy = [(sx(x), sy(y)) for x, y in _p]
            _xs = [p[0] for p in _pxy]; _ys = [p[1] for p in _pxy]
            _nogo_boxes.append((min(_xs), min(_ys), max(_xs), max(_ys)))

    # Zone name/age labels are QUEUED here and drawn AFTER the coverage swaths (below), so
    # persistent coverage paths don't bury them. [Nate 2026-06-21]
    _zone_label_queue: list = []
    for idx, zone in enumerate(drawable):
        pts = safe_points(zone.get("points") or [])
        if len(pts) > 300:
            step = max(1, math.ceil(len(pts) / 300))
            pts  = pts[::step]
        xy = [(sx(x), sy(y)) for x, y in pts]
        if len(xy) >= 3:
            draw.polygon(xy, fill=_zone_fill(zone, idx), outline=GREEN_LINE)
            if _show_zone_labels:
                cx = sum(p[0] for p in xy) / len(xy)
                cy = sum(p[1] for p in xy) / len(xy)
                label = str(zone.get("name") or zone.get("hashId") or idx)[:16]
                if _zone_age_style:
                    # Fold the age INTO the zone label (no second label → doesn't worsen the
                    # no-go/zone label clash). "Front Right · 5d" / "· new" if never mowed.
                    _lm = _zone_ages.get(zone.get("hashId") or zone.get("name"))
                    if _lm:
                        label = f"{label[:12]} · {int((_now_ts - float(_lm)) / 86400.0)}d"
                    else:
                        label = f"{label[:12]} · new"
                # Place the label so it stays INSIDE the zone (full text width) and clear of
                # every no-go FILL (always drawn, so this applies in any Map Labels mode) —
                # fixes labels half-covered by a no-go and labels drifting off an irregular
                # zone's edge. Falls back to the centroid if nothing fits. [Nate 2026-06-20]
                lx, lcy = _place_label_xy(cx, cy, xy, len(label) * 6.0, _nogo_boxes)
                _zone_label_queue.append((label, lx, lcy))   # drawn on top, after coverage


    # No-go zones / excluded areas — orange overlay
    for idx, zone in enumerate(drawable_nogo):
        pts = safe_points(zone.get("points") or [])
        if len(pts) < 3:
            continue

        if len(pts) > 300:
            step = max(1, math.ceil(len(pts) / 300))
            pts = pts[::step]

        xy = [(sx(x), sy(y)) for x, y in pts]

        if xy and xy[0] != xy[-1]:
            xy.append(xy[0])

        try:
            draw.polygon(xy, fill=(249, 115, 22, 130))   # orange transparent fill
            # A 1px same-hue outline vanished against the fill ("just one colour"). Draw a
            # thick dark-burnt-orange border so the no-go boundary actually reads on the map.
            draw.line(xy, fill=(140, 45, 8, 255), width=3)

            if _show_nogo_labels:
                cx = sum(p[0] for p in xy) / len(xy)
                cy = sum(p[1] for p in xy) / len(xy)
                label = str(zone.get("name") or zone.get("hashId") or f"No-Go {idx + 1}")[:14]
                draw_center(draw, label, cx, cy, 9, WHITE)

        except Exception:
            pass

    # Channels / corridors — the points are a CENTRELINE path A→B, so render a corridor RIBBON
    # (offset ±CHANNEL_RIBBON_HALFWIDTH_M perpendicular along the path, mitred at the bends) instead
    # of closing the points into a triangle/sliver. Drawn ON TOP of the zones (translucent) so the
    # corridor reads clearly. Per-vertex normal = perpendicular to the averaged in/out tangent.
    _ch_ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    _chd = ImageDraw.Draw(_ch_ov, "RGBA")
    for channel in (btmap.get("channels") or []):
        ribbon = corridor_ribbon(channel.get("points") or [], CHANNEL_RIBBON_HALFWIDTH_M)
        if len(ribbon) >= 3:
            rib_px = [(sx(x), sy(y)) for x, y in ribbon]
            _chd.polygon(rib_px, fill=(YELLOW[0], YELLOW[1], YELLOW[2], 90),
                         outline=(YELLOW[0], YELLOW[1], YELLOW[2], 230))
    img.alpha_composite(_ch_ov)

    # (Removed the vestigial "planned route" faint-dashed overlay. It drew
    # planned_path_segments, which only ever holds the LAST mowed zone's 444 perimeter
    # cut — so it ghosted a grey outline on just that one zone, visible in every style
    # (worst in Paths Off, where nothing else covers it). The "planned route" concept is
    # dead (444 = perimeter CUT, not a plan); the intentional perimeter ring still draws
    # in the path styles via _perimeter_ring. Proper per-zone perimeter rendering from the
    # real 444 data (all zones) is the queued Logical-Passes-from-real-data work.)

    # Coverage — the accumulated cut track (time-ordered), drawn as a swath with a
    # recency gradient: faint teal where mowed earliest → bright green where mowed
    # most recently. Replaces the old flat hull-fill, which wrongly filled the whole
    # zone after a single perimeter lap. Width ≈ cut swath in px; round joints close
    # the gaps PIL leaves between thick segments at turns.
    # Read the breadcrumb pose-trail directly (canonical source: restored on restart and
    # updated every frame) rather than coverage_track, which is only set inside the
    # QUERY_PATH branch during an active mow and is stale/empty after a restart.
    # COVERAGE = the breadcrumb pose-trail (canonical: restored on restart, updated every
    # frame; coverage_track is only set during an active mow). Time-ordered, so it draws
    # as a connected swath. Render style is a user preference (Coverage Style select):
    #   Gradient       — green recency ramp (dark→bright = oldest→newest)
    #   Logical Passes — each mow row coloured by axis (blue=one pass, orange=the other)
    #   Green Checker  — 2-tone green by row direction, pass-1 thick + pass-2 thin so the
    #                    crosscut reads as a checkerboard (and as clean stripes on a
    #                    single-direction mow, since pass-1 is the thick one).
    def _hatch_poly(poly_px):
        """Diagonal grey hatch clipped to a zone polygon = 'no telemetry data here'."""
        xs = [p[0] for p in poly_px]; ys = [p[1] for p in poly_px]
        x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.polygon(poly_px, fill=(110, 120, 135, 55))                  # faint base
        span = y1 - y0
        for ox in range(x0 - span, x1 + 1, 10):                        # 45° diagonal lines
            ld.line([(ox, y0), (ox + span, y1)], fill=(175, 184, 198, 130), width=1)
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).polygon(poly_px, fill=255)
        img.alpha_composite(Image.composite(layer, Image.new("RGBA", img.size, (0, 0, 0, 0)), mask))

    # MAP LAYER: "Coverage" (the breadcrumb swath, styled) or a telemetry-channel heatmap.
    map_layer = data.get("map_layer") or "Coverage"
    coverage = safe_points(data.get("breadcrumb_track") or data.get("coverage_track") or [])
    skipped = None
    if map_layer == "Pass Coverage":
        # Keep the coverage swath as the base; single-pass give-up clusters (computed in the
        # coordinator) are drawn as red markers AFTER the swath block below.
        skipped = (data.get("pass_coverage") or {}).get("clusters") or []
        dbg.update({"map_layer": map_layer, "skipped": len(skipped)})
    elif map_layer == "Connection Type":
        # Categorical (not a gradient): HOW each point reached us — live WiFi (blue), live
        # Cellular (orange), or back-propagated (violet, no live link). Back-prop points are
        # added once the coverage-union exists; for now the breadcrumb's wifi/cell.
        img.alpha_composite(render_categorical(
            data.get("breadcrumb_track") or [], sx, sy, img.size, scale))
        dbg.update({"map_layer": map_layer, "coverage_points": len(coverage)})
        coverage = []
    elif map_layer not in ("Coverage",):
        spec = HM_LAYERS.get(map_layer)
        if spec is not None:
            img.alpha_composite(render_heatmap(
                data.get("breadcrumb_track") or [], spec[0], spec[1], spec[2],
                sx, sy, img.size, scale, data.get("heatmap_style") or "Smooth"))
            # NO-DATA hatch: telemetry only exists where breadcrumbs were captured (live
            # connection) — it CANNOT be back-filled. Zones the mower never tracked have NO
            # signal data, so hatch them as an explicit "no data here" instead of leaving an
            # empty patch that reads like a clean/good signal.
            _hatched = 0
            for _z in drawable:
                _zp = safe_points(_z.get("points") or [])
                if len(_zp) < 3:
                    continue
                _ar = polygon_area(_zp)
                _cov = (_zstats.get(_z.get("hashId") or _z.get("name")) or {}).get("covered_m2") or 0.0
                # Same bar as Paths Off "mowed/DONE": only a substantially-mowed zone has real
                # telemetry. Everything else (transited or untouched) is no-data → hatch it, so
                # the rule is consistent with the green/grey zones, not an arbitrary cutoff.
                if _ar > 0 and _cov / _ar >= COMPLETE_FRAC:
                    continue
                _hatch_poly([(sx(x), sy(y)) for x, y in _zp])
                _hatched += 1
            dbg["heatmap_nodata_zones"] = _hatched
        dbg.update({"map_layer": map_layer, "coverage_points": len(coverage)})
        coverage = []   # heatmap is drawn; empty out so the swath block below is a no-op
                        # and rendering falls through to the dock/robot/RTK markers.

    # Pass Coverage: AMBER "thin-data" regions where coverage was BACK-PROPAGATED (the sparse
    # burst after a comms blackout). Drawn over the red base but UNDER the swath, so mowed cells
    # still go green — but an un-covered GAP inside a back-prop area reads AMBER ("lost dense
    # tracking here, eyeball it") instead of the solid RED of a likely real miss. Clipped to the
    # red (flaggable) zones so it never bleeds onto un-mowed ground.
    if _pc_layer:
        _bp = [(p["x"], p["y"]) for p in (data.get("breadcrumb_track") or [])
               if isinstance(p, dict) and p.get("conn") == "backprop" and p.get("x") is not None]
        if _bp:
            _amber = Image.new("RGBA", img.size, (0, 0, 0, 0))
            _ad = ImageDraw.Draw(_amber, "RGBA")
            _r = max(4, scale * 2.2)   # ~2.2 m, matches the sparse back-prop pose spacing
            for _x, _y in _bp:
                _cx, _cy = sx(_x), sy(_y)
                _ad.ellipse((_cx - _r, _cy - _r, _cx + _r, _cy + _r), fill=(238, 170, 48, 255))
            _mask = Image.new("L", img.size, 0)
            _md = ImageDraw.Draw(_mask)
            for _i, _z in enumerate(drawable):
                if _i in _mowed_idx:
                    _zp = [(sx(x), sy(y)) for x, y in safe_points(_z.get("points") or [])]
                    if len(_zp) >= 3:
                        _md.polygon(_zp, fill=255)
            if ImageFilter is not None:
                _amber = _amber.filter(ImageFilter.GaussianBlur(max(1, scale * 0.25)))
            img.alpha_composite(Image.composite(
                _amber, Image.new("RGBA", img.size, (0, 0, 0, 0)), _mask))

    style = data.get("coverage_style") or "Green Checker"
    # PERSISTENT per-zone coverage (#4 retention): draw the stored occupancy masks as the
    # coverage BASE, age-tinted, BEFORE the live breadcrumb swath. These survive new tasks +
    # restarts, so previously-mowed zones keep their coverage and a rained-out zone shows only
    # its partial cells; the live breadcrumb then draws on top for the zone being mowed now.
    # Coverage layer only, and only for coverage-showing styles (Zone Age / Paths Off / No
    # Coverage draw their own whole-zone view, so skip the cells there). [Nate 2026-06-21]
    if map_layer == "Coverage" and style not in ("Paths Off", "No Coverage", "Zone Age"):
        _clip = [[(sx(x), sy(y)) for x, y in safe_points(z.get("points") or [])]
                 for z in drawable]
        _nogo = [[(sx(x), sy(y)) for x, y in safe_points(z.get("points") or [])]
                 for z in drawable_nogo]
        img.alpha_composite(_render_persistent_masks(
            data.get("zone_coverage_history") or {}, _now_ts, _mow_interval,
            sx, sy, scale, img.size, clip_polys=_clip, nogo_polys=_nogo))
    width = max(3, int(scale * SWATH_DRAW_M))   # SWATH_DRAW_M / CUT_WIDTH_M from map_tuning.py

    def _polyline(pts_xy, col, w):
        for k in range(1, len(pts_xy)):
            draw.line([pts_xy[k - 1], pts_xy[k]], fill=col, width=w)
            jx, jy = pts_xy[k]; r = w / 2
            draw.ellipse((jx - r, jy - r, jx + r, jy + r), fill=col)  # round joint

    def _perimeter_ring(rows, col):
        # The boundary lap is ONE continuous loop, not a logical V/H pass — draw it as a clean
        # uniform ring (planned segment + heuristic-tagged rows), never heading-coloured or
        # jittered. Shared by Green Checker and Logical Passes. Gated on coverage so it never
        # bleeds onto a heatmap layer (which empties `coverage`).
        if not coverage:
            return
        for s in (safe_points(seg) for seg in (data.get("planned_path_segments") or [])):
            if len(s) >= 2:
                if len(s) > 2500:
                    s = simplify_path(s, 2500)
                _polyline([(sx(x), sy(y)) for x, y in s], col + (255,), width)
        for row in rows:
            if row.get("kind") == "perimeter":
                _polyline([(sx(x), sy(y)) for x, y in row["pts"]], col + (255,), width)

    if style in ("Paths Off", "No Coverage", "Zone Age"):
        # Paths Off / Zone Age: no mowed swaths — the per-zone fill drawn above is the whole
        # story (Paths Off = DONE/flagged/no-data status; Zone Age = green tint by mow-age).
        # A clean at-a-glance view; legend drawn below.
        pass
    elif style == "Gradient":
        GAP_M2 = 4.0
        runs, cur = [], []
        for p in coverage:
            if cur and (p[0]-cur[-1][0])**2 + (p[1]-cur[-1][1])**2 > GAP_M2:
                runs.append(cur); cur = []
            cur.append(p)
        if cur: runs.append(cur)
        total = sum(len(r) for r in runs) or 1
        thinned = [simplify_path(r, max(2, int(2500*len(r)/total))) if len(r) > 2 else r for r in runs]
        tot = sum(len(r) for r in thinned) or 1
        gi = 0
        for rs in thinned:
            pxy = [(sx(x), sy(y)) for x, y in rs]
            for k in range(1, len(pxy)):
                t = gi / max(1, tot - 1)
                _polyline(pxy[k-1:k+1], (int(20+t*150), int(90+t*165), int(40+t*30), 235), width)
                gi += 1
    elif style == "Activity" and coverage:
        # Activity-state path: colour each breadcrumb segment by what the mower was DOING —
        # TRAVEL (grey: in a channel / transit, no cut) · PERIMETER (blue: the boundary laps) ·
        # MAIN (green: the strip fill). Tagged live per pose (the validated state machine).
        # `and coverage`: on a heatmap layer `coverage` is emptied (see ~L884) so this path
        # never draws OVER the heatmap — the selected layer stays the star (the swath styles
        # honour this by iterating `coverage`; we read breadcrumb_track directly, so gate here).
        ACT_COL = {"travel": (140, 146, 156), "perimeter": (96, 165, 250), "main": (52, 211, 153)}
        bc = data.get("breadcrumb_track") or []
        apxy = [(sx(p["x"]), sy(p["y"]), p.get("act")) for p in bc
                if isinstance(p, dict) and p.get("x") is not None]
        r2 = width / 2
        for i in range(1, len(apxy)):
            col = ACT_COL.get(apxy[i][2])
            if col is None:
                continue
            P0, P1 = apxy[i-1][:2], apxy[i][:2]
            draw.line([P0, P1], fill=col + (255,), width=width)
            draw.ellipse((P1[0]-r2, P1[1]-r2, P1[0]+r2, P1[1]+r2), fill=col + (255,))
    else:
        if style == "Logical Passes":
            # PASS-ORDER colouring (config-driven, Nate's 3-class model): pass-1 = blue, pass-2
            # (the cross cut) = orange, traversal / channel = grey. Class per point comes from
            # _classify_passes (uses each zone's cleanMode + stripe/cross angles). Drawn OPAQUE in
            # temporal order so the 2nd cut paints over the 1st → a fully cross-cut zone reads solid
            # orange, a single-pass zone solid blue. Traversals are drawn FIRST (under the passes)
            # so they only show on pure-transit ground (channels), never scarring a mowed zone.
            BLUE, ORANGE, TRAV = (96, 165, 250), (251, 146, 60), (140, 146, 156)
            PERIM_LP = (120, 150, 185)                  # muted steel — the boundary lap, not a pass
            rows_lp, _, perim_pts = _checker_compute(coverage, drawable)
            cls = _classify_passes(coverage, drawable)
            COL = {'1': BLUE, '2': ORANGE, 'T': TRAV}
            r2 = width / 2

            def _is_perim(i):
                pa, pb = coverage[i - 1], coverage[i]
                return ((round(pa[0], 2), round(pa[1], 2)) in perim_pts
                        and (round(pb[0], 2), round(pb[1], 2)) in perim_pts)

            def _draw_seg(i, col):
                P0 = (sx(coverage[i - 1][0]), sy(coverage[i - 1][1]))
                P1 = (sx(coverage[i][0]), sy(coverage[i][1]))
                draw.line([P0, P1], fill=col + (255,), width=width)
                draw.ellipse((P1[0] - r2, P1[1] - r2, P1[0] + r2, P1[1] + r2), fill=col + (255,))

            for i in range(1, len(coverage)):           # under-layer: traversals first
                if cls[i] == 'T' and not _is_perim(i):
                    _draw_seg(i, TRAV)
            for i in range(1, len(coverage)):           # over-layer: passes, temporal (2nd over 1st)
                if cls[i] != 'T' and not _is_perim(i):
                    _draw_seg(i, COL[cls[i]])
            _perimeter_ring(rows_lp, PERIM_LP)         # clean boundary ring
        else:  # Green Checker (plaid) — RAW-HEADING infill + uniform-dark perimeter
            # Pale lime vs deep forest; pass-1 opaque, pass-2 semi-transparent overlay so
            # the crossing passes blend into a plaid. The INFILL is coloured per-point by
            # local heading over the FULL trail (turn arcs included), so stripes reach the
            # edge and a U-turn fades between colours as the heading rotates — no gaps.
            # PERIMETER laps are one continuous single-direction loop, so they're tagged
            # and painted a UNIFORM dark-green ring on top, never coloured by heading.
            # Lawn-stripe greens: a bright and a deeper green, both clearly GREEN (not the old
            # near-black forest + yellowish lime, which read as harsh dark/yellow stripes). Close
            # in hue + brightness so it looks like real mowing stripes on grass. [Nate 2026-06-07]
            LIGHT, DARK = (138, 214, 104), (70, 150, 84)
            HW = 5  # heading-smoothing window (wider = fewer H/V flips on curvy GPS)
            rows, pass1, perim_pts = _checker_compute(coverage, drawable)  # memoised heavy work
            # Pass-2 alpha is now PER-ZONE from the mower's cleanMode: translucent (130) inside a
            # CHECKER zone so the red background shows through; opaque (255) inside a single-pass
            # zone so it's solid and doesn't false-bleed red. No global single-direction guess.
            p2alpha = _stripe_alpha(coverage, drawable)

            def _hax(i):
                a = max(0, i - HW); b = min(len(coverage) - 1, i + HW)
                dx = coverage[b][0] - coverage[a][0]; dy = coverage[b][1] - coverage[a][1]
                horiz = abs(dx) >= abs(dy)
                return ("H" if horiz else "V"), ((dx if horiz else dy) >= 0)

            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            odraw = ImageDraw.Draw(overlay, "RGBA")
            r2 = width / 2
            for i in range(1, len(coverage)):
                p0, p1 = coverage[i - 1], coverage[i]
                # Skip perimeter-lap steps here — drawn uniform-dark afterwards.
                if ((round(p0[0], 2), round(p0[1], 2)) in perim_pts
                        and (round(p1[0], 2), round(p1[1], 2)) in perim_pts):
                    continue
                ax, sign = _hax(i)
                base = (LIGHT if sign else DARK)
                P0 = (sx(p0[0]), sy(p0[1])); P1 = (sx(p1[0]), sy(p1[1]))
                if ax == pass1:                       # opaque underlay onto the map
                    draw.line([P0, P1], fill=base + (255,), width=width)
                    draw.ellipse((P1[0] - r2, P1[1] - r2, P1[0] + r2, P1[1] + r2), fill=base + (255,))
                else:                                  # blended overlay (per-zone alpha)
                    a2 = p2alpha[i]
                    odraw.line([P0, P1], fill=base + (a2,), width=width)
                    odraw.ellipse((P1[0] - r2, P1[1] - r2, P1[0] + r2, P1[1] + r2), fill=base + (a2,))
            img.alpha_composite(overlay)
            # Uniform dark-green perimeter ring on top of the infill checker, from BOTH the real
            # structural lap (planned_path_segments) AND the heuristic-tagged rows (union, so even
            # small/thin zones whose lap is under PLANNED_MIN_POINTS still get a ring). Same colour
            # → overlap invisible. _perimeter_ring() also gates on coverage so it never bleeds onto
            # a heatmap layer.
            _perimeter_ring(rows, DARK)

    # Dim-by-age MODIFIER: darken each zone's stripes by mow-age (older = darker/duller),
    # the stripe pattern stays visible through the veil. Only on the Coverage layer with a
    # stripe style — NOT the dedicated Zone Age fill, Paths Off, or a heatmap layer. [Nate]
    if (_dim_by_age and map_layer in ("Coverage", "Pass Coverage")
            and not _zone_age_style and not _paths_off):
        veil = Image.new("RGBA", img.size, (0, 0, 0, 0))
        vdraw = ImageDraw.Draw(veil, "RGBA")
        drew = False
        for zone in drawable:
            a = _dim_alpha(_zone_ages.get(zone.get("hashId") or zone.get("name")),
                           _now_ts, _mow_interval)
            if a <= 0:
                continue
            pts = safe_points(zone.get("points") or [])
            if len(pts) > 300:
                pts = pts[::max(1, math.ceil(len(pts) / 300))]
            xy = [(sx(x), sy(y)) for x, y in pts]
            if len(xy) >= 3:
                vdraw.polygon(xy, fill=_DIM_VEIL + (a,)); drew = True
        if drew:
            # Punch the no-go polygons out of the veil so they're NOT dimmed — they stay
            # clearly orange instead of going brownish in an aged zone. [Nate 2026-06-20]
            hole = Image.new("L", img.size, 0)
            hdraw = ImageDraw.Draw(hole)
            for ngz in drawable_nogo:
                npts = safe_points(ngz.get("points") or [])
                if len(npts) > 300:
                    npts = npts[::max(1, math.ceil(len(npts) / 300))]
                nxy = [(sx(x), sy(y)) for x, y in npts]
                if len(nxy) >= 3:
                    hdraw.polygon(nxy, fill=255)
            valpha = veil.getchannel("A")
            valpha.paste(0, mask=hole)        # zero the veil's alpha over every no-go
            veil.putalpha(valpha)
            img.alpha_composite(veil)
        dbg["dim_by_age"] = True
    dbg.update({"coverage_points": len(coverage), "coverage_style": style})

    # Zone name/age labels ON TOP of the coverage swaths (queued during the zone-fill pass),
    # so held/persistent mow paths never bury the names. [Nate 2026-06-21]
    for _lbl, _lx, _ly in _zone_label_queue:
        draw_center(draw, _lbl, _lx, _ly, 10, WHITE)

    # (Missed count/total now live in the grey diagnostic header.)

    # Mower/dock/RTK marker sizing. The floor + cap scale with the RESOLUTION (image px) — a
    # FIXED 84px cap crushed the Map-Mower-Size differences at higher resolution (Medium/Large/
    # XL all clamped to 84). Now the cap is ~84 at 800px and grows with the image, so the size
    # select stays meaningful at any resolution. Dock/RTK render a bit smaller than the mower.
    # [Nate 2026-06-21]
    _mk_mult = MOWER_SIZE_MULT.get(data.get("mower_size") or MOWER_SIZE_DEFAULT,
                                   MOWER_SIZE_MULT[MOWER_SIZE_DEFAULT])
    _px_floor = max(12, round(min(W, H) * 0.02))            # findable on huge yards / 4K
    _px_cap = max(_px_floor + 1, round(min(W, H) * 0.105))  # ~84 at 800px, scales with resolution
    mower_icon_px = max(_px_floor, min(_px_cap, round(scale * CUT_WIDTH_M * _mk_mult)))
    marker_px = max(10, round(mower_icon_px * 0.72))        # dock/RTK a touch smaller than the mower

    # Prefer the mower-reported dock; fall back to the derived one (captured from its pose
    # while charging) so the dock marker persists even when chargingStationLoc drops out.
    # Pick the FIRST source with valid x/y — a reported dock that's present-but-empty (no
    # x/y) must NOT short-circuit the fallback (that's why the dock vanished mid-mow).
    x = y = None; dock_src = None
    for src, cand in (("reported", data.get("chargingStationLoc")), ("derived", data.get("derived_dock"))):
        if isinstance(cand, dict):
            _x, _y = to_float(cand.get("x")), to_float(cand.get("y"))
            if _x is not None and _y is not None:
                x, y, dock_src = _x, _y, src
                break
    dbg["dock"] = dock_src or "none"
    if x is not None and y is not None:
        dx, dy = sx(x), sy(y)
        # Charging-dock glyph: rounded plate + lightning bolt. Drawn BEFORE the robot
        # so the mower marker always sits on top and stays visible while docked.
        # Scaled by g (glyph was tuned at ~22px) so it matches the mower icon size. [Nate]
        g = marker_px / 22.0
        s = max(8, round(11 * g))
        draw.rounded_rectangle((dx - s, dy - s, dx + s, dy + s), radius=max(3, round(4 * g)),
                               fill=(45, 55, 72, 255), outline=YELLOW, width=max(2, round(3 * g)))
        draw.polygon([(dx + 3 * g, dy - s + 4 * g), (dx - 5 * g, dy + 1 * g), (dx, dy + 1 * g),
                      (dx - 3 * g, dy + s - 4 * g), (dx + 6 * g, dy - 2 * g), (dx, dy - 2 * g)],
                     fill=YELLOW)


    # RTK base antenna — physically at the ENU origin (0,0), the survey datum.
    # Drawn as a cyan diamond (distinct shape vs the round dock/robot pins) so it
    # reads as a fixed reference rather than a movable unit.
    ax, ay = sx(0.0), sy(0.0)
    _g = marker_px / 22.0                     # scale RTK glyph with the mower icon [Nate]
    _m = max(13, round(13 * _g))
    if -_m <= ax <= W + _m and HEADER_H - _m <= ay <= H - FOOTER_H + _m:
        # Survey-benchmark glyph: cyan triangle + center dot. The RTK base is the
        # fixed survey datum (the ENU origin everything is referenced to), so a
        # benchmark marker reads truer than a "broadcasting" antenna.
        draw.polygon(
            [(ax, ay - 11 * _g), (ax + 10 * _g, ay + 8 * _g), (ax - 10 * _g, ay + 8 * _g)],
            outline=CYAN, width=max(2, round(3 * _g)),
        )
        draw.ellipse((ax - 2 * _g, ay - 1 * _g, ax + 2 * _g, ay + 3 * _g), fill=CYAN)

    # Obstacles — a thing the mower routed around. HOLLOW amber hazard triangle sized to
    # the footprint: reads as "caution, physical object" AND stays see-through so the
    # un-mowed area underneath is still visible. (Distinct from give-up dashed rings.)
    HAZARD = (245, 170, 30, 255)
    for ob in (data.get("obstacle_events") or []):
        center = ob.get("center")
        if not center:
            continue
        try:
            ox, oy = sx(float(center[0])), sy(float(center[1]))
        except (TypeError, ValueError, IndexError):
            continue
        # Size to the estimated OBJECT (hole minus avoidance standoff), not the full
        # un-mowed hole — so the marker reflects the real thing, not the keep-out zone.
        fw, fh = (ob.get("object_m") or ob.get("footprint_m") or (0, 0))[:2]
        rad = max(11, int(max(float(fw), float(fh)) * scale / 2) + 3)
        draw.polygon([(ox, oy - rad), (ox - rad, oy + rad * 0.8), (ox + rad, oy + rad * 0.8)],
                     outline=HAZARD, width=3)
        draw_center(draw, "!", ox, oy + rad * 0.18, max(10, int(rad * 0.8)), HAZARD)
    dbg.update({"obstacle_events": len(data.get("obstacle_events") or [])})

    # Dwell / stuck anomalies (#5). Marker per class: SPIN (crop-circle) = magenta spiral;
    # STRUGGLE (thrashed in place) = red burst; EXCESS-TURN (> half turn) = amber partial spiral;
    # PAUSED (E-stop / RTK lock — stationary) = muted grey pause bars.
    # Markers scale with map resolution (were fixed-px, too small to spot at Large/4K). [Nate]
    _am = max(1.0, min(4.0, scale / 12.0))      # 1× at Standard, up to 4× at 4K
    _aw = max(2, round(2 * _am))                 # stroke width
    _af = max(9, round(9 * _am))                 # label font px
    def _ar(r):
        return r * _am                           # scaled radius/offset
    for ev in (data.get("anomaly_events") or []):
        ac = ev.get("center")
        if not ac:
            continue
        try:
            ax, ay = sx(float(ac[0])), sy(float(ac[1]))
        except (TypeError, ValueError, IndexError):
            continue
        _kind = ev.get("kind")
        if _kind == "spin":
            acol = (235, 60, 140, 255)
            for _k, _r in enumerate((_ar(5), _ar(9), _ar(13))):
                draw.arc((ax - _r, ay - _r, ax + _r, ay + _r), 30 + _k * 100, 30 + _k * 100 + 250,
                         fill=acol, width=_aw)
            draw_center(draw, "spin", ax, ay + _ar(17), _af, acol)
        elif _kind == "struggle":
            acol = (245, 170, 30, 255)                      # amber/yellow BURST (moderate, 2-4 m)
            for _deg in range(0, 360, 45):
                _r = math.radians(_deg)
                draw.line((ax + _ar(4) * math.cos(_r), ay + _ar(4) * math.sin(_r),
                           ax + _ar(13) * math.cos(_r), ay + _ar(13) * math.sin(_r)), fill=acol, width=_aw)
            draw.ellipse((ax - _ar(3), ay - _ar(3), ax + _ar(3), ay + _ar(3)), fill=acol)
            draw_center(draw, "struggle", ax, ay + _ar(17), _af, acol)
        elif _kind == "excess-turn":
            acol = (245, 170, 30, 255)                      # amber partial spiral
            draw.arc((ax - _ar(11), ay - _ar(11), ax + _ar(11), ay + _ar(11)), 30, 300, fill=acol, width=_aw)
            draw.arc((ax - _ar(6), ay - _ar(6), ax + _ar(6), ay + _ar(6)), 60, 250, fill=acol, width=_aw)
            draw_center(draw, "excess turn", ax, ay + _ar(17), _af, acol)
        elif _kind == "jitter":
            acol = (235, 70, 60, 255)                       # RED burst, bigger/denser = heavy wear (>= 4 m)
            for _deg in range(0, 360, 30):
                _r = math.radians(_deg)
                draw.line((ax + _ar(4) * math.cos(_r), ay + _ar(4) * math.sin(_r),
                           ax + _ar(16) * math.cos(_r), ay + _ar(16) * math.sin(_r)), fill=acol, width=_aw)
            draw.ellipse((ax - _ar(4), ay - _ar(4), ax + _ar(4), ay + _ar(4)), fill=acol)
            draw_center(draw, "jitter", ax, ay + _ar(19), _af, acol)
        else:                                                # paused (info)
            acol = (150, 156, 165, 255)
            draw.rectangle((ax - _ar(5), ay - _ar(7), ax - _ar(2), ay + _ar(7)), fill=acol)
            draw.rectangle((ax + _ar(2), ay - _ar(7), ax + _ar(5), ay + _ar(7)), fill=acol)
            draw_center(draw, "paused", ax, ay + _ar(16), _af, acol)
    dbg.update({"anomaly_events": len(data.get("anomaly_events") or [])})

    # Pass Coverage layer: dashed rings on under-/un-mowed spots. RED = MISSED (no swath laid —
    # matches the Missed-Areas truth); AMBER = single-pass (got one pass, not the cross-pass).
    # See-through and distinct from the obstacle hazard triangle. Drawn DEAD LAST.
    if skipped:
        MISSED_RING = (240, 60, 60, 255)
        GIVEUP = (255, 184, 70, 255)
        for c in skipped:
            ctr = c.get("center") or ()
            if len(ctr) != 2:
                continue
            try:
                cx, cy = float(ctr[0]), float(ctr[1])
            except (TypeError, ValueError):
                continue
            col = MISSED_RING if c.get("kind") == "missed" else GIVEUP
            r = max(11.0, math.sqrt(max(0.1, float(c.get("area_m2") or 1))) * scale * 0.5)
            X, Y = sx(cx), sy(cy)
            box = (X - r, Y - r, X + r, Y + r)
            for a in range(0, 360, 30):            # dashed: draw every other arc segment
                draw.arc(box, a, a + 17, fill=col, width=3)
            draw_center(draw, _fmt_area(c.get('area_m2'), imperial), X, Y - r - 8, 10, col)
        dbg["skipped_drawn"] = len(skipped)

    # MOWER — the top-down mower glyph, rotated by heading, drawn DEAD LAST so it's
    # always on top (incl. the dock when parked). LED strips are status-coloured like the
    # real mower: GREEN healthy, AMBER on a warning, RED on an error/emergency-stop.
    robot = data.get("robotLoc") or data.get("pose") or data.get("robotPosePib")
    if isinstance(robot, dict):
        rxv, ryv = to_float(robot.get("x")), to_float(robot.get("y"))
        if rxv is not None and ryv is not None:
            # Check BOTH workStatus AND robotStatus: an E-Stop (and E74-style obstacle
            # errors) set robotStatus=Emergency-Stop/Error while workStatus stays "Mowing"
            # — same dual check the Clear Error button uses.
            err = (data.get("workStatus") in (WORK_STATUS_ERROR, WORK_STATUS_EMERGENCY_STOP)
                   or data.get("robotStatus") in (WORK_STATUS_ERROR, WORK_STATUS_EMERGENCY_STOP)
                   or bool(data.get("errorCode")) or bool(data.get("errorCodes")))
            warn = bool(data.get("warningCodes"))
            charging = (data.get("workStatus") in (WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL)
                        or bool(data.get("isRecharging")) or bool(data.get(F_IS_CHARGING)))
            if err:
                led = (235, 70, 60, 255)
            elif warn:
                led = (245, 170, 30, 255)
            elif charging:
                # "Breathing" green while charging: brightness oscillates with wall time.
                # Each camera fetch renders the current phase, so it pulses at the card's
                # refresh rate (set a low refresh on the picture card for a smooth breathe).
                ph = (math.sin(time.time() * 1.7) + 1.0) / 2.0      # 0..1
                led = (int(35 + 28 * ph), int(105 + 115 * ph), int(48 + 38 * ph), 255)
            else:
                led = (70, 210, 90, 255)
            hdg = to_float(data.get("mowerHeading")) or 0.0
            # Headlights ON when the manual switch is on (camLedStatus==3) OR the mower is
            # heading home (workStatus/robotStatus == DOCKING). Auto-off otherwise (charging,
            # mowing, idle) unless the switch is on — a fun touch that mirrors the real mower
            # lighting up its path back to the dock.
            _cam_led = data.get("camLedStatus")
            if _cam_led is None:
                _rc = data.get("robotConfig")
                _cam_led = (_rc.get("camLedStatus") if isinstance(_rc, dict)
                            else getattr(_rc, "camLedStatus", None))
            # On while HEADING HOME (Docking), but NOT once parked + charging on the dock —
            # otherwise a mid-task rain-hold (docked + charging, status still "Docking", never
            # flips to Waiting) leaves the icon's headlight bit stuck on. Manual switch
            # (camLedStatus==3) always wins. [Nate 2026-06-20]
            headlights_on = (_cam_led == 3
                             or ((data.get("workStatus") == WORK_STATUS_DOCKING
                                  or data.get("robotStatus") == WORK_STATUS_DOCKING)
                                 and not charging))
            dbg["headlights"] = bool(headlights_on)
            # PIL rotates CCW; compass heading is CW from north → negate. Calibrated live.
            base = mower_icon(led=led, headlights_on=headlights_on)
            rot = base.rotate(-hdg, expand=True, resample=Image.BICUBIC)
            # rotate(expand=True) grows the bbox to the rotated diagonal (×√2 at 45°, ×1 at
            # N/S/E/W). A direct thumbnail() would then scale the body INVERSELY to heading —
            # biggest axis-aligned, smallest at 45°, pulsing as it turns. Paste the rotated
            # sprite onto a constant frame (the sprite diagonal) FIRST, so every heading
            # downscales by the same factor and the body stays one size at all angles.
            diag = math.ceil((base.width ** 2 + base.height ** 2) ** 0.5)
            frame = Image.new("RGBA", (diag, diag), (0, 0, 0, 0))
            frame.alpha_composite(rot, ((diag - rot.width) // 2, (diag - rot.height) // 2))
            icon = frame
            # Mower marker size = the shared, resolution-aware mower_icon_px computed above
            # (anchored to the real swath × the Map-Mower-Size mult, floor/cap scaled to the
            # image so the size select stays meaningful at any resolution). [Nate 2026-06-21]
            mower_px = mower_icon_px
            icon.thumbnail((mower_px, mower_px), Image.LANCZOS)
            dbg["mower_px"] = mower_px
            # Headlight BEAMS: the icon's bulbs vanish at map scale, so when lit, project two
            # warm-white cones forward (heading direction) UNDER the sprite — "lights on" then
            # reads at any zoom, mirroring the real mower lighting its path home while docking.
            if headlights_on and ImageFilter is not None:
                _cxp, _cyp = sx(rxv), sy(ryv)
                _rad = math.radians(hdg)
                _dx, _dy = math.sin(_rad), -math.cos(_rad)   # compass→screen (north = up = -y)
                _px, _py = -_dy, _dx                         # perpendicular
                _beam = Image.new("RGBA", img.size, (0, 0, 0, 0))
                _bd = ImageDraw.Draw(_beam, "RGBA")
                _L, _nose, _hw, _nw = mower_px * 1.7, mower_px * 0.34, mower_px * 0.42, mower_px * 0.08
                for _s in (-1, 1):
                    _bx = _cxp + _px * mower_px * 0.13 * _s
                    _by = _cyp + _py * mower_px * 0.13 * _s
                    _ox, _oy = _bx + _dx * _nose, _by + _dy * _nose
                    _fx, _fy = _ox + _dx * _L, _oy + _dy * _L
                    _bd.polygon([(_ox + _px * _nw, _oy + _py * _nw),
                                 (_fx + _px * _hw, _fy + _py * _hw),
                                 (_fx - _px * _hw, _fy - _py * _hw),
                                 (_ox - _px * _nw, _oy - _py * _nw)],
                                fill=(255, 244, 205, 95))
                _beam = _beam.filter(ImageFilter.GaussianBlur(max(1, mower_px * 0.06)))
                img.alpha_composite(_beam)
            ix, iy = int(sx(rxv) - icon.width / 2), int(sy(ryv) - icon.height / 2)
            img.alpha_composite(icon, (ix, iy))
            dbg["mower_heading"] = hdg

    # Pass Coverage legend — what the background colours mean (red miss vs amber thin-data).
    if _pc_layer:
        lx, ly = 20, img.size[1] - 58
        draw.rectangle([lx - 8, ly - 6, lx + 168, ly + 43], fill=(15, 23, 42, 185))
        for i, (col, lab) in enumerate((
            ((225, 50, 50), "Possible miss"),
            ((238, 170, 48), "Thin tracking (back-prop)"),
        )):
            yy = ly + i * 19
            draw.rectangle([lx, yy, lx + 14, yy + 12], fill=col, outline=GREEN_LINE)
            draw_text(draw, lab, lx + 22, yy - 1, 12, WHITE)

    # Paths Off legend — explains the zone-status colours.
    if style in ("Paths Off", "No Coverage"):
        lx, ly = 20, img.size[1] - 58
        draw.rectangle([lx - 8, ly - 6, lx + 150, ly + 43], fill=(15, 23, 42, 180))
        for i, (col, lab) in enumerate((
            (DONE_GREEN, "Mowed"),
            (NODATA_GREY, "Not mowed / no data"),
        )):
            yy = ly + i * 19
            draw.rectangle([lx, yy, lx + 14, yy + 12], fill=col, outline=GREEN_LINE)
            draw_text(draw, lab, lx + 22, yy - 1, 12, WHITE)

    # Activity legend — what the mower was doing along each segment.
    if style == "Activity":
        lx, ly = 20, img.size[1] - 76
        draw.rectangle([lx - 8, ly - 6, lx + 118, ly + 61], fill=(15, 23, 42, 185))
        for i, (col, lab) in enumerate(((( 96, 165, 250), "Perimeter"),
                                        (( 52, 211, 153), "Main cut"),
                                        ((140, 146, 156), "Travel"))):
            yy = ly + i * 19
            draw.rectangle([lx, yy, lx + 14, yy + 12], fill=col + (255,))
            draw_text(draw, lab, lx + 22, yy - 1, 12, WHITE)

    # Connection Type legend.
    if map_layer == "Connection Type":
        lx, ly = 20, img.size[1] - 76
        draw.rectangle([lx - 8, ly - 6, lx + 130, ly + 61], fill=(15, 23, 42, 185))
        for i, (cls, lab) in enumerate((("wifi", "WiFi"), ("cell", "Cellular"),
                                        ("backprop", "Back-prop"))):
            yy = ly + i * 19
            draw.ellipse((lx, yy, lx + 13, yy + 13), fill=CONN_COLORS[cls] + (255,))
            draw_text(draw, lab, lx + 22, yy - 1, 12, WHITE)

    # Icon / marker legend (bottom-RIGHT, flush to the edge) — explains the map glyphs (vs the
    # colour-fill legends bottom-left). The dashed missed/single-pass rings only exist on the
    # Pass Coverage layer, so those rows are added only there; the rest appear on every layer.
    def _g_dock(gx, yy):
        s = 7
        draw.rounded_rectangle((gx - s, yy - s, gx + s, yy + s), radius=3,
                               fill=(45, 55, 72, 255), outline=YELLOW, width=2)
        draw.polygon([(gx + 2, yy - s + 3), (gx - 3, yy + 1), (gx, yy + 1),
                      (gx - 2, yy + s - 3), (gx + 4, yy - 1), (gx, yy - 1)], fill=YELLOW)

    def _g_rtk(gx, yy):
        draw.polygon([(gx, yy - 7), (gx + 7, yy + 6), (gx - 7, yy + 6)], outline=CYAN, width=2)
        draw.ellipse((gx - 1.5, yy, gx + 1.5, yy + 3), fill=CYAN)

    def _g_obstacle(gx, yy):
        draw.polygon([(gx, yy - 7), (gx - 7, yy + 6), (gx + 7, yy + 6)],
                     outline=(245, 170, 30, 255), width=2)
        draw_center(draw, "!", gx, yy - 2, 10, (245, 170, 30, 255))

    def _g_dashring(col):
        def _g(gx, yy):
            for _a in range(0, 360, 60):                # dashed: every other arc segment
                draw.arc((gx - 7, yy - 7, gx + 7, yy + 7), _a, _a + 32, fill=col, width=2)
        return _g

    def _g_anomaly(gx, yy):
        for _deg in range(0, 360, 60):
            _rr = math.radians(_deg)
            draw.line((gx + 2 * math.cos(_rr), yy + 2 * math.sin(_rr),
                       gx + 7 * math.cos(_rr), yy + 7 * math.sin(_rr)), fill=(235, 60, 140, 255), width=2)

    # COLUMNS of (top, bottom) pairs — 2 rows tall so the panel stays short (a tall single
    # column crept up into bigger yards). Order L→R: Dock/RTK · Missed/Single-pass · Obstacle/Anomaly.
    # The Missed/Single-pass column only exists on Pass Coverage (the only layer with those rings).
    _cols = [[(_g_dock, "Dock"), (_g_rtk, "RTK base")]]
    if _pc_layer:
        _cols.append([(_g_dashring((240, 60, 60, 255)), "Missed area"),
                      (_g_dashring((255, 184, 70, 255)), "Single-pass")])
    _cols.append([(_g_obstacle, "Obstacle"), (_g_anomaly, "Stuck / anomaly")])

    # Per-column width = glyph + the column's OWN widest label + a small gap, so short
    # columns (Dock/RTK) don't reserve room for the longest label. Tight, flush right.
    def _tw(s):
        try:
            b = draw.textbbox((0, 0), s, font=font(12))
            return b[2] - b[0]
        except Exception:
            return len(s) * 7
    _COLGAP = 18
    _colws = [26 + max(_tw(_lab) for _, _lab in _col) + _COLGAP for _col in _cols]
    _iright = img.size[0] - 2
    _ix0 = _iright - sum(_colws)
    _ibot = img.size[1] - 8
    _itop = _ibot - (2 * 19 + 8)
    draw.rectangle([_ix0 - 6, _itop, _iright, _ibot], fill=(15, 23, 42, 190))
    _cx = _ix0
    for _c, _col in enumerate(_cols):
        _gx, _tx = _cx + 9, _cx + 26
        for _r, (_gf, _lab) in enumerate(_col):
            _yy = _itop + 14 + _r * 19
            _gf(_gx, _yy)
            draw_text(draw, _lab, _tx, _yy - 7, 12, WHITE)
        _cx += _colws[_c]

    return png_bytes(img.convert("RGB")), dbg


def text_png(title: str, subtitle: str) -> bytes:
    if Image is None or ImageDraw is None:
        return FALLBACK_PNG
    img  = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img, "RGBA")
    draw_center(draw, title,    W / 2, H / 2 - 16, 22, WHITE)
    draw_center(draw, subtitle, W / 2, H / 2 + 18, 13, GREY)
    return png_bytes(img.convert("RGB"))


def png_bytes(img) -> bytes:
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()


_FONT_CACHE: dict = {}


def font(size: int):
    """Cached font lookup. Previously every draw_text/draw_center re-ran
    ImageFont.truetype("DejaVuSans.ttf") — which fails to resolve by bare name on most
    HA OS installs and fell back to load_default() on EVERY text draw, a real source of
    map-render lag (reported by xar). Resolve once per size and memoise. [2026-06-20]"""
    if ImageFont is None:
        return None
    f = _FONT_CACHE.get(size)
    if f is None:
        try:
            f = ImageFont.truetype("DejaVuSans.ttf", size)
        except Exception:
            # Where DejaVuSans isn't resolvable, prefer the SIZED bundled default
            # (Pillow >= 10.1) so headers/labels still scale; fall back to the legacy
            # fixed default on older Pillow (which renders everything one size).
            try:
                f = ImageFont.load_default(size)
            except TypeError:
                f = ImageFont.load_default()
        _FONT_CACHE[size] = f
    return f


def draw_text(draw, text: str, x: float, y: float, size: int, fill) -> None:
    draw.text((x, y), str(text), font=font(size), fill=fill)


def draw_center(draw, text: str, x: float, y: float, size: int, fill) -> None:
    f    = font(size)
    text = str(text)
    try:
        box     = draw.textbbox((0, 0), text, font=f)
        tw, th  = box[2] - box[0], box[3] - box[1]
    except Exception:
        tw, th = len(text) * size * 0.6, size
    draw.text((x - tw / 2, y - th / 2), text, font=f, fill=fill)


def to_float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def safe_points(points: list[Any]) -> list[tuple[float, float]]:
    """Normalise zone points regardless of storage format.

    Stored as dicts {x, y} from older MQTT path or as tuples (x, y)
    from decode_map_fields / S3 backup map.
    """
    out: list[tuple[float, float]] = []
    for p in points:
        if isinstance(p, dict):
            x, y = to_float(p.get("x")), to_float(p.get("y"))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y = to_float(p[0]), to_float(p[1])
        else:
            continue
        if x is not None and y is not None:
            out.append((x, y))
    return out
