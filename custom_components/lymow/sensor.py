"""Lymow sensor platform."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfArea, UnitOfLength, UnitOfSpeed, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CLEAN_MODE_ADAPTIVE_ZIGZAG,
    CLEAN_MODE_CHESS_BOARD,
    CLEAN_MODE_PERIMETER_ONLY,
    CLEAN_MODE_ZIGZAG,
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


# ── Descriptor ────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class LymowSensorDesc(SensorEntityDescription):
    value_source: str | Callable[[dict], Any] = ""
    transform: Callable[[Any], Any] | None = None


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


def _history_summary(key: str) -> Callable[[dict], Any]:
    return lambda d: (d.get("cleanHistorySummary") or {}).get(key)


def _btmap(key: str) -> Callable[[dict], Any]:
    return lambda d: (d.get("btMap") or {}).get(key)


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
            return sum(1 for z in (btmap.get("zones") or []) if isinstance(z, dict) and z.get("points"))
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
        name="Error",
        icon="mdi:alert-circle-outline",
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
        value_source="cleanPercent",
        transform=lambda v: int(round(float(v) * 100)) if v is not None else None,
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

    # ── GNSS / Localization ─────────────────────────────────────────────────
    LymowSensorDesc(
        key="gnss_satellites",
        name="GNSS Satellites",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-variant",
        value_source="gnssNumSatellites",
    ),
    LymowSensorDesc(
        key="gnss_horizontal_accuracy",
        name="GNSS Horizontal Accuracy",
        native_unit_of_measurement=UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:crosshairs-gps",
        value_source="gnssHorizontalAccuracy",
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
    ),
    LymowSensorDesc(
        key="rtk_satellites",
        name="RTK Satellites",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-variant",
        value_source=_rtk("satelliteCount"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="rtk_l1_satellites",
        name="RTK L1 Satellites",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-variant",
        value_source=_rtk("l1SatelliteCount"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="rtk_l2_satellites",
        name="RTK L2 Satellites",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-variant",
        value_source=_rtk("l2SatelliteCount"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="rtk_base_status",
        name="RTK Base Status",
        icon="mdi:access-point",
        value_source=_rtk("baseStationStatus"),
        transform=lambda v: {0: "Online", 1: "Offline", 2: "Moved", 3: "Invalid"}.get(v, f"Unknown ({v})"),
        entity_registry_enabled_default=False,
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
    ),
    LymowSensorDesc(
        key="sim_iccid",
        name="SIM ICCID",
        icon="mdi:sim",
        value_source=_net("simIccid"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="sim_ip",
        name="SIM IP",
        icon="mdi:ip-network-outline",
        value_source=_net("simIp"),
        entity_registry_enabled_default=False,
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
        key="rtk_base_serial",
        name="RTK Base Serial",
        icon="mdi:radio-tower",
        value_source="rtkSn",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="wheel_firmware",
        name="Wheel Motor Firmware",
        icon="mdi:tire",
        value_source="wheelVer",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="blade_firmware",
        name="Blade Motor Firmware",
        icon="mdi:fan",
        value_source="knifeVer",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="audio_volume",
        name="Speaker Volume",
        icon="mdi:volume-high",
        value_source="audioVolume",
        transform=lambda v: {0: "Mute", 30: "Low", 70: "Medium", 100: "High"}.get(v, f"{v}%"),
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
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        # Readable "last voice prompt" as text (e.g. "Docking", "Charging
        # Started") — shows the words in History, unlike the Audio Event entity
        # whose state is a timestamp. audioId is sticky between frames.
        key="last_audio",
        name="Last Audio",
        icon="mdi:bullhorn-variant",
        value_source=lambda d: audio_label(d["audioId"]) if d.get("audioId") is not None else None,
    ),
    LymowSensorDesc(
        key="current_zone",
        name="Current Zone",
        icon="mdi:map-marker-radius",
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

    LymowSensorDesc(
        key="path_points",
        name="Path Points",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:vector-polyline",
        value_source=lambda d: (d.get("path_data") or {}).get("points_count"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="path_segments",
        name="Path Segments",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:vector-line",
        value_source=lambda d: (d.get("path_data") or {}).get("segment_count"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="path_length",
        name="Path Length",
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value_source=lambda d: (d.get("path_data") or {}).get("path_length_m"),
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
        [LymowSensor(coord, desc) for desc in SENSORS] + [LymowMapGeoJsonSensor(coord)] + [LymowZoneHistorySensor(coord)] + [LymowCurrentChannelSensor(coord)],
        update_before_add=False,
    )


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
            # Lymow cloud telemetry only reports a single session-level clean
            # summary (area/time/percent) plus the list of zones in the task. It
            # does NOT break those stats down per zone, so any duration/area/percent
            # shown per zone is the SESSION total covering all zones, not that zone
            # alone. This flag documents that limitation for dashboards/automations.
            "per_zone_stats_available": False,
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
        drawable = sum(1 for z in zones if isinstance(z, dict) and z.get("points") and len(z.get("points") or []) >= 3)
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