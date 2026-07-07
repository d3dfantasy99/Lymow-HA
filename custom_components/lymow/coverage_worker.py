"""Pure coverage-compute worker, isolated so it can run in a ProcessPoolExecutor child.

The heavy coverage math (point-in-polygon attribution + zone rasterisation) is GIL-bound
pure Python and, on a large lawn, blocks HA's event loop for 0.5–1.8s per tick (measured —
see bench.py). Running it here, imported into a spawned subprocess, keeps that work off the
loop. This module deliberately imports ONLY the pure compute modules (no coordinator / HA),
so the child stays light and import-safe.

All inputs/outputs are plain builtins (lists/tuples/dicts) so they pickle across the process
boundary cleanly.
"""
from __future__ import annotations

from .zone_stats import assign_to_zones
from .obstacles import detect_obstacles
from .pass_coverage import analyze_pass_coverage


def compute_coverage(
    zones: list,
    gz: list,
    nogo: list,
    xy: list,
    bp_xy: list,
    do_obstacle: bool,
) -> tuple:
    """Run the heavy attribution. Mirrors the inline block in coordinator._handle_pboutput.

    Returns (zone_stats, obstacle_events, pass_coverage) — any of which may be None when not
    computed this tick. The caller applies the (cheap) flaggable-filter and state writes on
    the event loop using these results.
    """
    zone_stats = assign_to_zones(zones, xy, []) if (zones and xy) else None

    obstacle_events = None
    pcov = None
    if do_obstacle and gz:
        obstacle_events = detect_obstacles(gz, nogo, xy, lowconf=bp_xy)
        pcov = analyze_pass_coverage(
            xy,
            [g["polygon"] for g in gz],
            nogo,
            obstacles=obstacle_events,
            double_polys=[g["polygon"] for g in gz if g.get("double")],
        )

    return zone_stats, obstacle_events, pcov
