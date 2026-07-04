"""Lymow protocol layer — protobuf-first encode/decode + btMap wire parser.

This module uses the generated protobuf classes for normal PbInput/PbOutput
handling and keeps the low-level wire parser only for Lymow's nested btMap
queryAck blobs, where parts of the recovered .proto are incomplete/opaque.

Generated protobuf module location expected by this integration:
    custom_components/lymow/proto/lymow_pb2.py
"""
from __future__ import annotations

import base64
import json
import logging
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from .proto import lymow_pb2 as pb

_DAYS_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

_LOGGER = logging.getLogger(__name__)

PB_VERSION_4_9 = 40

USER_CTRL_CLEAN                  = 1
USER_CTRL_DOCK                   = 2
USER_CTRL_PAUSE                  = 3
USER_CTRL_RESUME                 = 4
USER_CTRL_QUERY_MAP              = 19
USER_CTRL_QUERY_SCHEDULES        = 20
USER_CTRL_PAUSE_DOCK             = 21
USER_CTRL_RESUME_DOCK            = 22
USER_CTRL_QUERY_PATH             = 23
USER_CTRL_QUERY_CLEANING         = 24
USER_CTRL_OTA                    = 26   # trigger firmware OTA
USER_CTRL_ABORT_OTA              = 27   # abort in-progress OTA
USER_CTRL_FORCE_REINIT           = 28
USER_CTRL_RECHARGE_DOCK          = 33
USER_CTRL_QUERY_CLEANING_SUMMARY = 34
USER_CTRL_QUERY_ROBOT_CFG        = 35
USER_CTRL_QUERY_RUN_TIME_CONFIG  = 51
USER_CTRL_QUERY_WIFI_4G          = 52
USER_CTRL_QUERY_NET_DETAIL       = 53
USER_CTRL_QUERY_RTK_L1           = 57
USER_CTRL_QUERY_RTK_L2           = 58

# cleanMode int -> string (PbZoneConfig.cleanMode values). CORRECTED 2026-06-06 by decompiling
# the Lymow 3.0.7 Hermes bundle (CleanModeType enum, declaration order = ordinal, cross-checked
# vs MOW_END_NONE=0). The old table had 2/3/4 wrong (it claimed 2=CHESS,3=PERIMETER,4=ADAPTIVE),
# which mislabeled every cross-pattern zone (cleanMode=3) as "perimeter-only". Verified against
# Nate's live zones: his cross/double zones = 3 = CHESS_BOARD, Right Side single = 1 = ZIGZAG.
CLEAN_MODE_INT = {
    0: "NONE",
    1: "ZIGZAG_MODE",            # single pass
    2: "ADAPTIVE_ZIGZAG_MODE",   # single pass, optimized direction
    3: "CHESS_BOARD_MODE",       # cross pattern / DOUBLE pass
    4: "PERIMETER_LAPS_ONLY_MODE",
}
CLEAN_MODE_STR = {v: k for k, v in CLEAN_MODE_INT.items() if k != 0}


# ---------------------------------------------------------------------------
# Dataclasses used by HA entities/camera/selects
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ZoneInfo:
    hash_id: str
    name: str
    mow_order: int = 0
    is_enabled: bool = True
    polygon_points: list[tuple[float, float]] = field(default_factory=list)
    zone_config: dict[str, Any] = field(default_factory=dict)
    text_pos: tuple[float, float] | None = None
    zone_type: int | None = None
    area: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "hashId": self.hash_id,
            "name": self.name,
            "mowOrder": self.mow_order,
            "isEnabled": self.is_enabled,
            "points": self.polygon_points,
            "points_count": len(self.polygon_points),
        }
        if self.zone_type is not None:
            out["zoneType"] = self.zone_type
        if self.zone_config:
            out["zoneConfig"] = self.zone_config
        if self.text_pos is not None:
            out["textPos"] = {"x": self.text_pos[0], "y": self.text_pos[1]}
        if self.area is not None:
            out["area"] = self.area
        return out


@dataclass(slots=True)
class ChannelInfo:
    hash_id: str
    zone1: str = ""
    zone2: str = ""
    is_valid: bool | None = None
    is_docking_channel: bool = False
    # Per-channel config (PbChannel fields 8/9/10) — the obstacle-detection mode,
    # cut height and blade-lift used when transiting this corridor.
    detect_mode: int | None = None
    cut_height: int | None = None
    channel_lift: bool | None = None
    polygon_points: list[tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "hashId": self.hash_id,
            "zone1": self.zone1,
            "zone2": self.zone2,
            "isDockingChannel": self.is_docking_channel,
            "points": self.polygon_points,
            "points_count": len(self.polygon_points),
        }
        if self.is_valid is not None:
            out["isValid"] = self.is_valid
        if self.detect_mode is not None:
            out["detectMode"] = self.detect_mode
        if self.cut_height is not None:
            out["cutHeight"] = self.cut_height
        if self.channel_lift is not None:
            out["channelLift"] = self.channel_lift
        return out

@dataclass(slots=True)
class NoGoZoneInfo:
    """One no-go zone / excluded area from QUERY_MAP."""

    hash_id: str
    name: str
    is_enabled: bool
    polygon_points: list[tuple[float, float]]
    linked_zone_hash_ids: list[str] = field(default_factory=list)
    zone_type: int | None = None
    area: float | None = None
    points_source: str | None = None
    bound_00: tuple[float, float] | None = None
    bound_11: tuple[float, float] | None = None
    inner_point: tuple[float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "hashId": self.hash_id,
            "name": self.name,
            "isEnabled": self.is_enabled,
            "points": self.polygon_points,
            "points_count": len(self.polygon_points),
            "linkedZoneHashIds": self.linked_zone_hash_ids,
        }

        if self.zone_type is not None:
            out["zoneType"] = self.zone_type

        if self.area is not None:
            out["area"] = self.area

        if self.points_source:
            out["points_source"] = self.points_source

        if self.bound_00 is not None:
            out["bound_00"] = self.bound_00

        if self.bound_11 is not None:
            out["bound_11"] = self.bound_11

        if self.inner_point is not None:
            out["innerPoint"] = self.inner_point

        return out


@dataclass(slots=True)
class ZoneCatalog:
    zones: list[ZoneInfo] = field(default_factory=list)
    channels: list[ChannelInfo] = field(default_factory=list)
    nogo_zones: list[NoGoZoneInfo] = field(default_factory=list)
    zones_by_hashid: dict[str, ZoneInfo] = field(default_factory=dict)
    runtime_config: dict[str, Any] | None = None
    enu_base_point: dict[str, Any] | None = None
    charging_station_loc: dict[str, Any] | None = None

    def to_btmap_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "zones": [z.to_dict() for z in self.zones],
            "zone_count": len(self.zones),
            "zones_with_points": sum(1 for z in self.zones if z.polygon_points),

            "nogoZones": [z.to_dict() for z in self.nogo_zones],
            "nogo_zone_count": len(self.nogo_zones),
            "nogo_zones_with_points": sum(
                1 for z in self.nogo_zones if z.polygon_points
            ),

            "channels": [c.to_dict() for c in self.channels],
            "channels_with_points": sum(1 for c in self.channels if c.polygon_points),
        }
        if self.runtime_config:
            out["runTimeConfig"] = self.runtime_config
            for k in ("cutHeight", "cutSpeed", "moveSpeed"):
                if k in self.runtime_config:
                    out[k] = self.runtime_config[k]
        if self.enu_base_point:
            out["enuBasePoint"] = self.enu_base_point
        if self.charging_station_loc:
            out["chargingStationLoc"] = self.charging_station_loc
        return out

@dataclass(slots=True)
class ScheduleConfigInfo:
    hash_id: str
    cut_height: int | None = None
    move_speed: float | None = None
    clean_dir: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hashId": self.hash_id,
            "cutHeight": self.cut_height,
            "moveSpeed": self.move_speed,
            "cleanDir": self.clean_dir,
        }


@dataclass(slots=True)
class ScheduleZoneInfo:
    hash_id: str
    name: str | None = None
    mow_order: int = 0
    text_pos: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "hashId": self.hash_id,
            "mowOrder": self.mow_order,
        }

        if self.name:
            out["name"] = self.name

        if self.text_pos:
            out["textPos"] = self.text_pos

        return out


@dataclass(slots=True)
class ScheduleInfo:
    id: int
    hour: int
    minute: int
    days_of_week: list[int]
    day_names: list[str]
    timezone: int
    is_repeated: bool
    is_disabled: bool
    is_angle_offset: bool
    mow_angle: int
    zones: list[ScheduleZoneInfo] = field(default_factory=list)
    config: list[ScheduleConfigInfo] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return not self.is_disabled

    @property
    def time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def zone_hash_ids(self) -> list[str]:
        return [z.hash_id for z in sorted(self.zones, key=lambda z: z.mow_order or 999)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "enabled": self.enabled,
            "time": self.time,
            "hour": self.hour,
            "minute": self.minute,
            "daysOfWeek": self.days_of_week,
            "dayNames": self.day_names,
            "timezone": self.timezone,
            "isRepeated": self.is_repeated,
            "isDisabled": self.is_disabled,
            "isAngleOffset": self.is_angle_offset,
            "mowAngle": self.mow_angle,
            "zoneHashIds": self.zone_hash_ids,
            "zones": [z.to_dict() for z in self.zones],
            "config": [c.to_dict() for c in self.config],
        }
# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------

def wrap_envelope(raw: bytes) -> str:
    """Wrap raw protobuf bytes in Lymow/AWS IoT JSON envelope."""
    return json.dumps({"message": base64.b64encode(raw).decode("ascii")})


def unwrap_envelope(envelope_bytes: bytes) -> bytes | None:
    """Unwrap JSON {message:<base64>} or accept raw/base64 payloads."""
    if not envelope_bytes:
        return None
    stripped = envelope_bytes.lstrip()
    if stripped.startswith(b"{"):
        try:
            obj = json.loads(envelope_bytes.decode("utf-8"))
            for key in ("message", "value", "data", "payload"):
                v = obj.get(key)
                if isinstance(v, str):
                    return base64.b64decode(v)
        except Exception:
            _LOGGER.debug("Failed to unwrap JSON MQTT envelope", exc_info=True)
            return None
    try:
        return base64.b64decode(envelope_bytes, validate=True)
    except Exception:
        # Some tests/callers may already pass raw protobuf bytes.
        return envelope_bytes




# ---------------------------------------------------------------------------
# Minimal raw encoder fallback for fields that are not correctly represented
# by the recovered .proto. Keep this limited: normal commands use pb.PbInput.
# ---------------------------------------------------------------------------

def _raw_enc_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | 0x80 if value else b)
        if not value:
            return bytes(out)

def _raw_enc_i32(field_no: int, value: int) -> bytes:
    return _raw_enc_varint((field_no << 3) | 0) + _raw_enc_varint(value)

def _raw_enc_len(field_no: int, data: bytes) -> bytes:
    return _raw_enc_varint((field_no << 3) | 2) + _raw_enc_varint(len(data)) + data


# ---------------------------------------------------------------------------
# PbInput encoders — protobuf first
# ---------------------------------------------------------------------------

def _new_input() -> pb.PbInput:
    msg = pb.PbInput()
    msg.version = PB_VERSION_4_9
    return msg


def encode_userctrl(user_ctrl: int) -> bytes:
    """Encode PbInput {version=40, userCtrl=N}."""
    msg = _new_input()
    msg.userCtrl = int(user_ctrl)
    return msg.SerializeToString()


def encode_play_audio(audio_id: int) -> bytes:
    """Make the mower play a voice prompt / sound by AudioId (PbInput.audioId,
    field 16) — e.g. a locate / find-my-mower beep. IDs: see AUDIO_ID_LABELS."""
    msg = _new_input()
    msg.audioId = int(audio_id)
    return msg.SerializeToString()


def encode_query_map(query_index: int = 0) -> bytes:
    """Query full map via PbInput.btMap.queryMap."""
    msg = _new_input()
    msg.userCtrl = USER_CTRL_QUERY_MAP
    msg.btMap.queryIndex = int(query_index)
    msg.btMap.queryMap = True
    return msg.SerializeToString()


def encode_query_path(query_index: int = 0) -> bytes:
    """Query path data via PbInput.btMap.queryPath."""
    msg = _new_input()
    msg.userCtrl = USER_CTRL_QUERY_PATH
    msg.btMap.queryIndex = int(query_index)
    msg.btMap.queryPath = True
    return msg.SerializeToString()


def encode_query_schedules() -> bytes:
    return encode_userctrl(USER_CTRL_QUERY_SCHEDULES)


def encode_query_cleaning_info() -> bytes:
    return encode_userctrl(USER_CTRL_QUERY_CLEANING)


def encode_query_cleaning_summary() -> bytes:
    return encode_userctrl(USER_CTRL_QUERY_CLEANING_SUMMARY)


def encode_query_robot_config() -> bytes:
    return encode_userctrl(USER_CTRL_QUERY_ROBOT_CFG)


def encode_query_wifi_4g() -> bytes:
    return encode_userctrl(USER_CTRL_QUERY_WIFI_4G)


def encode_query_net_detail() -> bytes:
    return encode_userctrl(USER_CTRL_QUERY_NET_DETAIL)


def encode_query_rtk_l1() -> bytes:
    """RTK L1 query — uses version=49 (app 3.0.6) instead of global PB_VERSION."""
    msg = pb.PbInput()
    msg.version = 49
    msg.userCtrl = USER_CTRL_QUERY_RTK_L1
    return msg.SerializeToString()


def encode_query_rtk_l2() -> bytes:
    """RTK L2 query — uses version=49 (app 3.0.6) instead of global PB_VERSION."""
    msg = pb.PbInput()
    msg.version = 49
    msg.userCtrl = USER_CTRL_QUERY_RTK_L2
    return msg.SerializeToString()


def encode_debug_setting(
    *,
    upload_log: bool = False,
    upload_version: bool = False,
    query_wifi_config: bool = False,
    upload_robot_config: bool = False,
    upload_task_config: bool = False,
    exec_cmd: str | None = None,
) -> bytes:
    """Encode PbInput.debugSetting payload."""
    msg = _new_input()
    if upload_log:
        msg.debugSetting.uploadLog = True
    if upload_version:
        msg.debugSetting.uploadVersion = True
    if query_wifi_config:
        msg.debugSetting.queryWifiConfig = True
    if upload_robot_config:
        msg.debugSetting.uploadRobotConfig = True
    if upload_task_config:
        msg.debugSetting.uploadTaskConfig = True
    if exec_cmd:
        msg.debugSetting.execCmd = exec_cmd
    return msg.SerializeToString()


def encode_query_device_profile() -> bytes:
    return encode_debug_setting(upload_version=True, upload_robot_config=True)


def encode_query_wifi_config_debug() -> bytes:
    return encode_debug_setting(query_wifi_config=True)


def encode_upload_robot_config() -> bytes:
    """Trigger robotConfig broadcast without userCtrl."""
    return encode_debug_setting(upload_robot_config=True)


def encode_app_connect(client_uuid: str) -> bytes:
    msg = _new_input()
    msg.appConnect = 2
    msg.uuid = client_uuid
    return msg.SerializeToString()


def encode_start_zones(zone_hash_ids: list[str]) -> bytes:
    """Start mowing selected zones using PbInput.map.goZones."""
    msg = _new_input()
    msg.userCtrl = USER_CTRL_CLEAN
    for i, hash_id in enumerate(zone_hash_ids, start=1):
        if not hash_id:
            continue
        zone = msg.map.goZones.add()
        zone.basicInfo.hashId = hash_id
        zone.basicInfo.mowOrder = i
    return msg.SerializeToString()


def encode_set_cut_height(cut_height_mm: int) -> bytes:
    msg = _new_input()
    msg.map.runTimeConfig.cutHeight = int(cut_height_mm)
    return msg.SerializeToString()


def encode_set_clean_mode(mode_int: int) -> bytes:
    """Set global mowing mode.

    This is intentionally encoded with the tiny raw fallback instead of
    ``pb.PbInput().robotConfig``: the recovered Python schema maps
    PbRobotConfig field 7 as ``isOpenLed``, while live captures from the app
    showed the clean-mode command using PbInput.robotConfig field 7 as an int.
    Using pb2 here would turn the value into a boolean and could toggle LED
    state instead of setting the mowing mode.
    """
    robot_config = _raw_enc_i32(7, int(mode_int))
    return _raw_enc_i32(2, PB_VERSION_4_9) + _raw_enc_len(13, robot_config)

def encode_remote_control(
    linear_speed: float = 0.0,
    angular_speed: float = 0.0,
) -> bytes:
    """Encode remote/manual movement command.

    linear_speed:
      > 0 forward
      < 0 backward

    angular_speed:
      > 0 rotate one direction
      < 0 rotate the opposite direction

    The sign for left/right may need to be swapped after testing.
    """
    msg = _new_input()
    msg.remoteControl.linearSpeed = float(linear_speed)
    msg.remoteControl.angularSpeed = float(angular_speed)
    return msg.SerializeToString()


def encode_remote_stop() -> bytes:
    """Stop remote/manual movement."""
    return encode_remote_control(0.0, 0.0)


def encode_set_audio_volume(volume: int) -> bytes:
    """Set speaker volume. Values: 0=Mute, 30=Low, 70=Medium, 100=High."""
    msg = pb.PbInput()
    msg.version = PB_VERSION_4_9
    msg.robotConfig.audioVolume = int(volume)
    msg.debugSetting.uploadRobotConfig = True
    return msg.SerializeToString()


def encode_set_task_config(
    charging_mode: int,
    rain_cleaning: bool,
    zone_order: int = 0,
    disable_charging_park: bool = False,
) -> bytes:
    """Write the FULL PbTaskConfig (userCtrl=36).

    The mower applies the whole taskConfig message, so every field must be sent
    on every write — sending just one field zeroes the rest. (Verified on
    hardware 2026-06-02: toggling Rainy Mowing alone reset chargingMode to
    Follow Perimeter.) Callers pass the current values of the fields they are
    not changing; see LymowCoordinator._current_task_config.

    chargingMode: 0=Follow Perimeter, 1=Direct Route (matches select.py).
    """
    msg = _new_input()
    msg.userCtrl = 36  # USER_CTRL_SET_TASK_CONFIG
    msg.taskConfig.chargingMode = int(charging_mode)
    msg.taskConfig.rainCleaning = bool(rain_cleaning)
    msg.taskConfig.zoneOrder = int(zone_order)
    msg.taskConfig.disableChargingPark = bool(disable_charging_park)
    return msg.SerializeToString()


def encode_set_dock_on_error(enabled: bool) -> bytes:
    """Set whether the mower returns to dock on error."""
    msg = pb.PbInput()
    msg.version = PB_VERSION_4_9
    msg.robotConfig.dockOnError = bool(enabled)
    msg.debugSetting.uploadRobotConfig = True
    return msg.SerializeToString()


def encode_set_rr_config(
    *,
    enable_rr: bool,
    recharge_bat: int | None = None,
    resume_bat: int | None = None,
    period_start_hour: int | None = None,
    period_start_minute: int | None = None,
    period_end_hour: int | None = None,
    period_end_minute: int | None = None,
) -> bytes:
    """Encode no-userCtrl robotConfig.rrConfig update."""
    msg = _new_input()
    rr = msg.robotConfig.rrConfig
    rr.enableRr = bool(enable_rr)
    if recharge_bat is not None:
        rr.rechargeBat = int(recharge_bat)
    if resume_bat is not None:
        rr.resumeBat = int(resume_bat)
    if period_start_hour is not None:
        rr.resumePeriodStart.hour = int(period_start_hour)
    if period_start_minute is not None:
        rr.resumePeriodStart.minute = int(period_start_minute)
    if period_end_hour is not None:
        rr.resumePeriodEnd.hour = int(period_end_hour)
    if period_end_minute is not None:
        rr.resumePeriodEnd.minute = int(period_end_minute)
    msg.debugSetting.uploadRobotConfig = True
    return msg.SerializeToString()

# SocSignal — real-time commands carried on PbRobotConfig.signal (field 8).
SIGNAL_TURN_ON_CAMERA_LIGHT  = 6
SIGNAL_TURN_OFF_CAMERA_LIGHT = 7
SIGNAL_TURN_ON_VEHICLE_LIGHT  = 10
SIGNAL_TURN_OFF_VEHICLE_LIGHT = 11


def encode_set_headlights(enabled: bool) -> bytes:
    """Turn the headlight (camera LED) on or off.

    Matches the app's RobotCommands.switchCameraLed(): a real-time SocSignal on
    PbRobotConfig.signal (field 8) — SIGNAL_TURN_ON_CAMERA_LIGHT (6) /
    SIGNAL_TURN_OFF_CAMERA_LIGHT (7) — with an otherwise-empty robotConfig.

    NOTE: the old code set isOpenLed (field 7), which this firmware IGNORES —
    confirmed via app bytecode. State reads back from camLedStatus (3=on, 4=off).
    Only effective while the mower is awake/IoT-connected; the firmware auto-offs
    the camera LED on dock/charge regardless.
    """
    msg = pb.PbInput()
    msg.version = PB_VERSION_4_9
    msg.robotConfig.signal = (
        SIGNAL_TURN_ON_CAMERA_LIGHT if enabled else SIGNAL_TURN_OFF_CAMERA_LIGHT
    )
    return msg.SerializeToString()


def encode_set_vehicle_led(enabled: bool) -> bytes:
    """Turn the vehicle LEDs on or off.

    The vehicle LEDs are the indicator LEDs on top of the mower plus the LCD
    backlight — distinct from the headlight/camera LED. Matches the app's
    RobotCommands.switchVehicleLed(): a real-time SocSignal on PbRobotConfig.signal
    (field 8) — SIGNAL_TURN_ON_VEHICLE_LIGHT (10) / SIGNAL_TURN_OFF_VEHICLE_LIGHT
    (11) — with an otherwise-empty robotConfig. Same cloud transport as the
    headlight command.

    State reads back from vehLedStatus (3=on, 4=off), same encoding as camLedStatus.
    Unlike the camera LED, the firmware does NOT auto-manage the vehicle LEDs on
    dock/charge — they hold whatever they were last set to, so an HA automation is
    the intended way to turn them off while docked.
    """
    msg = pb.PbInput()
    msg.version = PB_VERSION_4_9
    msg.robotConfig.signal = (
        SIGNAL_TURN_ON_VEHICLE_LIGHT if enabled else SIGNAL_TURN_OFF_VEHICLE_LIGHT
    )
    return msg.SerializeToString()


def build_initial_query_packets(
    query_index: int = 0,
    client_uuid: str | None = None,
) -> list[bytes]:
    """Startup packet set. Kept compatible with old coordinator imports."""
    packets: list[bytes] = []
    if client_uuid:
        packets.append(encode_app_connect(client_uuid))
    packets.extend([
        encode_query_map(query_index),
        encode_query_schedules(),
        encode_upload_robot_config(),
        # Ask for live/status data immediately, not only after 90s
        encode_query_cleaning_info(),
        encode_query_net_detail(),
        encode_query_robot_config(),
        encode_query_wifi_4g(),
        encode_query_cleaning_summary(),
    ])
    return packets


def build_refresh_query_packets(client_uuid: str | None = None) -> list[bytes]:
    """Light periodic refresh packet set."""
    packets: list[bytes] = []
    if client_uuid:
        packets.append(encode_app_connect(client_uuid))
    packets.extend([
        encode_query_cleaning_info(),
        encode_query_net_detail(),
        encode_query_robot_config(),
        encode_query_wifi_4g(),
        encode_query_rtk_l1(),
        encode_query_rtk_l2(),
        encode_query_cleaning_summary(),
    ])
    return packets


# ---------------------------------------------------------------------------
# PbOutput decode — protobuf first
# ---------------------------------------------------------------------------

def decode_pboutput(raw: bytes) -> pb.PbOutput:
    """Parse raw protobuf bytes as PbOutput."""
    msg = pb.PbOutput()
    msg.ParseFromString(raw)
    return msg


def decode_pboutput_envelope(envelope_bytes: bytes) -> pb.PbOutput | None:
    """Decode JSON-enveloped/raw payload into PbOutput."""
    raw = unwrap_envelope(envelope_bytes)
    if not raw:
        return None
    return decode_pboutput(raw)


# ---------------------------------------------------------------------------
# Low-level wire parser for opaque btMap/queryAck blobs
# ---------------------------------------------------------------------------

def _dec_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    for _ in range(10):
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise ValueError("varint overflow")


def _wire_parse(buf: bytes) -> dict[int, list[tuple[str, Any]]]:
    if not isinstance(buf, (bytes, bytearray)) or not buf:
        return {}
    out: dict[int, list[tuple[str, Any]]] = {}
    pos = 0
    while pos < len(buf):
        try:
            tag, pos = _dec_varint(buf, pos)
            fno, wt = tag >> 3, tag & 7
            if wt == 0:
                v, pos = _dec_varint(buf, pos)
                out.setdefault(fno, []).append(("v", v))
            elif wt == 1:
                out.setdefault(fno, []).append(("f64", buf[pos:pos + 8]))
                pos += 8
            elif wt == 2:
                ln, pos = _dec_varint(buf, pos)
                out.setdefault(fno, []).append(("L", buf[pos:pos + ln]))
                pos += ln
            elif wt == 5:
                out.setdefault(fno, []).append(("f32", buf[pos:pos + 4]))
                pos += 4
            else:
                break
        except Exception:
            break
    return out

def _is_path_marker(pt: tuple[float, float]) -> bool:
    x, y = pt
    return (
        (abs(x - 333.0) < 0.001 and abs(y - 333.0) < 0.001)
        or (abs(x - 444.0) < 0.001 and abs(y - 444.0) < 0.001)
    )


def _split_path_segments(points: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    for pt in points:
        if _is_path_marker(pt):
            if len(current) >= 2:
                segments.append(current)
            current = []
            continue

        current.append(pt)

    if len(current) >= 2:
        segments.append(current)

    return segments


def _marker_kind(pt: tuple[float, float]) -> int | None:
    """Return 333 or 444 if pt is that marker, else None."""
    x, y = pt
    if abs(x - 333.0) < 0.001 and abs(y - 333.0) < 0.001:
        return 333
    if abs(x - 444.0) < 0.001 and abs(y - 444.0) < 0.001:
        return 444
    return None


def _split_path_segments_typed(
    points: list[tuple[float, float]],
) -> list[dict[str, Any]]:
    """Like _split_path_segments, but tags each segment with the marker kind
    (333 / 444) immediately before and after it. The legacy splitter collapses
    both markers into a generic separator and discards which one it was, forcing
    a fragile point-count heuristic downstream. Capturing the marker context lets
    cut(333)/planned(444) classification use the real markers instead of size.
    """
    out: list[dict[str, Any]] = []
    current: list[tuple[float, float]] = []
    last_marker: int | None = None

    for pt in points:
        m = _marker_kind(pt)
        if m is not None:
            if len(current) >= 2:
                out.append({"points": current, "marker_before": last_marker, "marker_after": m})
            current = []
            last_marker = m
            continue
        current.append(pt)

    if len(current) >= 2:
        out.append({"points": current, "marker_before": last_marker, "marker_after": None})

    return out


def _path_length(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(points)):
        x1, y1 = points[i - 1]
        x2, y2 = points[i]
        total += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return round(total, 2)


def _bounds(points: list[tuple[float, float]]) -> dict[str, float] | None:
    if not points:
        return None
    return {
        "min_x": round(min(x for x, y in points), 4),
        "max_x": round(max(x for x, y in points), 4),
        "min_y": round(min(y for x, y in points), 4),
        "max_y": round(max(y for x, y in points), 4),
    }


def parse_query_path(bt_map_msg) -> dict:
    """Parse QUERY_PATH response from PbOutput.btMap.

    Returns planned/cut path points as ENU metres.
    Marker points (333,333) and (444,444) are treated as segment separators.
    """
    raw = bt_map_msg.SerializeToString()
    root = _wire_parse(raw)

    result: dict[str, Any] = {
        "btMap_bytes": len(raw),
        "queryAck_found": False,
        "inner_found": False,
        "inner_bytes": 0,
        "inner_field_numbers": [],
        "raw_points_count": 0,
        "marker_count": 0,
        "points_count": 0,
        "segment_count": 0,
        "path_length_m": 0,
        "bounds": None,
        "points": [],
        "segments": [],
        "segment_markers": [],
    }

    try:
        if 2 not in root or not root[2] or root[2][0][0] != "L":
            return result

        qa = _wire_parse(root[2][0][1])
        result["queryAck_found"] = True

        inner_raw = None

        if 3 in qa and qa[3] and qa[3][0][0] == "L":
            inner_raw = qa[3][0][1]
        else:
            for _fno, entries in qa.items():
                for kind, val in entries:
                    if kind == "L" and isinstance(val, (bytes, bytearray)) and len(val) > 20:
                        inner_raw = val
                        break
                if inner_raw is not None:
                    break

        if not inner_raw:
            return result

        inner = _wire_parse(inner_raw)
        result["inner_found"] = True
        result["inner_bytes"] = len(inner_raw)
        result["inner_field_numbers"] = sorted(inner.keys())

        raw_points: list[tuple[float, float]] = []

        # Nel tuo debug: inner_field_numbers = [1], quindi field 1 = repeated PbPoint
        for kind, val in inner.get(1, []):
            if kind != "L":
                continue

            pf = _wire_parse(val)
            x = _gf(pf, 1)
            y = _gf(pf, 2)

            if x is not None and y is not None:
                raw_points.append((round(float(x), 4), round(float(y), 4)))

        segments = _split_path_segments(raw_points)
        clean_points = [pt for seg in segments for pt in seg]

        result["raw_points_count"] = len(raw_points)
        result["marker_count"] = sum(1 for pt in raw_points if _is_path_marker(pt))
        result["points_count"] = len(clean_points)
        result["segment_count"] = len(segments)
        result["points"] = clean_points
        result["segments"] = segments
        # Diagnostic: per-segment marker context (size + bounding 333/444 markers),
        # to learn the real cut/planned semantics and drive a marker-based classifier.
        result["segment_markers"] = [
            {"n": len(s["points"]), "before": s["marker_before"], "after": s["marker_after"]}
            for s in _split_path_segments_typed(raw_points)
        ]
        result["bounds"] = _bounds(clean_points)
        result["path_length_m"] = round(sum(_path_length(seg) for seg in segments), 2)

        return result

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

def parse_schedules(schedule_msg: Any) -> list[ScheduleInfo]:
    """Decode PbSchedules into ScheduleInfo list.

    Works even if generated PbSchedule is incomplete/empty by walking raw wire format.
    """
    raw = schedule_msg.SerializeToString()
    root = _wire_parse(raw)

    schedules: list[ScheduleInfo] = []

    # PbSchedules.tasks = field 1 repeated PbSchedule
    for kind, task_raw in root.get(1, []):
        if kind != "L":
            continue

        try:
            f = _wire_parse(task_raw)

            days_of_week: list[int] = []

            # field 1 dayOfWeek packed repeated enum
            for day_kind, day_val in f.get(1, []):
                if day_kind == "L":
                    days_of_week.extend(_decode_packed_varints(day_val))
                elif day_kind == "v":
                    days_of_week.append(int(day_val))

            day_names = [
                _DAYS_NAMES[d]
                for d in days_of_week
                if 0 <= d < len(_DAYS_NAMES)
            ]

            hour = int(f[2][0][1]) if 2 in f and f[2][0][0] == "v" else 0
            minute = int(f[3][0][1]) if 3 in f and f[3][0][0] == "v" else 0

            is_repeated = bool(f[4][0][1]) if 4 in f and f[4][0][0] == "v" else False
            schedule_id = int(f[6][0][1]) if 6 in f and f[6][0][0] == "v" else 0

            timezone = 0
            if 7 in f and f[7][0][0] == "v":
                timezone = _as_signed_64(int(f[7][0][1]))

            is_disabled = bool(f[8][0][1]) if 8 in f and f[8][0][0] == "v" else False
            is_angle_offset = bool(f[9][0][1]) if 9 in f and f[9][0][0] == "v" else False
            mow_angle = int(f[10][0][1]) if 10 in f and f[10][0][0] == "v" else 0

            zones: list[ScheduleZoneInfo] = []
            for zone_kind, zone_raw in f.get(5, []):
                if zone_kind != "L":
                    continue
                zone = _decode_schedule_zone_basicinfo(zone_raw)
                if zone is not None:
                    zones.append(zone)

            # Keep zone order stable
            zones.sort(key=lambda z: z.mow_order or 999)

            config: list[ScheduleConfigInfo] = []
            for cfg_kind, cfg_raw in f.get(11, []):
                if cfg_kind != "L":
                    continue
                cfg = _decode_schedule_config(cfg_raw)
                if cfg is not None:
                    config.append(cfg)

            schedules.append(
                ScheduleInfo(
                    id=schedule_id,
                    hour=hour,
                    minute=minute,
                    days_of_week=days_of_week,
                    day_names=day_names,
                    timezone=timezone,
                    is_repeated=is_repeated,
                    is_disabled=is_disabled,
                    is_angle_offset=is_angle_offset,
                    mow_angle=mow_angle,
                    zones=zones,
                    config=config,
                )
            )

        except Exception:
            _LOGGER.debug("Failed to decode Lymow schedule task", exc_info=True)

    return schedules

def _decode_packed_varints(buf: bytes) -> list[int]:
    out: list[int] = []
    pos = 0

    while pos < len(buf):
        try:
            value, pos = _wire_varint(buf, pos)
            out.append(value)
        except Exception:
            break

    return out

def _decode_schedule_zone_basicinfo(buf: bytes) -> ScheduleZoneInfo | None:
    """Decode PbZoneBasicInfo-like item inside PbSchedule field 5."""
    f = _wire_parse(buf)

    hash_id = ""
    name: str | None = None
    mow_order = 0
    text_pos: dict[str, float] | None = None

    if 2 in f and f[2][0][0] == "L":
        name = _wire_str(f[2][0][1]) or None

    if 3 in f and f[3][0][0] == "L":
        hash_id = _wire_str(f[3][0][1]) or ""

    if 6 in f and f[6][0][0] == "L":
        # zoneRename fallback
        rename = _wire_str(f[6][0][1])
        if rename:
            name = rename

    if 8 in f and f[8][0][0] == "v":
        mow_order = int(f[8][0][1])

    if 9 in f and f[9][0][0] == "L":
        pt = _parse_point(f[9][0][1])
        if pt is not None:
            text_pos = {"x": pt[0], "y": pt[1]}

    if not hash_id:
        return None

    return ScheduleZoneInfo(
        hash_id=hash_id,
        name=name,
        mow_order=mow_order,
        text_pos=text_pos,
    )


def _decode_schedule_config(buf: bytes) -> ScheduleConfigInfo | None:
    """Decode PbScheduleConfig.

    Schema:
      1 hashId
      2 cutHeight
      3 moveSpeed
      4 cleanDir
    """
    f = _wire_parse(buf)

    hash_id = ""
    cut_height: int | None = None
    move_speed: float | None = None
    clean_dir: int | None = None

    if 1 in f and f[1][0][0] == "L":
        hash_id = _wire_str(f[1][0][1]) or ""

    if 2 in f and f[2][0][0] == "v":
        cut_height = int(f[2][0][1])

    if 3 in f and f[3][0][0] == "f32":
        move_speed = _wire_f32(f[3][0][1])

    if 4 in f and f[4][0][0] == "v":
        clean_dir = _as_signed_64(int(f[4][0][1]))

    if not hash_id:
        return None

    return ScheduleConfigInfo(
        hash_id=hash_id,
        cut_height=cut_height,
        move_speed=move_speed,
        clean_dir=clean_dir,
    )

def _gv(f: dict, n: int) -> int | None:
    e = f.get(n)
    return e[0][1] if e and e[0][0] == "v" else None


def _gs(f: dict, n: int) -> str | None:
    e = f.get(n)
    if e and e[0][0] == "L":
        try:
            return e[0][1].decode("utf-8")
        except Exception:
            return None
    return None


def _gf(f: dict, n: int) -> float | None:
    e = f.get(n)
    if e and e[0][0] == "f32" and len(e[0][1]) == 4:
        return round(struct.unpack("<f", e[0][1])[0], 4)
    return None


def _sub(f: dict, n: int) -> dict:
    e = f.get(n)
    return _wire_parse(e[0][1]) if e and e[0][0] == "L" else {}


def _s32(v: int) -> int:
    return v - (1 << 64) if v >= (1 << 63) else v


def _wire_str(blob: bytes) -> str | None:
    try:
        s = blob.decode("utf-8")
        if s.isprintable():
            return s
    except Exception:
        pass
    return None

def _wire_f32(blob: Any) -> float | None:
    """Decode protobuf fixed32/float little-endian."""
    if not isinstance(blob, (bytes, bytearray)) or len(blob) != 4:
        return None

    try:
        return float(struct.unpack("<f", bytes(blob))[0])
    except Exception:
        return None
    
def _wire_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode a protobuf varint from buf starting at pos.

    Returns:
        (value, new_pos)
    """
    result = 0
    shift = 0

    for _ in range(10):  # protobuf varint max 10 bytes for 64-bit
        if pos >= len(buf):
            raise ValueError("truncated varint")

        b = buf[pos]
        pos += 1

        result |= (b & 0x7F) << shift

        if not (b & 0x80):
            return result, pos

        shift += 7

    raise ValueError("varint overflow")


def _as_signed_64(value: int) -> int:
    """Convert unsigned varint value to signed int64 when needed."""
    if value > 0x7FFFFFFFFFFFFFFF:
        value -= 1 << 64
    return value


def _parse_point(buf: bytes) -> tuple[float, float] | None:
    """Decode PbPoint { x = field 1 float, y = field 2 float }."""
    fields = _wire_parse(buf)

    if 1 not in fields or 2 not in fields:
        return None

    x_kind, x_raw = fields[1][0]
    y_kind, y_raw = fields[2][0]

    if x_kind != "f32" or y_kind != "f32":
        return None

    x = _wire_f32(x_raw)
    y = _wire_f32(y_raw)

    if x is None or y is None:
        return None

    return (round(x, 4), round(y, 4))


def _decode_point_dict(fields: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    x = _gf(fields, 1)
    y = _gf(fields, 2)
    if x is not None:
        out["x"] = x
    if y is not None:
        out["y"] = y
    return out


def _decode_pose_dict(fields: dict) -> dict[str, Any]:
    out = _decode_point_dict(fields)
    theta = _gf(fields, 3)
    z = _gf(fields, 4)
    if theta is not None:
        out["theta"] = theta
        out["heading"] = theta
    if z is not None:
        out["z"] = z
    return out


def _decode_lla_dict(fields: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lat = _gf(fields, 1)
    lon = _gf(fields, 2)
    alt = _gf(fields, 3)
    if lat is not None:
        out["latitude"] = lat
    if lon is not None:
        out["longitude"] = lon
    if alt is not None:
        out["altitude"] = alt
    return out


def _decode_polygon_points(fields: dict) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for kind, val in fields.get(1, []):
        if kind != "L":
            continue
        pf = _wire_parse(val)
        x = _gf(pf, 1)
        y = _gf(pf, 2)
        if x is not None and y is not None:
            pts.append((round(x, 4), round(y, 4)))
    return pts


def _polygon_area(pts: list[tuple[float, float]]) -> float:
    if len(pts) < 3:
        return 0.0
    total = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        total += x1 * y2 - x2 * y1
    return abs(total) * 0.5


def _decode_zone_config_fields(fields: dict) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for fno, key in [
        (1, "cutHeight"), (5, "brushSpeed"), (6, "cutSpeed"), (7, "cleanMode"),
        (8, "cleanDir"), (9, "pathSpacing"), (10, "perimeterMowLaps"),
        (11, "perimeterMowDir"), (12, "noGoMowLaps"), (13, "obsDecMode"),
        (15, "startProgress"), (16, "relativeCleanDir"), (19, "followDetectMode"),
    ]:
        v = _gv(fields, fno)
        if v is not None:
            cfg[key] = _s32(v) if key in {"cleanDir"} else v
    for fno, key in [
        (2, "raiseCutHeight"), (3, "lowerCutHeight"), (14, "pathOrder"),
        (17, "lineFollowMode"), (18, "disableOuterDischarge"),
    ]:
        v = _gv(fields, fno)
        if v is not None:
            cfg[key] = bool(v)
    f = _gf(fields, 4)
    if f is not None:
        cfg["moveSpeed"] = f
    return cfg


def _parse_pbzone_basicinfo(buf: bytes) -> dict[str, Any]:
    f = _wire_parse(buf)
    out: dict[str, Any] = {
        "type": _gv(f, 1),
        "name": _gs(f, 2) or "",
        "hashId": _gs(f, 3) or "",
        "isEnabled": bool(_gv(f, 4)) if _gv(f, 4) is not None else True,
        "zoneRename": _gs(f, 6) or "",
        "updateTime": _gv(f, 7),
        "mowOrder": _gv(f, 8) or 0,
        "polygon": [],
        "textPos": None,
    }
    poly = _sub(f, 5)
    if poly:
        out["polygon"] = _decode_polygon_points(poly)
    text_pos = _sub(f, 9)
    if text_pos:
        p = _decode_point_dict(text_pos)
        if "x" in p and "y" in p:
            out["textPos"] = (p["x"], p["y"])
    return out


def _rectangle_from_bounds(b00: Any, b11: Any) -> list[tuple[float, float]]:
    try:
        x1, y1 = float(b00[0]), float(b00[1])
        x2, y2 = float(b11[0]), float(b11[1])
    except Exception:
        return []
    if x1 == x2 or y1 == y2:
        return []
    return [
        (round(x1, 4), round(y1, 4)),
        (round(x2, 4), round(y1, 4)),
        (round(x2, 4), round(y2, 4)),
        (round(x1, 4), round(y2, 4)),
    ]


def _decode_pp_basic_info(fields: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    b00 = _sub(fields, 1)
    if b00:
        p = _decode_point_dict(b00)
        if "x" in p and "y" in p:
            out["bound_00"] = (p["x"], p["y"])
    b11 = _sub(fields, 2)
    if b11:
        p = _decode_point_dict(b11)
        if "x" in p and "y" in p:
            out["bound_11"] = (p["x"], p["y"])
    area = _gv(fields, 3)
    if area is not None:
        out["ppArea"] = area
    cw = _gv(fields, 4)
    if cw is not None:
        out["isClockwise"] = bool(cw)
    inner = _sub(fields, 5)
    if inner:
        p = _decode_point_dict(inner)
        if "x" in p and "y" in p:
            out["innerPoint"] = (p["x"], p["y"])
    return out


# ---------------------------------------------------------------------------
# PbMap/PbBtMap catalog parsers
# ---------------------------------------------------------------------------

def parse_map_fields(map_data: dict[int, list[tuple[str, Any]]]) -> ZoneCatalog:
    """Parse a PbMap wire dict into ZoneCatalog."""
    catalog = ZoneCatalog()

    enu = _sub(map_data, 7)
    if enu:
        catalog.enu_base_point = _decode_lla_dict(enu) or None

    dock = _sub(map_data, 4)
    if dock:
        catalog.charging_station_loc = _decode_pose_dict(dock) or None

    rtc = _sub(map_data, 13)
    if rtc:
        runtime: dict[str, Any] = {}
        v = _gv(rtc, 1)
        if v is not None:
            runtime["cutHeight"] = v
        f = _gf(rtc, 4)
        if f is not None:
            runtime["moveSpeed"] = f
        v = _gv(rtc, 6)
        if v is not None:
            runtime["cutSpeed"] = v
        if runtime:
            catalog.runtime_config = runtime

    # PbMap.goZones (field 1)
    hash_re = re.compile(r"^[A-Za-z0-9_]{4,32}$")
    for kind, zval in map_data.get(1, []):
        if kind != "L":
            continue
        z = _wire_parse(zval)
        basic = _sub(z, 1)
        if not basic:
            continue
        bi = _parse_pbzone_basicinfo(z[1][0][1])
        hash_id = bi.get("hashId") or ""
        if not hash_id or not hash_re.match(hash_id):
            continue
        points = list(bi.get("polygon") or [])

        # Fallback from ppBasicInfo bounds if no polygon points exist.
        pp = _sub(z, 3)
        if not points and pp:
            pp_info = _decode_pp_basic_info(pp)
            if pp_info.get("bound_00") and pp_info.get("bound_11"):
                points = _rectangle_from_bounds(pp_info["bound_00"], pp_info["bound_11"])

        zone_cfg: dict[str, Any] = {}
        zcfg = _sub(z, 2)
        if zcfg:
            zone_cfg = _decode_zone_config_fields(zcfg)

        name = bi.get("name") or bi.get("zoneRename") or hash_id
        text_pos = bi.get("textPos")
        zi = ZoneInfo(
            hash_id=hash_id,
            name=name,
            mow_order=int(bi.get("mowOrder") or 0),
            is_enabled=bool(bi.get("isEnabled")),
            polygon_points=points,
            zone_config=zone_cfg,
            text_pos=text_pos if isinstance(text_pos, tuple) else None,
            zone_type=bi.get("type"),
            area=round(_polygon_area(points), 3) if points else None,
        )
        catalog.zones.append(zi)
        catalog.zones_by_hashid[zi.hash_id] = zi
    
    # PbMap.nogozone (field 2)
    for kind, raw_nogo in map_data.get(2, []):
        if kind != "L":
            continue

        try:
            nogo = _parse_nogo_zone(raw_nogo)
            if nogo is not None:
                catalog.nogo_zones.append(nogo)
        except Exception:
            _LOGGER.debug("Failed to parse no-go zone", exc_info=True)

    # PbMap.channels (field 3)
    for kind, cval in map_data.get(3, []):
        if kind != "L":
            continue
        chf = _wire_parse(cval)
        hash_id = _gs(chf, 1) or ""
        if not hash_id:
            continue
        poly = _sub(chf, 5)
        pts = _decode_polygon_points(poly) if poly else []
        is_valid_raw = _gv(chf, 4)
        catalog.channels.append(ChannelInfo(
            hash_id=hash_id,
            zone1=_gs(chf, 2) or "",
            zone2=_gs(chf, 3) or "",
            is_valid=bool(is_valid_raw) if is_valid_raw is not None else None,
            is_docking_channel=bool(_gv(chf, 6)) if _gv(chf, 6) is not None else False,
            detect_mode=_gv(chf, 8),
            cut_height=_gv(chf, 9),
            channel_lift=bool(_gv(chf, 10)) if _gv(chf, 10) is not None else None,
            polygon_points=pts,
        ))

    return catalog


def parse_zone_catalog(bt_map: pb.PbBtMap) -> ZoneCatalog:
    """Parse PbBtMap QUERY_MAP response into ZoneCatalog.

    The rich map is usually hidden inside:
      PbBtMap.queryAck -> queryAck field 3 -> PbMap blob
    """
    if bt_map is None or bt_map.ByteSize() == 0:
        return ZoneCatalog()

    root = _wire_parse(bt_map.SerializeToString())

    # Path used by real QUERY_MAP response: btMap field 2 = queryAck.
    try:
        if 2 in root and root[2][0][0] == "L":
            qa = _wire_parse(root[2][0][1])
            if 3 in qa and qa[3][0][0] == "L":
                inner = _wire_parse(qa[3][0][1])
                return parse_map_fields(inner)
    except Exception:
        _LOGGER.debug("Failed parsing btMap.queryAck map blob", exc_info=True)

    # Fallback: sometimes the bytes may already look like PbMap-ish fields.
    return parse_map_fields(root)

def _parse_nogo_zone(raw: bytes) -> NoGoZoneInfo | None:
    """Parse one no-go zone / excluded area from QUERY_MAP inner.field_2."""
    msg = _wire_parse(raw)

    hash_id = ""
    name = ""
    is_enabled = True
    zone_type: int | None = None
    points: list[tuple[float, float]] = []
    points_source: str | None = None
    area: float | None = None
    bound_00: tuple[float, float] | None = None
    bound_11: tuple[float, float] | None = None
    inner_point: tuple[float, float] | None = None

    # field 1 = basicInfo-like
    basic = _sub(msg, 1)
    if basic:
        if (v := _gv(basic, 1)) is not None:
            zone_type = int(v)

        if (hid := _gs(basic, 3)):
            hash_id = hid

        if (v := _gv(basic, 4)) is not None:
            is_enabled = bool(v)

        poly = _sub(basic, 5)
        if poly:
            pts = _decode_polygon_points(poly)
            if pts:
                points = pts
                points_source = "basicInfo.polygon"
                area = round(_polygon_area(pts), 3)

    # field 3 = ppBasicInfo-like, fallback bounds/inner point
    pp = _sub(msg, 3)
    if pp:
        pp_info = _decode_pp_basic_info(pp)

        if isinstance(pp_info.get("bound_00"), tuple):
            bound_00 = pp_info["bound_00"]

        if isinstance(pp_info.get("bound_11"), tuple):
            bound_11 = pp_info["bound_11"]

        if isinstance(pp_info.get("innerPoint"), tuple):
            inner_point = pp_info["innerPoint"]

        if not points and bound_00 and bound_11:
            rect = _rectangle_from_bounds(bound_00, bound_11)
            if rect:
                points = rect
                points_source = "ppBasicInfo.bounds_fallback"
                area = round(_polygon_area(rect), 3)

    # field 4 = linked go-zone hash ids
    linked_zone_hash_ids: list[str] = []
    for kind, value in msg.get(4, []):
        if kind != "L":
            continue
        try:
            linked_zone_hash_ids.append(value.decode("utf-8"))
        except Exception:
            pass

    if not hash_id and not points:
        return None

    if not hash_id:
        hash_id = f"nogo_{abs(hash(tuple(points))) % 1000000}"

    name = hash_id

    return NoGoZoneInfo(
        hash_id=hash_id,
        name=name,
        is_enabled=is_enabled,
        polygon_points=points,
        linked_zone_hash_ids=linked_zone_hash_ids,
        zone_type=zone_type,
        area=area,
        points_source=points_source,
        bound_00=bound_00,
        bound_11=bound_11,
        inner_point=inner_point,
    )


# ---------------------------------------------------------------------------
# Start schedule encoder
# ---------------------------------------------------------------------------

def encode_start_schedule_task_full(task: ScheduleInfo | dict[str, Any]) -> bytes:
    """Start a schedule manually with zone order and per-zone config."""
    pb_in = pb.PbInput()
    pb_in.version = PB_VERSION_4_9
    pb_in.userCtrl = USER_CTRL_CLEAN

    if isinstance(task, dict):
        zones = task.get("zones") or []
        configs = task.get("config") or []
    else:
        zones = [z.to_dict() for z in task.zones]
        configs = [c.to_dict() for c in task.config]

    config_by_hash = {
        c.get("hashId"): c
        for c in configs
        if c.get("hashId")
    }

    # fallback se non ci sono zones dettagliate
    if not zones and isinstance(task, dict):
        zones = [
            {"hashId": hid, "mowOrder": i}
            for i, hid in enumerate(task.get("zoneHashIds") or [], start=1)
        ]

    for i, zone_data in enumerate(zones, start=1):
        hash_id = zone_data.get("hashId")
        if not hash_id:
            continue

        z = pb_in.map.goZones.add()
        z.basicInfo.hashId = hash_id
        z.basicInfo.mowOrder = int(zone_data.get("mowOrder") or i)

        cfg = config_by_hash.get(hash_id)
        if cfg:
            if cfg.get("cutHeight") is not None:
                z.zoneConfig.cutHeight = int(cfg["cutHeight"])
            if cfg.get("moveSpeed") is not None:
                z.zoneConfig.moveSpeed = float(cfg["moveSpeed"])
            if cfg.get("cleanDir") is not None:
                z.zoneConfig.cleanDir = int(cfg["cleanDir"])

    return pb_in.SerializeToString()
