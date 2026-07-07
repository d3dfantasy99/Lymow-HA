"""Persistent per-zone mow-coverage history.

Live session coverage (the breadcrumb pose-trail) is per-session: it resets when a
new task starts and is held in `_cut_store`/`breadcrumb` only for the CURRENT mow.
So the map goes blank for a zone the moment a new task begins, and a user can't see
"when did I last mow this zone / where did I cover."

This module keeps a PERSISTENT per-zone occupancy MASK (0.25 m cells) that survives
both restarts AND new tasks. A zone keeps its last mow on the map until that zone is
itself re-mowed, at which point its mask is cleared (copy-on-write) and rebuilt from
the new session's coverage.

Two rules make it transit-proof (driving across zone A to reach zone C must NOT wipe
A's history):
  * a zone only goes "live" (clear-then-rebuild) when it is on the task list
    (cleanZoneIds) AND is the actively-mowed current zone — exactly the signal
    `_flaggable_zones` uses. Merely transiting an unselected zone never triggers it.
  * `last_mowed` is stamped only on genuine per-zone COMPLETION (mowEndType=completed);
    a cancelled/rained-out zone keeps its partial mask but stays "due."

Storage size is bounded by zone AREA, not mow duration or restart count: a ~30 m²
zone at 0.25 m cells ≈ 1.8 KB packed; a half-acre yard ≈ tens of KB for ALL zones
combined (zones partition the yard — 56 zones does not multiply the cost).

Masks are world-anchored (ENU metres, relative to the RTK base). `enu_base` is stored
alongside each mask so a base relocation can be detected and the stale mask dropped
rather than drawn misaligned.
"""
from __future__ import annotations

import math
from typing import Any

from .zone_stats import point_in_polygon

# Match zone_stats: 0.25 m cells with ±1 cell dilation ≈ the ~0.41 m (16 in) cut swath.
CELL_M = 0.25
DIL = 1

# "Mow this often" — drives the age colour ramp, the overdue threshold, and the
# Overdue Zones sensor. One knob (the Mow Interval number entity) for all of them.
MOW_INTERVAL_DEFAULT_DAYS = 7.0


def cells_for_points(points: list, poly: list, cell: float = CELL_M,
                     dil: int = DIL, into: set | None = None) -> set:
    """Rasterise the (x,y) points that fall inside `poly` to a dilated cell set."""
    out = into if into is not None else set()
    if len(poly) < 3:
        return out
    # bbox fast-reject: most breadcrumb points are far from any single live zone, so 4
    # comparisons skip them before the point-in-polygon ray-cast (identical result).
    bx0 = min(p[0] for p in poly); bx1 = max(p[0] for p in poly)
    by0 = min(p[1] for p in poly); by1 = max(p[1] for p in poly)
    for p in points:
        try:
            px, py = float(p[0]), float(p[1])
        except (IndexError, TypeError, ValueError):
            continue
        if px < bx0 or px > bx1 or py < by0 or py > by1:
            continue
        if not point_in_polygon(px, py, poly):
            continue
        c0 = int(math.floor(px / cell))
        r0 = int(math.floor(py / cell))
        for dx in range(-dil, dil + 1):
            for dy in range(-dil, dil + 1):
                out.add((c0 + dx, r0 + dy))
    return out


def _norm_base(b) -> list | None:
    """Normalise an ENU base point to a numeric [lat, lon, alt] list, or None.

    Accepts the live `enu_base_point` DICT {latitude, longitude, altitude}, an already-
    normalised [lat, lon, alt] list/tuple, or junk — notably a stale list-of-dict-KEYS
    (['latitude','longitude','altitude']) left by the earlier `list(dict)` bug, which
    normalises to None so it's treated as 'no recorded base' (adopt), never 'changed'."""
    if b is None:
        return None
    if isinstance(b, dict):
        lat, lon, alt = b.get("latitude"), b.get("longitude"), b.get("altitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return [lat, lon, alt]
        return None
    if isinstance(b, (list, tuple)):
        if len(b) >= 2 and isinstance(b[0], (int, float)) and isinstance(b[1], (int, float)):
            return list(b)
        return None
    return None


def _base_changed(a, b, tol: float = 0.5) -> bool:
    """True if two ENU base points differ by more than `tol` metres (or one is missing).
    Inputs are assumed already normalised by `_norm_base`; on any parse surprise we
    return False — a real coverage mask must NEVER be dropped on an uncertain compare."""
    if not a or not b:
        return bool(a) != bool(b)
    try:
        return (float(a[0]) - float(b[0])) ** 2 + (float(a[1]) - float(b[1])) ** 2 > tol * tol
    except (IndexError, TypeError, ValueError, KeyError):
        return False


class ZoneCoverageHistory:
    """Per-zone persistent coverage masks with copy-on-write re-mow clearing.

    A zone entry = {name, cells:set[(col,row)], last_mowed:float|None, session_key,
    enu_base:[x,y]|None}. Cells are integer grid coords at `cell_m` resolution.
    """

    def __init__(self, cell_m: float = CELL_M) -> None:
        self.cell_m = cell_m
        self.mow_interval_days = MOW_INTERVAL_DEFAULT_DAYS
        self.dim_by_age = False        # modifier: dim the stripe styles by mow-age
        self._zones: dict[str, dict[str, Any]] = {}
        self._live: set[str] = set()      # zone keys cleared + rebuilding THIS session
        self._session_key: Any = None

    # ── session lifecycle ───────────────────────────────────────
    def begin_session(self, session_key: Any) -> None:
        """A new mow session: forget which zones were 'live' so the next note_active
        for each re-mowed zone triggers a fresh copy-on-write clear."""
        if session_key != self._session_key:
            self._session_key = session_key
            self._live = set()

    def note_active(self, key: str, name: str | None, session_key: Any,
                    enu_base: list | None) -> None:
        """Mark zone `key` as actively being mowed THIS session. The FIRST time per
        session this clears the zone's stored mask (copy-on-write) so the new mow
        rebuilds it from scratch — that is the "clears only when about to be re-mowed"
        behaviour. Callers must gate this on (on task list AND current mowed zone)."""
        self.begin_session(session_key)
        if key in self._live:
            return
        self._live.add(key)
        z = self._zones.setdefault(key, {})
        z["name"] = name or z.get("name") or key
        z["cells"] = set()                 # CLEAR — copy-on-write for the new mow
        z["session_key"] = session_key
        z["enu_base"] = _norm_base(enu_base)
        # last_mowed is preserved here; it only advances on genuine completion.

    def update_live(self, cells_by_key: dict[str, set]) -> None:
        """Replace the stored cells of zones live this session with their current
        full-session cells (the live coverage is recomputed cumulatively each pull,
        so a straight replace keeps the growing mask correct)."""
        for key in self._live:
            cells = cells_by_key.get(key)
            if cells is not None:
                self._zones.setdefault(key, {})["cells"] = set(cells)

    def seed_last_mowed(self, seed: dict) -> None:
        """Backfill/refresh last_mowed from the mower's per-zone history so the age view +
        Overdue sensor track the mower's authoritative last-mow date. ADVANCES a zone to the
        newer of (stored, seed) — monotonic: a fresher live completion is never moved
        backwards, but a zone left blank (None) or stuck on a stale seed is corrected forward.
        seed = {key: {"last_mowed": epoch, "name": str}}. Returns True if anything changed."""
        changed = False
        for key, info in (seed or {}).items():
            ts = info.get("last_mowed")
            if not ts:
                continue
            z = self._zones.setdefault(key, {})
            cur = z.get("last_mowed")
            if cur is None or ts > cur:
                z["last_mowed"] = ts
                z["name"] = z.get("name") or info.get("name") or key
                z.setdefault("cells", set())
                z.setdefault("session_key", None)
                z.setdefault("enu_base", None)
                changed = True
        return changed

    def mark_completed(self, keys, ts: float) -> None:
        """Stamp last_mowed for zones that genuinely completed (mowEndType=completed)."""
        for key in keys or []:
            z = self._zones.get(key)
            if z is not None:
                z["last_mowed"] = ts

    def drop_zones(self, valid_keys) -> None:
        """Forget masks for zones no longer in the map (deleted/renamed)."""
        valid = set(valid_keys or [])
        for key in [k for k in self._zones if k not in valid]:
            del self._zones[key]
            self._live.discard(key)

    def invalidate_on_base_change(self, enu_base: list | None) -> None:
        """Drop masks captured against a KNOWN, DIFFERENT RTK base origin — their world
        coords no longer line up. Entries with no recorded base (e.g. seeded last-mowed
        timestamps, which carry no spatial cells) are NOT dropped — they ADOPT the current
        base so they stay anchored. Only a known-and-different base invalidates; an
        unrecorded base must never be treated as 'changed'."""
        enu_base = _norm_base(enu_base)
        if not enu_base:
            return
        for key in list(self._zones):
            z = self._zones[key]
            b = _norm_base(z.get("enu_base"))
            z["enu_base"] = b                     # heal any stale/garbage base in place
            if b is None:
                z["enu_base"] = list(enu_base)    # unrecorded/garbage base → anchor, don't drop
            elif _base_changed(b, enu_base):
                del self._zones[key]
                self._live.discard(key)

    @property
    def live(self) -> set:
        """Zone keys cleared + rebuilding this session (read-only copy)."""
        return set(self._live)

    # ── read side (render / sensors) ────────────────────────────
    def last_mowed_map(self) -> dict[str, float | None]:
        return {k: z.get("last_mowed") for k, z in self._zones.items()}

    def render_masks(self) -> dict[str, dict[str, Any]]:
        """{key: {name, cells:[[c,r],…], last_mowed, enu_base}} for the map renderer."""
        return {
            k: {
                "name": z.get("name") or k,
                "cells": [list(c) for c in z.get("cells", ())],
                "last_mowed": z.get("last_mowed"),
                "enu_base": z.get("enu_base"),
                "cell_m": self.cell_m,
            }
            for k, z in self._zones.items()
        }

    def covered_m2(self, key: str) -> float:
        z = self._zones.get(key) or {}
        return round(len(z.get("cells", ())) * self.cell_m * self.cell_m, 1)

    # ── persistence (HA Store) ──────────────────────────────────
    def to_dict(self) -> dict:
        sk = self._session_key
        return {
            "cell_m": self.cell_m,
            "mow_interval_days": self.mow_interval_days,
            "dim_by_age": self.dim_by_age,
            "session_key": list(sk) if isinstance(sk, (list, tuple)) else sk,
            "zones": {
                k: {
                    "name": z.get("name") or k,
                    "cells": [list(c) for c in z.get("cells", ())],
                    "last_mowed": z.get("last_mowed"),
                    "session_key": z.get("session_key"),
                    "enu_base": z.get("enu_base"),
                }
                for k, z in self._zones.items()
            },
        }

    def load_dict(self, data: dict | None) -> None:
        if not data:
            return
        self.cell_m = float(data.get("cell_m") or self.cell_m)
        try:
            self.mow_interval_days = float(data.get("mow_interval_days") or self.mow_interval_days)
        except (TypeError, ValueError):
            pass
        self.dim_by_age = bool(data.get("dim_by_age", self.dim_by_age))
        sk = data.get("session_key")
        self._session_key = tuple(sk) if isinstance(sk, list) else sk
        self._zones = {}
        for k, z in (data.get("zones") or {}).items():
            self._zones[k] = {
                "name": z.get("name") or k,
                "cells": {(int(c[0]), int(c[1])) for c in z.get("cells", []) if len(c) == 2},
                "last_mowed": z.get("last_mowed"),
                "session_key": z.get("session_key"),
                "enu_base": _norm_base(z.get("enu_base")),
            }
        # A fresh load is the start of "this run" — nothing is live until note_active.
        self._live = set()
