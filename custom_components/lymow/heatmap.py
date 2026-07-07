"""Map data-heatmap overlay: a layer registry + numpy-free render styles.

The map camera shows ONE layer at a time (a "Map Layer" select). "Coverage" is the
default and uses the coverage styles; every other layer is a per-point telemetry channel
rendered here as a heatmap in the chosen Heatmap Style. Adding a channel = one line in
LAYERS + the select option — that's the whole extensibility story.
"""
from __future__ import annotations

try:
    from PIL import Image, ImageDraw, ImageFilter
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageFilter = None

# Map Layer select options. "Coverage" is special-cased in the camera.
MAP_LAYER_OPTIONS = [
    "Coverage", "Pass Coverage", "Connection Type", "WiFi Signal", "Cellular Signal",
    "LoRa Link", "RTK SNR", "RTK Satellites", "Position Accuracy", "RTK Correction Age",
]
HEATMAP_STYLE_OPTIONS = ["Smooth", "Contour", "Weak-spots", "Path"]

# layer name -> (breadcrumb key, BAD value, GOOD value). ABSOLUTE bounds, not the data's
# own min/max: a near-constant channel (e.g. RTK SNR 33-37) then renders at its true
# quality (uniformly green), instead of auto-scaling a 4-unit jitter into a full red→green
# gradient. Only channels with real spatial variation (WiFi/Cell) show a gradient.
LAYERS = {
    "WiFi Signal":       ("wifi",    -85, -55),    # dBm  (higher = better)
    "Cellular Signal":   ("cell",    -95, -60),    # dBm
    "LoRa Link":         ("lora",    400, 1000),   # bps to RTK base (higher = better)
    "RTK SNR":           ("rtk_snr",  28,   42),   # dB-Hz
    "RTK Satellites":    ("sats",     12,   22),   # count, all-band RTK solution (higher = better)
    "Position Accuracy": ("hacc",    0.15, 0.02),  # metres (LOWER = better)
    "RTK Correction Age": ("diffage",  12,    2),  # seconds (LOWER = better)
}

# "Connection Type" is CATEGORICAL, not a gradient — it shows HOW each point's data reached
# us: live WiFi, live Cellular, or back-propagated (recovered after a comms gap, no live link).
CONN_COLORS = {
    "wifi":     (56, 189, 248),    # sky blue  — live WiFi uplink
    "cell":     (251, 146, 60),    # orange    — live cellular uplink
    "backprop": (167, 139, 250),   # violet    — recovered via 333/444 back-prop (no live link)
}


def conn_of(p: dict) -> str | None:
    """Connection class for a breadcrumb point: explicit `conn` if captured, else inferred
    from which signal was present (older data has no `conn` field)."""
    c = p.get("conn")
    if c in CONN_COLORS:
        return c
    if isinstance(p.get("wifi"), (int, float)) and p["wifi"]:
        return "wifi"
    if isinstance(p.get("cell"), (int, float)) and p["cell"]:
        return "cell"
    return None


def render_categorical(points, sx, sy, size, scale, color_map=CONN_COLORS, classifier=conn_of):
    """Render points coloured by a CATEGORICAL class (dots, not a gradient). Used by the
    Connection Type layer; points already tagged 'backprop' render in that colour too."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    if not points:
        return img
    d = ImageDraw.Draw(img)
    r = max(2, int(scale * 0.30))
    for p in points:
        if not isinstance(p, dict) or p.get("x") is None:
            continue
        col = color_map.get(classifier(p))
        if col is None:
            continue
        cx, cy = sx(p["x"]), sy(p["y"])
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col + (225,))
    return img


def _ramp(t: float):
    """t in 0..1, 1 = best → green, 0 = worst → red (through amber)."""
    t = max(0.0, min(1.0, t))
    return (int(235 - max(0.0, t - 0.5) * 2 * 190),
            int(70 + min(t, 0.5) * 2 * 155), 65)


def render_heatmap(breadcrumbs, key, bad, good, sx, sy, size, scale, style):
    """Return an RGBA overlay (size = (W,H)) for one telemetry channel, to be
    alpha_composited onto the map. breadcrumbs = list of {x,y,<key>,...} dicts.
    bad/good = ABSOLUTE channel bounds (good can be < bad for lower-is-better)."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    pts = [(p["x"], p["y"], p.get(key)) for p in breadcrumbs
           if isinstance(p, dict) and isinstance(p.get(key), (int, float))
           and p.get("x") is not None]
    if len(pts) < 3:
        return img
    span = (good - bad) or 1

    def tval(v):
        # 1 = good (green), 0 = bad (red). Absolute scale → flat channels stay uniform.
        return max(0.0, min(1.0, (v - bad) / span))

    CELL = 0.7
    cells: dict = {}
    for x, y, v in pts:
        cells.setdefault((round(x / CELL), round(y / CELL)), []).append(v)
    cmean = {k: sum(vv) / len(vv) for k, vv in cells.items()}
    cw = max(2.0, scale * CELL)

    if style == "Path":
        for x, y, v in pts:
            px, py = sx(x), sy(y); s = max(2, scale * 0.3)
            d.rectangle((px - s, py - s, px + s, py + s), fill=_ramp(tval(v)) + (235,))
    elif style == "Weak-spots":
        for (cx, cy), m in cmean.items():
            weak = tval(m) < 0.30   # absolute: only genuinely-poor cells flag (not relative)
            px, py = sx(cx * CELL), sy(cy * CELL)
            if weak:
                s = scale * 0.95
                d.ellipse((px - s, py - s, px + s, py + s), fill=(235, 70, 65, 205))
            else:
                s = max(1, scale * 0.25)
                d.ellipse((px - s, py - s, px + s, py + s), fill=(70, 160, 90, 130))
    else:  # Smooth / Contour — blurred cell field
        ov = Image.new("RGBA", size, (0, 0, 0, 0))
        od = ImageDraw.Draw(ov, "RGBA")
        for (cx, cy), m in cmean.items():
            t = tval(m)
            if style == "Contour":
                t = round(t * 5) / 5
            px, py = sx(cx * CELL), sy(cy * CELL); s = cw * 0.85
            od.rectangle((px - s, py - s, px + s, py + s), fill=_ramp(t) + (220,))
        # Blur scaled to the cell pixel size so it's actually smooth at ANY resolution — the
        # old fixed 9px blur barely softened the big cells at high res (looked blocky). [Nate]
        _blur = (cw * 0.6) if style == "Smooth" else (cw * 0.18)
        ov = ov.filter(ImageFilter.GaussianBlur(max(2.0, _blur)))
        img.alpha_composite(ov)
    return img

MAP_LAYER_DEFAULT = "Coverage"
HEATMAP_STYLE_DEFAULT = "Smooth"
