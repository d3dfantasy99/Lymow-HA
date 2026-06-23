"""Lymow sensor platform."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.core import callback
from homeassistant.helpers.restore_state import RestoreEntity

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfArea, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CLEAN_MODE_ADAPTIVE_ZIGZAG,
    CLEAN_MODE_CHESS_BOARD,
    CLEAN_MODE_PERIMETER_ONLY,
    CLEAN_MODE_ZIGZAG,
    DEFAULT_MOW_INTERVAL_DAYS,
    DOMAIN,
    F_BATTERY,
    F_CLEAN_AREA,
    F_CLEAN_MODE,
    F_CUT_HEIGHT,
    F_ERROR_CODE,
    F_FW_VERSION,
    F_IP_ADDRESS,
    F_LTE_SIGNAL,
    F_MCU_VERSION,
    F_NET_DETAIL,
    F_RTK_STATUS,
    F_SERIAL_NO,
    F_WIFI_SIGNAL,
    MANUFACTURER,
    NET_SIM_SIGNAL,
    NET_WIFI_SIGNAL,
    RTK_STATUS_LABELS,
    RTSP_PATH,
    RTSP_PORT,
    error_label,
    warning_label,
    audio_label,
)
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity

# ── Label maps ────────────────────────────────────────────────

CLEAN_MODE_LABELS: dict[str, str] = {
    CLEAN_MODE_ZIGZAG:          "Zigzag",
    CLEAN_MODE_CHESS_BOARD:     "Chess Board",
    CLEAN_MODE_PERIMETER_ONLY:  "Perimeter Only",
    CLEAN_MODE_ADAPTIVE_ZIGZAG: "Adaptive Zigzag",
}

WORK_STATUS_LABELS: dict[int, str] = {
    -1: "Offline",
    0:  "Idle",
    1:  "Waiting",
    2:  "Mowing",
    3:  "Paused",
    4:  "Docking",
    5:  "Charging",
    6:  "Remote Control",
    7:  "Error",
    8:  "Resuming",
    9:  "Zone Partitioning",
    10: "Pause Docking",
    11: "Updating",
    12: "Fully Charged",
    13: "Emergency Stop",
    14: "Escaping",
    15: "RTT Test",
}

NET_TYPE_LABELS: dict[int, str] = {0: "None", 1: "WiFi", 2: "LTE"}

# Cellular enums decoded from the Lymow app (3.0.7) — authoritative app labels.
# CARD_REGIST_*: NONE=0, REGISTERED=1, SEARCHING=2, NO_REGISTERED=3
CELL_REGIST_LABELS: dict[int, str] = {0: "None", 1: "Registered", 2: "Searching", 3: "No Registered"}
# CARD_STATUS_*: NONE=0, READY=1, NO_CARD=2, ERROR=3
SIM_CARD_STATUS_LABELS: dict[int, str] = {0: "None", 1: "Ready", 2: "No Card", 3: "Error"}

# PbMutateResult.code = the mower's ack to the last "mutate" command (zone/channel/
# settings change). Decoded from the app's MutateRes enum (re-3.0.7 bundle, confirmed
# via its name↔int switch). The mower persistently echoes the LAST result, so the
# steady-state value reflects the most recent config change (code 5 = a zone-info edit
# that applied cleanly). Friendly wording replaces the raw "code N".
MUTATE_RESULT_LABELS: dict[int, str] = {
    0: "None",
    1: "Zone Cleared",
    2: "Clear Zone Failed",
    3: "Channel Deleted",
    4: "Delete Channel Failed",
    5: "Zone Settings Updated",
    6: "Zone Update Failed",
    7: "All Zones/Channels Cleared",
    8: "Clear All Failed",
    9: "RTK Bound",
    10: "RTK Binding Failed",
    11: "Factory Restore Failed",
    12: "Factory Restored",
    13: "Channel Updated",
    14: "Channel Update Failed",
    15: "Global Settings Updated",
    16: "Global Settings Failed",
    17: "Runtime Settings Updated",
    18: "Runtime Settings Failed",
    19: "Zones Merged",
    20: "Merge Zones Failed",
    21: "Zone Cut",
    22: "Cut Zone Failed",
}


# ── Descriptor ────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class LymowSensorDesc(SensorEntityDescription):
    value_source: str | Callable[[dict], Any] = ""
    transform: Callable[[Any], Any] | None = None
    attrs_source: Callable[[dict], dict] | None = None
    # Plain-language explanation surfaced as a `description` attribute in the HA
    # more-info dialog, so a user troubleshooting can understand what the value
    # means and which direction is good/bad without leaving HA.
    description: str | None = None


# ── Helpers ───────────────────────────────────────────────────

def _read(obj: Any, key: str, default: Any = None) -> Any:
    """Read key from dict or protobuf/dataclass object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _net(key: str) -> Callable[[dict], Any]:
    """Read network value from protobuf netDetailInfo, dict, or flat alias."""
    return lambda d: _read(d.get(F_NET_DETAIL), key, d.get(key))


def _rtk(key: str) -> Callable[[dict], Any]:
    """Read RTK L1 value from protobuf object, dict, or flat alias."""
    return lambda d: _read(d.get("rtkDiagnosticL1"), key, d.get(key))


def _robot_ip(d: dict) -> str | None:
    """Robot IP — top-level ipAddress, fallback netDetailInfo.wifiIp/rest IP."""
    return d.get(F_IP_ADDRESS) or _read(d.get(F_NET_DETAIL), "wifiIp") or d.get("rest_ip_address")


def _rtk_link_state(d: dict) -> str | None:
    """At-a-glance RTK correction-link health from differential age + fix status.
    Reads coordinator state directly (rtkDiffAge + rtkStatus) so it is independent
    of whether the detailed RTK sensors are enabled."""
    age = d.get("rtkDiffAge")
    if age is None:
        return None  # no RTK frame yet → unknown
    if age >= 15:
        return "Lost"      # corrections gone (LoRa packets stopped)
    if age >= 5:
        return "Stale"     # corrections slowing
    st = d.get("rtkStatus")
    if st == 2:            # RTK_STATUS_FIX (~2 cm)
        return "Healthy"
    if st == 1:            # RTK_STATUS_FLOAT_FIX (~40 cm)
        return "Degraded"
    return "Acquiring"     # fresh corrections, no fix yet


def _history_summary(key: str) -> Callable[[dict], Any]:
    return lambda d: (d.get("cleanHistorySummary") or {}).get(key)


def _zone_catalog_value(key: str) -> Callable[[dict], Any]:
    def _inner(d: dict) -> Any:
        catalog = d.get("zone_catalog")
        if catalog is not None:
            if key == "zone_count":
                return len(getattr(catalog, "zones", []) or [])
            if key == "channel_count":
                return len(getattr(catalog, "channels", []) or [])
            if key == "zones_with_points":
                return sum(1 for z in (getattr(catalog, "zones", []) or []) if getattr(z, "polygon_points", None))
            if key == "has_enu_base_point":
                return getattr(catalog, "enu_base_point", None) is not None
        btmap = d.get("btMap") or {}
        if key == "channel_count":
            return len(btmap.get("channels") or [])
        if key == "zones_with_points":
            return sum(1 for z in (btmap.get("zones") or []) if z.get("points"))
        if key == "has_enu_base_point":
            return bool(btmap.get("enuBasePoint") or d.get("enu_base_point"))
        return btmap.get(key)
    return _inner


# ── Sensor definitions ────────────────────────────────────────

SENSORS: tuple[LymowSensorDesc, ...] = (

    # ── Status ───────────────────────────────────────────────────────────
    LymowSensorDesc(
        key="work_status",
        name="Status",
        icon="mdi:robot-mower",
        value_source="workStatus",
        transform=lambda v: WORK_STATUS_LABELS.get(v, f"Unknown ({v})"),
    ),
    LymowSensorDesc(
        key="robot_status",
        name="Robot Status",
        icon="mdi:robot-mower-outline",
        value_source="robotStatus",
        transform=lambda v: WORK_STATUS_LABELS.get(v, f"Unknown ({v})"),
    ),
    LymowSensorDesc(
        key="error",
        name="Error Detail",
        icon="mdi:alert-circle-outline",
        description="WHAT the current fault is — the decoded error code/name, or 'None' when the mower is OK. The companion 'Error' binary sensor is the on/off flag for WHETHER a fault is active; this one says what it is.",
        value_source=F_ERROR_CODE,
        transform=lambda v: error_label(v) if v else "None",
        entity_registry_enabled_default=False,
    ),

    # ── Battery ──────────────────────────────────────────────────────────
    LymowSensorDesc(
        key="battery",
        name="Battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
        value_source=F_BATTERY,
    ),

    # ── Mowing config ────────────────────────────────────────────────────
    LymowSensorDesc(
        key="clean_mode",
        name="Mow Mode",
        icon="mdi:grass",
        value_source=F_CLEAN_MODE,
        transform=lambda v: CLEAN_MODE_LABELS.get(v, v) if v else None,
    ),
    LymowSensorDesc(
        key="blade_height",
        name="Blade Height",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:scissors-cutting",
        value_source=F_CUT_HEIGHT,
    ),
    LymowSensorDesc(
        key="move_speed",
        name="Move Speed",
        native_unit_of_measurement="m/s",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
        value_source="moveSpeed",
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="cut_speed",
        name="Blade Speed",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan",
        value_source="cutSpeed",
        entity_registry_enabled_default=False,
    ),

    # ── Motion ───────────────────────────────────────────────────────────
    LymowSensorDesc(
        key="mower_heading",
        name="Heading",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:compass",
        value_source="mowerHeading",
        description="The mower's compass heading in degrees (0°=North, 90°=East), derived from its RTK pose orientation. This is what rotates the mower icon on the map to point the way it's driving.",
        entity_registry_enabled_default=False,
    ),
    # Current Speed (twistLinear) and Turn Rate (twistAngular) are sourced from
    # baseOutput.twist which the mower does NOT include in its MQTT broadcast.
    # These fields may only be available via BLE. Re-enable if a future firmware
    # update adds them to the MQTT stream.
    #
    # LymowSensorDesc(
    #     key="twist_linear",
    #     name="Current Speed",
    #     native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
    #     device_class=SensorDeviceClass.SPEED,
    #     state_class=SensorStateClass.MEASUREMENT,
    #     icon="mdi:speedometer",
    #     value_source="twistLinear",
    #     entity_registry_enabled_default=False,
    # ),
    # LymowSensorDesc(
    #     key="twist_angular",
    #     name="Turn Rate",
    #     native_unit_of_measurement="rad/s",
    #     state_class=SensorStateClass.MEASUREMENT,
    #     icon="mdi:rotate-right",
    #     value_source="twistAngular",
    #     entity_registry_enabled_default=False,
    # ),

    # ── Clean session ────────────────────────────────────────────────────
    LymowSensorDesc(
        key="session_area",
        name="Session Mowed Area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-check",
        value_source=F_CLEAN_AREA,
        transform=lambda v: int(float(v)) if v is not None else None,
    ),
    LymowSensorDesc(
        key="session_time",
        name="Session Duration",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
        value_source="cleanTime",
        transform=lambda v: int(float(v)) if v is not None else None,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="session_percent",
        name="Session Progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-check",
        # Derived in the coordinator: monotonic within a task, pinned to 100% on completion
        # (raw cleanPercent dips backward as the planned total grows and resets to 0 on dock).
        value_source="session_percent_display",
        transform=lambda v: int(v) if v is not None else None,
    ),
    LymowSensorDesc(
        key="session_remain",
        name="Session Remaining",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-sand",
        value_source="remainCleanTime",
        transform=lambda v: int(float(v)) if v is not None else None,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="last_session_travel_time",
        name="Last Session Travel Time",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:transit-connection-variant",
        # Time the mower spent in transit (between zones / to-from dock), NOT mowing —
        # accumulated per session by the zone-visit logger (coordinator _zv_travel_s).
        value_source="last_session_travel_minutes",
        description="Minutes the mower spent travelling (between zones and to/from the dock) during the last session — transit time, separate from mowing time.",
    ),
    LymowSensorDesc(
        key="map_area",
        name="Map Total Area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map",
        value_source="mapArea",
        transform=lambda v: int(float(v)) if v is not None else None,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="zone_count",
        name="Zone Count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-multiple",
        value_source=_zone_catalog_value("zone_count"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="nogo_zone_count",
        name="No-Go Zone Count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-off",
        value_source=lambda d: (d.get("btMap") or {}).get("nogo_zone_count"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="nogo_zones_with_points",
        name="No-Go Zones With Points",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:vector-polygon",
        value_source=lambda d: (d.get("btMap") or {}).get("nogo_zones_with_points"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="nogo_area_total",
        name="No-Go Area Total",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-off",
        value_source=lambda d: int(round(sum(
            float(z.get("area") or 0)
            for z in ((d.get("btMap") or {}).get("nogoZones") or [])
            if isinstance(z, dict)
        ))),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="zones_with_points",
        name="Zones With Points",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:vector-polygon",
        value_source=_zone_catalog_value("zones_with_points"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="channel_count",
        name="Channel Count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-path",
        value_source=_zone_catalog_value("channel_count"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="map_has_gps_origin",
        name="Map Has GPS Origin",
        icon="mdi:crosshairs-gps",
        value_source=_zone_catalog_value("has_enu_base_point"),
        entity_registry_enabled_default=False,
    ),

    # ── Path engine (zone-analytics Phase 1) ─────────────────────────────
    LymowSensorDesc(
        key="coverage_points",
        name="Coverage Points",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-path",
        # Coverage = the breadcrumb pose-trail (robot's actual position every frame):
        # dense, time-ordered, order-independent. Replaced the sparse QUERY_PATH-delta
        # accumulator (which also made coverage depend on whether/when the perimeter
        # segment appeared). Updates every frame.
        value_source=lambda d: len(d.get("breadcrumb_track") or []) or None,
        description="Count of GPS pose-trail points captured this mow — the dense breadcrumb that feeds the coverage map, the Pass Coverage analysis, and the per-zone stats. The 'by_zone' attribute breaks the count down per zone (point-in-polygon attribution).",
        # Per-zone breakdown (geometric attribution): {zone: {coverage_points}}.
        attrs_source=lambda d: {"by_zone": d.get("zone_stats") or {}},
    ),
    LymowSensorDesc(
        key="planned_path_points",
        # Renamed: the large STATIC QUERY_PATH segment is the PERIMETER / structural
        # lap, not a precomputed plan (proven 2026-06-03 by a perimeter-last run where
        # it appeared at 85% instead of the start). Entity id stays planned_path_points
        # for continuity; the displayed name reflects what it actually is.
        name="Perimeter / Structural Path",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:vector-square",
        description="Points in the mower's perimeter / structural lap — the large static QUERY_PATH segment. It's the ACTUAL perimeter the mower drives (not a precomputed plan), debounced so it doesn't flicker while being laid down.",
        value_source=lambda d: (d.get("path_engine") or {}).get("planned_points"),
        attrs_source=lambda d: {"segment_markers": d.get("segment_markers") or []},
    ),
    # Path Deviations sensor removed 2026-06-04 — it measured actual-vs-PLANNED, but the
    # mower never sends a planned route, so it was undefinable. Obstacles is now detected
    # plan-free from coverage holes (see below).
    LymowSensorDesc(
        key="obstacles",
        name="Obstacles",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:bullseye-arrow",
        description="Distinct obstacles the mower routed around this session — detected plan-free as UN-MOWED islands surrounded by mowed area inside a zone (and not explained by a no-go). Each one's location + footprint is in the attribute. Updates as the mow progresses; a tree/post/bed shows up once the mower has worked around it.",
        # Coverage-hole obstacle events (obstacles.detect_obstacles): an enclosed un-mowed
        # island = something routed around. Attribute carries each event's center/size.
        value_source=lambda d: (d.get("path_engine") or {}).get("obstacle_count"),
        attrs_source=lambda d: {"obstacles": [
            {"center": list(c.get("center", ())), "cells": c.get("cells"),
             "object_m": list(c.get("object_m", ())),
             "footprint_m": list(c.get("footprint_m", ())), "zone": c.get("zone")}
            for c in (d.get("obstacle_events") or [])[:50]
        ]},
    ),
    LymowSensorDesc(
        key="anomalies",
        name="Anomalies",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:alert-decagram-outline",
        description="Count of FLAGGED stuck-spot anomalies this session — the mower pinned in one spot while mowing. Classed by yard wear: spin (crop-circle), jitter (>=4 m thrash, heavy wear), struggle (2-4 m), excess-turn (>half a turn). Stationary 'paused' events (E-stop / RTK lock) are NOT counted but listed in the attribute. Each carries center/duration/path/turns/zone.",
        value_source=lambda d: sum(
            1 for e in (d.get("anomaly_events") or [])
            if e.get("kind") in ("spin", "jitter", "struggle", "excess-turn")),
        attrs_source=lambda d: {
            "flagged": [e for e in (d.get("anomaly_events") or [])
                        if e.get("kind") in ("spin", "jitter", "struggle", "excess-turn")][-20:],
            "paused": [e for e in (d.get("anomaly_events") or []) if e.get("kind") == "paused"][-20:],
            "last": ((d.get("anomaly_events") or [None])[-1]),
        },
    ),
    LymowSensorDesc(
        key="double_coverage",
        name="Double Coverage",
        icon="mdi:check-all",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        description="On a checkerboard ('chess') mow every spot should be covered TWICE — once per infill axis. This is the % of the infill-mowed area that actually got both passes. Less than ~100% means Lymow's planner skipped the perpendicular return pass somewhere (a real, submittable path-planning bug). Measured on the two infill axes only, so perimeter laps don't skew it. The 'skipped' attribute lists each single-pass spot (size + location + which axis covered it) — useful to report to Lymow as 'it keeps missing here when mowing this direction'. Computed during a mow once enough coverage exists.",
        value_source=lambda d: (d.get("pass_coverage") or {}).get("double_pct"),
        attrs_source=lambda d: {
            "single_pass_m2": (d.get("pass_coverage") or {}).get("single_pass_m2"),
            "missed_m2": (d.get("pass_coverage") or {}).get("missed_m2"),
            "missed_count": (d.get("pass_coverage") or {}).get("missed_count"),
            "infill_area_m2": (d.get("pass_coverage") or {}).get("infill_area_m2"),
            "single_direction": (d.get("pass_coverage") or {}).get("single_direction"),
            "skipped": (d.get("pass_coverage") or {}).get("clusters") or [],
        },
    ),
    LymowSensorDesc(
        key="last_command_result",
        name="Last Command Result",
        icon="mdi:message-alert-outline",
        description="The mower's acknowledgement of the most recent config command (a zone/channel/settings change), decoded to a readable result like 'Zone Settings Updated'. It's STICKY — it holds the last result until a new command, so during a mow it reflects your most recent change, not the mowing itself.",
        # promptInfo.mutateRet — the mower's ack to the last mutate command (zone/
        # channel/settings change). Prefer a human-readable label from the decoded
        # MutateRes enum; fall back to any firmware errorMsg, then a raw code.
        value_source=lambda d: (
            None if not d.get("mutateResult")
            else ((d["mutateResult"].get("errorMsg") or "").strip()
                  or MUTATE_RESULT_LABELS.get(d["mutateResult"].get("code"))
                  or f"Code {d['mutateResult'].get('code')}")
        ),
        # Keep the raw code/errorMsg in attributes for debugging.
        attrs_source=lambda d: dict(d.get("mutateResult") or {}),
    ),

    # ── GNSS / Localization ─────────────────────────────────────────────────
    # NOTE: there is deliberately NO "GNSS Satellites" sensor. The mower's
    # localization numSatellites field is a flat constant (~10) that never tracks
    # reality — it even exceeded the RTK count (10 vs 9) in a weak zone, so it can't
    # be a real in-view/common-visibility count. The single true satellite metric is
    # "RTK Satellites" (satelliteCount) below, which varies with sky/zone.
    LymowSensorDesc(
        key="gnss_horizontal_accuracy",
        name="GNSS Horizontal Accuracy",
        native_unit_of_measurement=UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:crosshairs-gps",
        value_source="gnssHorizontalAccuracy",
        description="Estimated horizontal position accuracy in metres. LOWER is better.",
    ),
    LymowSensorDesc(
        key="gnss_vertical_accuracy",
        name="GNSS Vertical Accuracy",
        native_unit_of_measurement=UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:crosshairs-gps",
        value_source="gnssVerticalAccuracy",
        description="Estimated vertical (up/down) position accuracy in metres. LOWER is better — the vertical companion to GNSS Horizontal Accuracy.",
    ),
    LymowSensorDesc(
        key="gnss_position_quality",
        name="GNSS Position Quality",
        icon="mdi:signal",
        value_source="gnssPositionQuality",
        # Lymow PositionQuality enum (recovered from app 3.0.7 bytecode):
        # 0=NO_SIGNAL, 1=SINGLE_POINT, 2=FLOAT_FIXED, 3=FIXED. This is NOT the
        # generic NMEA fix-type table — value 2 is RTK Float, not DGPS.
        transform=lambda v: {
            0: "No Signal", 1: "Single Point", 2: "RTK Float", 3: "RTK Fix",
        }.get(v, f"Unknown ({v})"),
        description="GNSS fix quality: RTK Fix (best, ~2cm), RTK Float (~40cm), DGPS, or lower.",
    ),

    # ── GPS / RTK ─────────────────────────────────────────────────────────
    LymowSensorDesc(
        key="rtk_status",
        name="RTK GPS",
        icon="mdi:satellite-uplink",
        value_source=F_RTK_STATUS,
        transform=lambda v: RTK_STATUS_LABELS.get(v, f"Unknown ({v})"),
    ),
    LymowSensorDesc(
        key="rtk_precision",
        name="RTK Precision",
        native_unit_of_measurement=UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:crosshairs-gps",
        value_source=_rtk("precision"),
        entity_registry_enabled_default=False,
        description="Estimated RTK horizontal accuracy in metres. LOWER is better — an RTK Fix is ~0.02m (2cm), Float is ~0.4m.",
    ),
    LymowSensorDesc(
        key="rtk_satellites",
        name="RTK Satellites",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-variant",
        value_source=_rtk("satelliteCount"),
        description="Satellites used in the RTK position solution (all bands) — the count the Lymow app shows. Varies with sky view and zone (drops in obstructed areas). HIGHER is better.",
    ),
    LymowSensorDesc(
        key="rtk_l1_satellites",
        name="RTK L1 Satellites",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-variant",
        value_source=_rtk("l1SatelliteCount"),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    LymowSensorDesc(
        key="rtk_l2_satellites",
        name="RTK L2 Satellites",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-variant",
        value_source=_rtk("l2SatelliteCount"),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    LymowSensorDesc(
        key="rtk_diff_age",
        name="RTK Differential Age",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        icon="mdi:timer-sand",
        # Age of the RTK correction data — the LoRa-link heartbeat. Normally ~2s
        # (one LoRa correction cycle); climbing = corrections have stopped arriving
        # (LoRa packets lost). Off by default (maintainer's call whether to surface);
        # users can enable it — it's the single best at-a-glance RTK-link health signal.
        value_source=lambda d: d.get("rtkDiffAge"),
        entity_registry_enabled_default=False,
        description="Age of the latest RTK correction in seconds — the LoRa-link heartbeat. ~2-3s is normal (one LoRa cycle). LOWER is better; a climbing value means corrections have stopped arriving (LoRa packets lost).",
    ),
    LymowSensorDesc(
        key="rtk_base_data_error_rate",
        name="RTK Base Data Error Rate",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:alert-circle-outline",
        value_source=lambda d: d.get("rtkBaseDataErrorRate"),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        description="Error rate of correction data from the RTK base. LOWER is better; high = corrupted/lost correction packets.",
    ),
    LymowSensorDesc(
        key="rtk_l1_snr",
        name="RTK L1 SNR",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal",
        value_source=lambda d: d.get("rtkL1Snr"),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        description="Signal-to-noise ratio of the L1 GNSS band. HIGHER is better (stronger satellite signal).",
    ),
    LymowSensorDesc(
        key="rtk_l2_snr",
        name="RTK L2 SNR",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal",
        value_source=lambda d: d.get("rtkL2Snr"),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        description="Signal-to-noise ratio of the L2 GNSS band. HIGHER is better.",
    ),
    LymowSensorDesc(
        key="rtk_l5_satellites",
        name="RTK L5 Satellites",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-variant",
        value_source=lambda d: d.get("rtkL5Satellites"),
        description="Satellites the RTK receiver is tracking on the GNSS L5 band. Tracking more frequency bands (L1/L2/L5) makes the centimetre fix more robust, especially near obstructions.",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    LymowSensorDesc(
        key="rtk_radio_link",
        name="RTK Radio Link",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:radio-tower",
        # RTK base↔rover radio (LoRa) link rate — best of the 3 channels. Per-channel
        # rate + advanced CW-interference/antenna/HW diagnostics in attributes.
        value_source=lambda d: max(d.get("rtkLoraBps") or [0]) or None,
        attrs_source=lambda d: {
            "lora_bps": d.get("rtkLoraBps"),
            "cw_ratio": d.get("rtkCwRatio"),
            "ant_value": d.get("rtkAntValue"),
            "hw_dc": d.get("rtkHwDc"),
            "l5_snr": d.get("rtkL5Snr"),
        },
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        description="RTK correction radio (LoRa) data rate in bits/sec, best of 3 channels. HIGHER = healthier link throughput.",
    ),
    LymowSensorDesc(
        key="rtk_interference",
        name="RTK Interference",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:waveform",
        # Carrier-wave (CW) interference ratio on the RTK LoRa band — worst of the
        # 3 radio channels. A nearby RF transmitter raises this and degrades the
        # correction link. Complements RTK Radio Link (link *rate*) and Differential
        # Age (link *freshness*): this is link *noise*. Direction/scale TBD live.
        value_source=lambda d: max(d.get("rtkCwRatio") or [0]) or None,
        attrs_source=lambda d: {"per_channel": d.get("rtkCwRatio")},
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        description="Carrier-wave (CW) interference indicator on the RTK LoRa band, worst of 3 channels (0 = clean, see per_channel attribute). HIGHER = MORE interference = worse. Direction inferred from data, not yet validated against a known source.",
    ),
    LymowSensorDesc(
        key="rtk_link_status",
        name="RTK Link Status",
        device_class=SensorDeviceClass.ENUM,
        options=["Healthy", "Degraded", "Acquiring", "Stale", "Lost"],
        icon="mdi:access-point-network",
        # Derived at-a-glance RTK correction-link health (see _rtk_link_state).
        # Healthy = fresh corrections + Fixed; Degraded = fresh + Float; Acquiring =
        # fresh, no fix yet; Stale = diffAge 5-15s (slowing); Lost = diffAge >15s
        # (LoRa packets stopped). Independent of the detailed RTK sensors.
        value_source=_rtk_link_state,
        entity_registry_enabled_default=False,
        description="Overall RTK correction-link health. Healthy = fresh corrections + cm-level Fixed; Degraded = fresh but only Float (~40cm); Acquiring = fresh, no fix yet; Stale = corrections slowing (diffAge 5-15s); Lost = corrections stopped (>15s, LoRa packets lost).",
    ),
    LymowSensorDesc(
        key="rtk_base_status",
        name="RTK Base Status",
        icon="mdi:access-point",
        value_source=_rtk("baseStationStatus"),
        transform=lambda v: {0: "Online", 1: "Offline", 2: "Moved", 3: "Invalid"}.get(v, f"Unknown ({v})"),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        description="RTK base station state: Online (good), Offline, Moved, or Invalid.",
    ),

    # ── Connectivity ──────────────────────────────────────────────────────
    LymowSensorDesc(
        key="wifi_signal",
        name="WiFi Signal",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:wifi",
        value_source=lambda d: d.get(F_WIFI_SIGNAL) or _net(NET_WIFI_SIGNAL)(d),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="lte_signal",
        name="4G Signal",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal-4g",
        value_source=lambda d: d.get(F_LTE_SIGNAL) or _net(NET_SIM_SIGNAL)(d),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="network_type",
        name="Network Type",
        icon="mdi:network",
        value_source=lambda d: NET_TYPE_LABELS.get(
            _net("currentNet")(d), f"Unknown ({_net('currentNet')(d)})"
        ) if _net("currentNet")(d) is not None else None,
        entity_registry_enabled_default=False,
    ),
    # Bluetooth Signal (btSignalQuality) is extracted from robotInfo but the
    # mower always reports 0. May only be meaningful via BLE connection.
    #
    # LymowSensorDesc(
    #     key="bt_signal",
    #     name="Bluetooth Signal",
    #     native_unit_of_measurement="dBm",
    #     device_class=SensorDeviceClass.SIGNAL_STRENGTH,
    #     state_class=SensorStateClass.MEASUREMENT,
    #     icon="mdi:bluetooth",
    #     value_source="btSignalQuality",
    #     entity_registry_enabled_default=False,
    # ),
    LymowSensorDesc(
        key="wifi_name",
        name="WiFi Network",
        icon="mdi:wifi-settings",
        value_source=_net("wifiName"),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    LymowSensorDesc(
        key="sim_iccid",
        name="4G SIM ICCID",
        icon="mdi:sim",
        value_source=_net("simIccid"),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    LymowSensorDesc(
        key="sim_ip",
        name="4G SIM IP",
        icon="mdi:ip-network-outline",
        value_source=_net("simIp"),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ── Cellular health (PbNetDetailInfo, populates on LTE) ───────────────
    LymowSensorDesc(
        key="cellular_registration",
        name="4G Registration",
        icon="mdi:sim-outline",
        device_class=SensorDeviceClass.ENUM,
        options=["None", "Registered", "Searching", "No Registered"],
        value_source=_net("simRegistration"),
        transform=lambda v: CELL_REGIST_LABELS.get(v, f"Unknown ({v})") if v is not None else None,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        description="Cellular network registration (4G). Registered = SIM is on the "
                    "network (healthy); Searching = looking for a tower; No Registered = "
                    "registration failed/denied; None = no cellular. Decoded from the app.",
    ),
    LymowSensorDesc(
        key="sim_card_status",
        name="4G SIM Status",
        icon="mdi:sim",
        device_class=SensorDeviceClass.ENUM,
        options=["None", "Ready", "No Card", "Error"],
        value_source=_net("simCardStatus"),
        transform=lambda v: SIM_CARD_STATUS_LABELS.get(v, f"Unknown ({v})") if v is not None else None,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        description="SIM card state (4G). Ready = SIM present and usable; No Card = no SIM "
                    "detected; Error = SIM fault; None = no cellular. Decoded from the app.",
    ),
    LymowSensorDesc(
        key="fw_version",
        name="Firmware",
        icon="mdi:chip",
        value_source=F_FW_VERSION,   # top-level string ("app2.3.9 bl0.0.1")
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="mcu_version",
        name="MCU Version",
        icon="mdi:memory",
        value_source=F_MCU_VERSION,  # top-level string ("v2.1.42_beta")
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="serial_number",
        name="Serial Number",
        icon="mdi:identifier",
        value_source=F_SERIAL_NO,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="mac_address",
        name="MAC Address",
        icon="mdi:network-outline",
        value_source="macAddress",
        description="The mower's network MAC address — its hardware network identifier. Diagnostic; off by default.",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # NOTE: Camera Confidence + GNSS Confidence sensors removed 2026-06-03 — they read
    # algoLocOutput.sensorConfidence, which the mower NEVER sends on the cloud telemetry
    # topic (0/7505 frames; it's a local/BLE-only message). No field enables it; dead.
    LymowSensorDesc(
        key="rtk_base_serial",
        name="RTK Base Serial",
        icon="mdi:radio-tower",
        value_source="rtkSn",
        description="Serial number of the paired RTK base station (the fixed reference antenna that broadcasts the cm-level corrections). Diagnostic; off by default.",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    LymowSensorDesc(
        key="wheel_firmware",
        name="Wheel Motor Firmware",
        icon="mdi:tire",
        value_source="wheelVer",
        description="Firmware version of the wheel/track motor controller. Diagnostic; off by default.",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="blade_firmware",
        name="Blade Motor Firmware",
        icon="mdi:fan",
        value_source="knifeVer",
        description="Firmware version of the blade/cutting motor controller. Diagnostic; off by default.",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="audio_volume",
        name="Speaker Volume",
        icon="mdi:volume-high",
        value_source="audioVolume",
        transform=lambda v: {0: "Mute", 30: "Low", 70: "Medium", 100: "High"}.get(v, f"{v}%"),
        description="The mower's speaker volume level (how loud its audio cues play) — Mute / Low / Medium / High. Off by default.",
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="warning",
        name="Warning",
        icon="mdi:alert-outline",
        value_source=lambda d: (
            ", ".join(warning_label(c) for c in d.get("warningCodes", []))
            if d.get("warningCodes") else "None"
        ),
        description="The mower's active warning(s), decoded from warningCodes to readable labels (e.g. lift timeout, tip-over, front/rear ultrasonic lost). 'None' when healthy. A WARNING is a caution (the mower keeps working) — distinct from Error, which is a hard fault. Drives the amber state on the map's mower icon.",
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        # Readable "last voice prompt" as text (e.g. "Docking", "Charging
        # Started") — shows the words in History, unlike the Audio Event entity
        # whose state is a timestamp. audioId is sticky between frames.
        key="last_audio",
        name="Last Audio",
        icon="mdi:bullhorn-variant",
        description="The most recent audio cue the mower played (mow-start chime, charging, blade-stop, etc.), decoded from its audioId. Sticky — holds the last cue until a new one. Useful as a correlation marker (e.g. a blade-stop near a path anomaly).",
        value_source=lambda d: audio_label(d["audioId"]) if d.get("audioId") is not None else None,
    ),
    LymowSensorDesc(
        key="current_zone",
        name="Current Zone",
        icon="mdi:map-marker-radius",
        description="Which mapped zone the mower is in right now — computed locally by point-in-polygon from its RTK position against the zone boundaries (with a ~1 ft edge buffer so a perimeter lap doesn't drop it, and exclusive-area sticky logic to pick a zone when overlapping ones contain the point). It PREFERS the active task's zones in an overlap (an edge-clip into an adjacent zone while mowing resolves to the zone being mowed), but never HIDES a non-task zone — if the mower is travelling through one to reach a far zone, or gets stuck/dies/errors in one, it still reports that zone so you can always locate it. If the mower drives PAST a small buffer INTO a no-go zone (an intrusion, common before it gets stuck), this reads 'No Go: <name>' instead; riding the no-go's edge on a perimeter lap is not flagged. 'unknown' when it's between zones (e.g. in a channel).",
        value_source="currentZone",
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="auto_recharge",
        name="Auto Recharge",
        icon="mdi:battery-sync",
        value_source="rrEnabled",
        transform=lambda v: "On" if v else "Off",
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="auto_recharge_battery",
        name="Auto Recharge Battery",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-arrow-down",
        value_source="rrRechargeBat",
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="auto_resume_battery",
        name="Auto Resume Battery",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-arrow-up",
        value_source="rrResumeBat",
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="total_clean_time",
        name="Total Clean Time",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
        value_source=_history_summary("total_clean_time"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="total_clean_area",
        name="Total Clean Area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-check",
        value_source=_history_summary("total_clean_area"),
        entity_registry_enabled_default=False,
    ),
    # ── Network / Camera ──────────────────────────────────────────────────
    LymowSensorDesc(
        key="ip_address",
        name="IP Address",
        icon="mdi:ip-network",
        # ipAddress is top-level in the MQTT state dict (from PbDeviceProfile.5)
        # Fallback: netDetailInfo.wifiIp
        value_source=_robot_ip,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="rtsp_url",
        name="Camera URL",
        icon="mdi:cctv",
        # Built as: rtsp://<ip>:<RTSP_PORT>/<RTSP_PATH>
        # Use with go2rtc or a Generic Camera integration.
        value_source=lambda d: (
            f"rtsp://{ip}:{RTSP_PORT}/{RTSP_PATH}"
            if (ip := _robot_ip(d))
            else None
        ),
        entity_registry_enabled_default=False,
    ),

)


# ── ENU → WGS84 helpers (used by LymowMapGeoJsonSensor) ───────

_WGS84_A = 6_378_137.0   # equatorial radius [m]


def _enu_to_latlon(
    east_m: float, north_m: float, lat0_deg: float, lon0_deg: float
) -> tuple[float, float]:
    """Convert ENU metres to WGS84 lat/lon (accurate to < 1 cm at garden scale)."""
    lat0 = math.radians(lat0_deg)
    dlat = math.degrees(north_m / _WGS84_A)
    dlon = math.degrees(east_m  / (_WGS84_A * math.cos(lat0)))
    return lat0_deg + dlat, lon0_deg + dlon


def _sf(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def _safe_pts(points: list[Any]) -> list[tuple[float, float]]:
    """Normalise zone points: dicts {x,y} or tuples/lists (x, y)."""
    out: list[tuple[float, float]] = []
    for p in points:
        if isinstance(p, dict):
            x, y = _sf(p.get("x")), _sf(p.get("y"))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y = _sf(p[0]), _sf(p[1])
        else:
            continue
        if x is not None and y is not None:
            out.append((x, y))
    return out


def _pts_to_ring(
    pts: list[tuple[float, float]], lat0: float, lon0: float
) -> list[list[float]]:
    ring = []
    for x, y in pts:
        lat, lon = _enu_to_latlon(x, y, lat0, lon0)
        ring.append([round(lon, 8), round(lat, 8)])
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


# ── Platform setup ────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LymowSensor(coord, desc) for desc in SENSORS] + [LymowMapGeoJsonSensor(coord)] + [LymowZoneHistorySensor(coord)] + [LymowCurrentChannelSensor(coord)] + [LymowOverdueZonesSensor(coord)],
        update_before_add=False,
    )

    # Per-zone history entities — one TIMESTAMP sensor per zone, discovered from the map
    # catalog (which arrives over MQTT after setup) and for any zone added later in the app.
    # (Discovery + hashId anchoring pattern adapted from Mortimer452/Lymow-One-MQTT, MIT.)
    _seen: set[str] = set()

    @callback
    def _discover_zone_entities() -> None:
        zones = ((coord.data or {}).get("btMap") or {}).get("zones") or []
        new: list[LymowZonePerZoneSensor] = []
        for z in zones:
            hid = z.get("hashId")
            if not hid or hid in _seen:
                continue
            _seen.add(hid)
            new.append(LymowZonePerZoneSensor(coord, hid, z.get("name") or hid))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coord.async_add_listener(_discover_zone_entities))
    _discover_zone_entities()


class LymowCurrentChannelSensor(LymowEntity, SensorEntity):
    """Live channel the mower is transiting — point-in-polygon of its pose against
    each channel polygon (active mowing only). State = readable link label
    (e.g. "Front Left Main ↔ Backyard"); attributes carry the stable channel_id
    and linked zones for automations (e.g. open a gate on a front↔back transit)."""

    _attr_icon = "mdi:transit-connection-variant"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "current_channel")
        self._attr_name = "Current Channel"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("currentChannel")

    @property
    def extra_state_attributes(self):
        info = (self.coordinator.data or {}).get("currentChannelInfo") or {}
        return {
            "description": "The connector channel the mower is transiting (the corridor linking two zones), computed locally by point-in-polygon of its position against each channel. In a zone it reads 'None'; genuinely outside all zones AND channels (a geofence breach) it reads 'Off-Map'.",
            "channel_id": info.get("channel_id"),
            "zone_1": info.get("zone1"),
            "zone_2": info.get("zone2"),
            "is_docking_channel": info.get("is_docking"),
            "distance_m": info.get("distance_m"),
        }


class LymowZoneHistorySensor(LymowEntity, SensorEntity):
    _attr_icon = "mdi:map-clock"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "zone_history")
        self._attr_name = "Zone History"

    @property
    def native_value(self):
        history = (self.coordinator.data or {}).get("zone_history") or {}
        return len(history)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        history = data.get("zone_history") or {}

        return {
            "zones": history,
            "zone_count": len(history),
            # One unified record per zone (keyed by hashId). Fields:
            #   last_mowed, end_type, mow_count, coverage_points
            #   mowing_minutes  = blade-down time (cloud cleanTime)
            #   session_minutes = wall-clock incl. travel to/from the dock
            #   area_covered_m2 = total blade coverage (incl. chess overlap, cloud)
            #   zone_area_m2    = the zone's geometric area
            # NOTE: the cloud never breaks area/time/percent down PER zone — on a
            # multi-zone task those cloud values are the SESSION total across all zones.
            "per_zone_stats_available": False,
            # description surfaced for users browsing the entity
            "description": "Per-zone mow history (persists across restarts). mowing_minutes = "
                           "blade-down time; session_minutes = total incl. travel; "
                           "area_covered_m2 = blade coverage incl. overlap; zone_area_m2 = "
                           "geometric zone area. Cloud area/time are session totals on "
                           "multi-zone tasks, not per-zone (per_zone_stats_available=false).",
        }


class LymowSensor(LymowEntity, SensorEntity):
    """Generic Lymow sensor — driven by LymowSensorDesc."""

    entity_description: LymowSensorDesc

    def __init__(self, coordinator: LymowCoordinator, desc: LymowSensorDesc) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> Any:
        d = self.coordinator.data or {}
        src = self.entity_description.value_source
        raw = src(d) if callable(src) else d.get(src)
        if raw is None:
            return None
        if fn := self.entity_description.transform:
            return fn(raw)
        return raw

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs: dict[str, Any] = {}
        if fn := self.entity_description.attrs_source:
            attrs.update(fn(self.coordinator.data or {}) or {})
        if self.entity_description.description:
            attrs["description"] = self.entity_description.description
        return attrs or None


class _ZoneSubDeviceEntity(LymowEntity):
    """Mixin: place an entity under a dedicated '<name> Zones' sub-device that hangs
    off the main mower (via_device). Yards with dozens of zones generate dozens of
    per-zone entities; grouping them into their own collapsible device card keeps the
    main device page from becoming an endless scroll."""

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.thing_name}_zones")},
            "name": f"{self._device_name} Zones",
            "manufacturer": MANUFACTURER,
            "model": "Zone History",
            "via_device": (DOMAIN, self.coordinator.thing_name),
        }


class LymowZonePerZoneSensor(_ZoneSubDeviceEntity, SensorEntity, RestoreEntity):
    """One sensor PER zone — state is the last-mowed timestamp, attributes carry that
    zone's unified history record (mow_count, mowing_minutes, derived per-zone mow time,
    session_minutes, battery_used, coverage, areas). hashId-anchored so an app rename keeps
    the entity (and any automations on it); RestoreEntity keeps the timestamp across
    restarts until the next mow. The 'Zone History' overview sensor still holds all zones
    for templates/summary. (Per-zone pattern adapted from Mortimer452/Lymow-One-MQTT, MIT.)
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:map-marker-check"

    def __init__(self, coordinator: LymowCoordinator, hash_id: str, name: str) -> None:
        super().__init__(coordinator, f"zone_hist_{hash_id}")
        self._hash_id = hash_id
        self._attr_name = f"Zone {name}"
        self._restored_ts: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            try:
                self._restored_ts = datetime.fromisoformat(last.state)
            except (TypeError, ValueError):
                pass

    @property
    def _record(self) -> dict:
        return ((self.coordinator.data or {}).get("zone_history") or {}).get(self._hash_id) or {}

    @property
    def native_value(self) -> datetime | None:
        lm = self._record.get("last_mowed")
        if lm:
            try:
                return datetime.fromisoformat(lm)
            except (TypeError, ValueError):
                pass
        return self._restored_ts

    @property
    def extra_state_attributes(self) -> dict:
        rec = self._record
        # Pick up app renames live from the catalog.
        for z in ((self.coordinator.data or {}).get("btMap") or {}).get("zones") or []:
            if z.get("hashId") == self._hash_id and z.get("name"):
                self._attr_name = f"Zone {z['name']}"
                break
        keys = ("zone_name", "mow_count", "mowing_minutes", "mowing_minutes_derived",
                "session_minutes", "battery_used_pct", "coverage_points",
                "area_covered_m2", "session_area_m2", "zone_area_m2",
                "path_spacing_cm", "path_spacing_in", "cut_overlap_pct", "end_type",
                "per_zone_stats_available")
        out = {k: rec.get(k) for k in keys if k in rec}
        out["description"] = ("Per-zone mow history. PER-SESSION (last mow, matches the "
                              "timestamp): mowing_minutes_derived = true blade-down time IN "
                              "this zone (mow-only, pauses across a recharge); battery_used_pct "
                              "= battery spent mowing THIS zone (continuous in-zone drain only); "
                              "area_covered_m2 = this zone's real mowed footprint; session_minutes "
                              "= this mow's wall-clock incl. travel. LIFETIME: mow_count. "
                              "CLOUD SESSION TOTALS (all zones): mowing_minutes, session_area_m2; "
                              "zone_area_m2 = the zone's geometric size.")
        return out


class LymowOverdueZonesSensor(LymowEntity, SensorEntity):
    """How many zones are overdue for a mow (#2). A zone is overdue when its last
    COMPLETED mow is older than the Mow Interval — so a cancelled / rained-out zone
    correctly stays "due". Never-mowed zones are NOT counted (unknown, not overdue).
    State = the count; the attribute lists the overdue zones (oldest first) so you can
    notify on it ("3 zones overdue, including Front Right") instead of reading the map."""

    _attr_icon = "mdi:mower"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "overdue_zones")
        self._attr_name = "Overdue Zones"

    def _compute(self) -> tuple[int, list[dict]]:
        data = self.coordinator.data or {}
        ages = data.get("zone_last_mowed") or {}          # {zone_key: epoch-seconds | None}
        if not ages:
            return 0, []
        zones = (data.get("btMap") or {}).get("zones") or []
        name_by_key = {(z.get("hashId") or z.get("name")): (z.get("name") or z.get("hashId"))
                       for z in zones}
        interval_days = float(data.get("mow_interval_days") or DEFAULT_MOW_INTERVAL_DAYS)
        now = time.time()
        overdue = []
        for key, ts in ages.items():
            if not ts:
                continue                                   # never completed → unknown, not overdue
            days = (now - float(ts)) / 86400.0
            if days > interval_days:
                overdue.append({"zone": name_by_key.get(key, key), "days_since_mowed": round(days, 1)})
        overdue.sort(key=lambda z: z["days_since_mowed"], reverse=True)
        return len(overdue), overdue

    @property
    def native_value(self) -> int:
        return self._compute()[0]

    @property
    def extra_state_attributes(self) -> dict:
        count, overdue = self._compute()
        data = self.coordinator.data or {}
        return {
            "mow_interval_days": float(data.get("mow_interval_days") or DEFAULT_MOW_INTERVAL_DAYS),
            "overdue_zones": [z["zone"] for z in overdue],
            "detail": overdue,
            "oldest_zone": overdue[0]["zone"] if overdue else None,
            "oldest_days": overdue[0]["days_since_mowed"] if overdue else None,
            "description": ("Zones whose last COMPLETED mow is older than the Mow Interval. "
                            "Cancelled/rained-out zones stay due (timestamp only advances on "
                            "completion); never-mowed zones are not counted."),
        }


class LymowMapGeoJsonSensor(LymowEntity, SensorEntity):
    """Exposes the Lymow zone map as a GeoJSON FeatureCollection.

    State  : "<N> zones"
    Attr   : geojson -> FeatureCollection (WGS84 when enuBasePoint available)

    Consumed by custom Lovelace map cards and by the Flutter control app
    via HA WebSocket / REST.

    Performance:
    - Cache: rebuilds only when (zone_count, mow_path_len, has_origin) changes,
      avoiding repeated ENU->WGS84 conversions on every MQTT push (~1 s).
    - Decimation: max 150 pts/zone, max 500 pts for mow path to stay under
      HA's 16 KB attribute limit with 52+ zones.
    """

    _attr_name = "Map GeoJSON"
    _attr_icon = "mdi:map-marker-path"

    # The GeoJSON FeatureCollections are large (well over the recorder's 16 KB
    # attribute cap on big maps) and have no value as DB history — they're consumed
    # live by map cards. Tell the recorder to skip them: kills the "attributes exceed
    # maximum size" warning + DB churn while leaving the live attributes full-size.
    _unrecorded_attributes = frozenset({
        "geojson", "geojson_zones", "geojson_nogo_zones", "geojson_mowed_area",
        "geojson_dock", "geojson_robot", "geojson_rtk_antenna",
    })

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "map_geojson")
        self._geojson_cache: dict | None = None
        self._cache_key: tuple | None = None  # (zone_count, mow_path_len, has_origin)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> str:
        btmap = (self.coordinator.data or {}).get("btMap") or {}
        zones = btmap.get("zones") or []
        drawable = sum(1 for z in zones if z.get("points") and len(z.get("points") or []) >= 3)
        return f"{drawable} zones"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data  = self.coordinator.data or {}
        btmap = data.get("btMap") or {}
        zones = btmap.get("zones") or []

        nogo_zones = btmap.get("nogoZones") or []

        ebp  = btmap.get("enuBasePoint") or data.get("enu_base_point") or {}
        lat0 = _sf(ebp.get("latitude"))
        lon0 = _sf(ebp.get("longitude"))
        has_origin = lat0 is not None and lon0 is not None

        # Only rebuild when something meaningful actually changed.
        mowed_polygons = data.get("mowed_area_polygons") or []

        mowed_area_points_count = sum(
            len(poly)
            for poly in mowed_polygons
            if isinstance(poly, list)
        )

        nogo_points_count = sum(
            len(z.get("points") or [])
            for z in nogo_zones
            if isinstance(z, dict)
        )

        cache_key = (
            len(zones),
            len(nogo_zones),
            nogo_points_count,
            len(mowed_polygons),
            mowed_area_points_count,
            has_origin,
        )
        if cache_key == self._cache_key and self._geojson_cache is not None:
            return self._geojson_cache

        features: list[dict[str, Any]] = []

        zone_features: list[dict[str, Any]] = []
        nogo_features: list[dict[str, Any]] = []
        mowed_area_features: list[dict[str, Any]] = []
        dock_features: list[dict[str, Any]] = []
        robot_features: list[dict[str, Any]] = []
        rtk_features: list[dict[str, Any]] = []

        # Zone polygons — decimated to max 150 pts each
        for idx, zone in enumerate(zones):
            pts = _safe_pts(zone.get("points") or [])
            if len(pts) < 3:
                continue
            if len(pts) > 150:
                step = max(1, len(pts) // 150)
                pts  = pts[::step]
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
            props: dict[str, Any] = {
                "type":     "zone",
                "name":     zone.get("name") or zone.get("hashId") or str(idx),
                "hashId":   zone.get("hashId"),
                "zoneType": zone.get("zoneType"),
            }
            if has_origin:
                geometry: dict[str, Any] = {
                    "type":        "Polygon",
                    "coordinates": [_pts_to_ring(pts, lat0, lon0)],
                }
            else:
                geometry = {
                    "type":        "Polygon",
                    "coordinates": [[[p[0], p[1]] for p in pts] + [list(pts[0])]],
                    "_crs":        "ENU_metres",
                }
            feature = {"type": "Feature", "properties": props, "geometry": geometry}
            features.append(feature)
            zone_features.append(feature)
        
        # No-go zone polygons — orange/excluded areas
        for idx, zone in enumerate(nogo_zones):
            pts = _safe_pts(zone.get("points") or [])
            if len(pts) < 3:
                continue

            if len(pts) > 300:
                step = max(1, len(pts) // 300)
                pts = pts[::step]

            props: dict[str, Any] = {
                "type": "nogo_zone",
                "name": zone.get("name") or zone.get("hashId") or f"No-Go {idx + 1}",
                "hashId": zone.get("hashId"),
                "zoneType": zone.get("zoneType"),
                "linkedZoneHashIds": zone.get("linkedZoneHashIds") or [],
            }

            if has_origin:
                geometry: dict[str, Any] = {
                    "type": "Polygon",
                    "coordinates": [_pts_to_ring(pts, lat0, lon0)],
                }
            else:
                ring = [[p[0], p[1]] for p in pts]
                if ring and ring[0] != ring[-1]:
                    ring.append(ring[0])
                geometry = {
                    "type": "Polygon",
                    "coordinates": [ring],
                    "_crs": "ENU_metres",
                }

            feature = {
                "type": "Feature",
                "properties": props,
                "geometry": geometry,
            }
            features.append(feature)
            nogo_features.append(feature)

        # Dock
        dock = data.get("chargingStationLoc")
        if isinstance(dock, dict):
            x, y = _sf(dock.get("x")), _sf(dock.get("y"))
            if x is not None and y is not None:
                if has_origin:
                    lat, lon = _enu_to_latlon(x, y, lat0, lon0)
                    coords: list[float] = [round(lon, 8), round(lat, 8)]
                else:
                    coords = [x, y]
                feature = {
                    "type": "Feature",
                    "properties": {"type": "dock", "name": "Dock", "heading": _sf(dock.get("heading"))},
                    "geometry": {"type": "Point", "coordinates": coords},
                }
                features.append(feature)
                dock_features.append(feature)

        # Robot position
        robot = data.get("robotLoc") or data.get("pose") or data.get("robotPosePib")
        if isinstance(robot, dict):
            x, y = _sf(robot.get("x")), _sf(robot.get("y"))
            if x is not None and y is not None:
                if has_origin:
                    lat, lon = _enu_to_latlon(x, y, lat0, lon0)
                    coords = [round(lon, 8), round(lat, 8)]
                else:
                    coords = [x, y]
                feature = {
                    "type": "Feature",
                    "properties": {
                        "type": "robot",
                        "name": "Robot",
                        "heading": _sf(robot.get("heading") or robot.get("theta")),
                    },
                    "geometry": {"type": "Point", "coordinates": coords},
                }
                features.append(feature)
                robot_features.append(feature)

        # RTK base station antenna — physically located at the ENU origin
        # (btMap.enuBasePoint), the survey datum RTK positions are referenced to.
        # The app maps the dock and mower but not this; surface it as its own pin.
        # By definition it's the origin: (lat0,lon0) in WGS84, or (0,0) in ENU
        # fallback — so it renders alongside the dock/robot in either frame.
        if has_origin:
            rtk_coords: list[float] = [round(lon0, 8), round(lat0, 8)]
        else:
            rtk_coords = [0.0, 0.0]
        feature = {
            "type": "Feature",
            "properties": {"type": "rtk_antenna", "name": "RTK Antenna"},
            "geometry": {"type": "Point", "coordinates": rtk_coords},
        }
        features.append(feature)
        rtk_features.append(feature)

        # Mowed area polygons from QUERY_PATH
        for idx, poly in enumerate(mowed_polygons):
            pts = _safe_pts(poly)
            if len(pts) < 3:
                continue

            if len(pts) > 800:
                step = max(1, len(pts) // 800)
                pts = pts[::step]

            if has_origin:
                ring = _pts_to_ring(pts, lat0, lon0)
            else:
                ring = [[p[0], p[1]] for p in pts]
                if ring and ring[0] != ring[-1]:
                    ring.append(ring[0])

            feature = {
                "type": "Feature",
                "properties": {
                    "type": "mowed_area",
                    "name": f"Mowed Area {idx + 1}",
                    "point_count": len(poly),
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [ring],
                },
            }
            features.append(feature)
            mowed_area_features.append(feature)

        result = {
            "geojson": {"type": "FeatureCollection", "features": features},

            "geojson_zones": {
                "type": "FeatureCollection",
                "features": zone_features,
            },
            "geojson_nogo_zones": {
                "type": "FeatureCollection",
                "features": nogo_features,
            },
            "geojson_mowed_area": {
                "type": "FeatureCollection",
                "features": mowed_area_features,
            },
            "geojson_dock": {
                "type": "FeatureCollection",
                "features": dock_features,
            },
            "geojson_robot": {
                "type": "FeatureCollection",
                "features": robot_features,
            },
            "geojson_rtk_antenna": {
                "type": "FeatureCollection",
                "features": rtk_features,
            },

            "zone_count": len(zones),
            "nogo_zone_count": len(nogo_zones),
            "mowed_area_polygon_count": len(mowed_polygons),
            "mowed_area_points_count": mowed_area_points_count,
            "feature_count": len(features),
            "has_gps_origin": has_origin,
            "enu_base_point": ebp or None,
        }
        self._geojson_cache = result
        self._cache_key     = cache_key
        return result