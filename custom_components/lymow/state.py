"""Lymow state helpers.

This module is the bridge between:
- protobuf messages received from MQTT (`lymow_pb2.PbOutput`);
- dataclasses produced by protocol.parse_zone_catalog();
- the flat compatibility dict used by existing HA entities.

The coordinator owns the dict. These helpers only mutate/derive it.
"""
from __future__ import annotations

import logging
from math import cos, hypot, radians
from typing import Any

from .const import DEFAULT_CHANNEL_BUFFER_M
from .map_tuning import CHANNEL_RIBBON_HALFWIDTH_M

_LOGGER = logging.getLogger(__name__)

try:
    from .proto import lymow_pb2 as pb
except Exception:  # pragma: no cover - allows standalone linting
    pb = None  # type: ignore


_ACTIVE_TASK_WORK_STATUSES = {2, 8, 9, 14}  # mowing, resume, zone partition, escaping
# Hold a zone while the mower is within this distance of its edge — on a perimeter
# lap the mower rides the boundary and its GPS sits right on/just outside the
# polygon, which would otherwise drop current_zone out to unknown. ~1 ft.
ZONE_EDGE_BUFFER_M = 0.3
# Statuses where the mower is out navigating and we should resolve zone/channel.
# Adds Docking (4) so it tracks on the way HOME too — it crosses the same
# corridors returning, which matters for transit automations (e.g. a gate).
_LOCALIZE_STATUSES = _ACTIVE_TASK_WORK_STATUSES | {4}


def _has_msg(msg: Any) -> bool:
    return msg is not None and hasattr(msg, "ByteSize") and msg.ByteSize() > 0


def _has_field(msg: Any, field_name: str) -> bool:
    if msg is None:
        return False
    try:
        return msg.HasField(field_name)
    except Exception:
        # Proto3 scalar fields often have no presence. If ListFields includes it,
        # it is definitely present in this packet.
        try:
            return any(f.name == field_name for f, _ in msg.ListFields())
        except Exception:
            return False


def _msg_to_point_dict(msg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("x", "y", "z", "theta"):
        if hasattr(msg, key):
            try:
                out[key] = float(getattr(msg, key))
            except Exception:
                pass
    if "theta" in out:
        out["heading"] = out["theta"]
    return out


def _msg_to_lla_dict(msg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("latitude", "longitude", "altitude"):
        if hasattr(msg, key):
            try:
                out[key] = float(getattr(msg, key))
            except Exception:
                pass
    return out


def _msg_to_tz_dict(msg: Any) -> dict[str, int]:
    return {
        "hour": int(getattr(msg, "hour", 0) or 0),
        "minute": int(getattr(msg, "minute", 0) or 0),
    }


def merge_pboutput(state: dict[str, Any], msg: Any) -> dict[str, Any]:
    """Merge one PbOutput into the flat coordinator state.

    PbOutput messages are partial. This function updates only the fields that
    are present in the current packet and intentionally preserves sticky fields
    such as zone_catalog / btMap / enu_base_point.
    """
    if msg is None:
        return state

    # Keep original protobuf submessages for advanced consumers.
    for field, value in msg.ListFields():
        if field.name == "btMap":
            continue

        state[field.name] = value

        # Diagnostic: mobilePushNotification (field 25) is the only push-ish field
        # in the proto, but it never appeared during a live rain-dock test. Log it
        # at INFO whenever it's actually non-zero so we can learn — from normal use —
        # whether the firmware ever populates it on the telemetry topic (and capture
        # the code values). If it stays silent over time, the field is dead on our
        # channel and the push entity should be removed.
        if field.name == "mobilePushNotification" and value:
            _LOGGER.info("Lymow mobilePushNotification fired: code=%s", value)

    if getattr(msg, "msgId", None):
        state["msgId"] = msg.msgId
    if getattr(msg, "version", None):
        state["version"] = msg.version

    # Repeated scalar fields. errorCodes/warningCodes are repeated, so proto3
    # omits them when empty — without an explicit clear they'd stay stuck at the
    # last error after it resolved. robotInfo is a full status snapshot, so clear
    # them when a robotInfo frame arrives carrying none (except in the Error (7)
    # / Emergency-Stop (13) states, where the active error is legitimate).
    ri = getattr(msg, "robotInfo", None)
    _ri_present = _has_msg(ri)
    _ws = getattr(ri, "workStatus", None) if _ri_present else None
    if len(getattr(msg, "errorCodes", [])):
        state["errorCodes"] = list(msg.errorCodes)
        state["errorCode"] = msg.errorCodes[0]
    elif _ri_present and _ws not in (7, 13):
        state["errorCodes"] = []
        state["errorCode"] = 0
    if len(getattr(msg, "warningCodes", [])):
        state["warningCodes"] = list(msg.warningCodes)
    elif _ri_present:
        state["warningCodes"] = []

    if _has_msg(ri):
        state["robotInfo"] = ri
        for src, dst in [
            ("robotStatus", "robotStatus"),
            ("battery", "battery"),
            ("wifiSignalQuality", "wifiSignalQuality"),
            ("lteSignalQuality", "lteSignalQuality"),
            ("btSignalQuality", "btSignalQuality"),
            ("workStatus", "workStatus"),
        ]:
            if _has_field(ri, src):
                state[dst] = getattr(ri, src)
        # proto3 bools are omitted from the wire when false, so the _has_field
        # gate above would leave them stuck at their last true value (e.g.
        # Charging never turning off after the mower leaves the dock). robotInfo
        # is a full status snapshot, so read these directly — false then applies.
        for b in ("isRecharging", "isCharging", "wifiWorking", "lteWorking"):
            state[b] = bool(getattr(ri, b, False))
        if "workStatus" in state:
            state["isOnline"] = True

    li = getattr(msg, "localizationInfo", None)
    if _has_msg(li):
        state["localizationInfo"] = li
        for src, dst in [
            # numSatellites intentionally NOT mapped: flat constant (~10), not a real
            # satellite count — superseded by rtkSatellites (satelliteCount) below.
            ("horizontalAccuracy", "gnssHorizontalAccuracy"),
            ("verticalAccuracy", "gnssVerticalAccuracy"),
            ("positionQuality", "gnssPositionQuality"),
            ("locNodeStatus", "gnssLocNodeStatus"),
        ]:
            if _has_field(li, src):
                state[dst] = getattr(li, src)

    bo = getattr(msg, "baseOutput", None)
    if _has_msg(bo):
        state["baseOutput"] = bo
        if _has_field(bo, "cutHeight"):
            state["cutHeight"] = bo.cutHeight
        twist = getattr(bo, "twist", None)
        if _has_msg(twist):
            if _has_field(twist, "linear"):
                state["twistLinear"] = twist.linear
            if _has_field(twist, "angular"):
                state["twistAngular"] = twist.angular

    # Firmware OTA progress — PbDebugSetting.downloadProgress (field 7) is the
    # exact value the official app renders as the live update percentage during
    # an OTA. (uploadProgress is unrelated: a map backup/restore enum.)
    ds = getattr(msg, "debugSetting", None)
    if _has_msg(ds) and _has_field(ds, "downloadProgress"):
        try:
            state["downloadProgress"] = int(getattr(ds, "downloadProgress", 0) or 0)
        except Exception:
            pass

    dp = getattr(msg, "deviceInfo", None)
    if _has_msg(dp):
        state["deviceInfo"] = dp
        for src, dst in [
            ("fwVersion", "fwVersion"),
            ("mcuVersion", "appFwVersion"),
            ("softwareVersion", "mcuVersion"),
            ("softwareVersion", "softwareVersion"),
            ("wifiSsid", "wifiSsid"),
            ("ipAddress", "ipAddress"),
            ("macAddress", "macAddress"),
            ("sn", "sn"),
            ("rtkSn", "rtkSn"),
            ("simId", "simId"),
            ("wheelVer", "wheelVer"),
            ("knifeVer", "knifeVer"),
        ]:
            if _has_field(dp, src):
                val = getattr(dp, src)
                state[dst] = val.strip() if isinstance(val, str) else val

    ci = getattr(msg, "cleanInfo", None)
    if _has_msg(ci):
        state["cleanInfo"] = ci
        for src, dst in [
            ("cleanTime", "cleanTime"),
            ("cleanArea", "cleanArea"),
            ("remainCleanTime", "remainCleanTime"),
            ("cleanPercent", "cleanPercent"),
            ("mapArea", "mapArea"),
        ]:
            if _has_field(ci, src):
                state[dst] = getattr(ci, src)
        if _has_msg(getattr(ci, "areaInfo", None)):
            area = ci.areaInfo
            if len(getattr(area, "cleanZoneIds", [])):
                state["cleanZoneIds"] = list(area.cleanZoneIds)
            if _has_field(area, "areaOrGlobal"):
                state["areaOrGlobal"] = area.areaOrGlobal

    pose = getattr(msg, "pose", None)
    if _has_msg(pose):
        state["poseMessage"] = pose
        pose_dict = _msg_to_point_dict(pose)
        if pose_dict:
            state["pose"] = pose_dict
            if "theta" in pose_dict:
                from math import degrees
                state["mowerHeading"] = round((90 - degrees(pose_dict["theta"])) % 360, 1)

    lla = getattr(msg, "robotLlaCoords", None)
    if _has_msg(lla):
        state["robotLlaCoordsMessage"] = lla
        lla_dict = _msg_to_lla_dict(lla)
        if lla_dict:
            state["robotLlaCoords"] = lla_dict
            state["latitude"] = lla_dict.get("latitude")
            state["longitude"] = lla_dict.get("longitude")

    dock = getattr(msg, "chargingStationLoc", None)
    if _has_msg(dock):
        dock_dict = _msg_to_point_dict(dock)
        if dock_dict:
            state["chargingStationLoc"] = dock_dict

    rc = getattr(msg, "robotConfig", None)
    if _has_msg(rc):
        state["robotConfig"] = rc
        for src, dst in [
            ("rcCutSpeed", "rcCutSpeed"),
            ("rcCutHeight", "rcCutHeight"),
            ("audioVolume", "audioVolume"),
            ("signal", "signal"),
            ("camLedStatus", "camLedStatus"),
            ("vehLedStatus", "vehLedStatus"),
            ("resumeBat", "resumeBat"),
            ("scheduleId", "scheduleId"),
            ("schedulePathOffset", "schedulePathOffset"),
            ("timezoneOffset", "timezoneOffset"),
            ("dockOnError", "dockOnError"),
        ]:
            if _has_field(rc, src):
                state[dst] = getattr(rc, src)
        rr = getattr(rc, "rrConfig", None)
        if _has_msg(rr):
            state["rrConfig"] = rr
            if _has_field(rr, "enableRr"):
                state["rrEnabled"] = bool(rr.enableRr)
            if _has_field(rr, "rechargeBat"):
                state["rrRechargeBat"] = rr.rechargeBat
            if _has_field(rr, "resumeBat"):
                state["rrResumeBat"] = rr.resumeBat
            if _has_msg(getattr(rr, "resumePeriodStart", None)):
                state["rrResumePeriodStart"] = _msg_to_tz_dict(rr.resumePeriodStart)
            if _has_msg(getattr(rr, "resumePeriodEnd", None)):
                state["rrResumePeriodEnd"] = _msg_to_tz_dict(rr.resumePeriodEnd)
        rtk_bind = getattr(rc, "rtkBinding", None)
        if rtk_bind is not None:
            locid = getattr(rtk_bind, "rtkLocid", "")
            if locid:
                state["rtkSn"] = locid
            pmode = getattr(rtk_bind, "powerMode", "")
            if pmode:
                state["rtkPowerMode"] = pmode

    wf = getattr(msg, "wifiConfigRes", None)
    if _has_msg(wf):
        state["wifiConfigRes"] = wf
        if _has_field(wf, "wifiRssi"):
            state["wifiRssi"] = wf.wifiRssi

    net = getattr(msg, "netDetailInfo", None)
    if _has_msg(net):
        state["netDetailInfo"] = net
        for key in [
            "currentNet", "wifiName", "wifiIp", "wifiSignal",
            "simCardStatus", "simIp", "simSignal", "simRegistration",
            "simConnection", "simIccid",
        ]:
            if _has_field(net, key):
                state[key] = getattr(net, key)

    # promptInfo (PbOutput field 15): transient command/check feedback the app uses
    # but we never parsed. Carries selfCheckingRet (pre-mow subsystem self-test),
    # mutateRet (command ack: code + human-readable errorMsg — the OTA-decline reason
    # we were hunting), and zoneRet (zone-edit result). Each sub-field is optional;
    # extract whichever is present and leave the others sticky.
    if any(f.name == "promptInfo" for f, _ in msg.ListFields()):
        pi = msg.promptInfo
        if _has_field(pi, "selfCheckingRet"):
            sc = pi.selfCheckingRet
            state["selfCheck"] = {
                "battery": bool(getattr(sc, "batteryPass", False)),
                "rtk": bool(getattr(sc, "rtkPass", False)),
                "cliff": bool(getattr(sc, "cliffPass", False)),
                "blade": bool(getattr(sc, "bladePass", False)),
                "rain": bool(getattr(sc, "rainPass", False)),
                "algo": bool(getattr(sc, "algoPass", False)),
            }
            _LOGGER.debug("Lymow self-check result: %s", state["selfCheck"])
        if _has_field(pi, "mutateRet"):
            mr = pi.mutateRet
            state["mutateResult"] = {"code": mr.code, "errorMsg": mr.errorMsg}
            _LOGGER.info("Lymow command result (mutateRet): code=%s msg=%r",
                         mr.code, mr.errorMsg)
        if _has_field(pi, "zoneRet"):
            zr = pi.zoneRet
            state["zoneResult"] = {"code": zr.code, "hashId": zr.hashId}

    # taskConfig: check parent ListFields (not ByteSize) because proto3
    # zero values (chargingMode=0) produce ByteSize=0 but are still valid.
    if any(f.name == "taskConfig" for f, _ in msg.ListFields()):
        tc = msg.taskConfig
        state["taskConfig"] = tc
        for src, dst in [
            ("chargingMode", "chargingMode"),
            ("zoneOrder", "zoneOrder"),
            ("rainCleaning", "rainCleaning"),
            ("disableChargingPark", "disableChargingPark"),
        ]:
            state[dst] = getattr(tc, src)

    rtk1 = getattr(msg, "rtkDiagnosticL1", None)
    if _has_msg(rtk1):
        state["rtkDiagnosticL1"] = rtk1
        if _has_field(rtk1, "rtkStatus"):
            state["rtkStatus"] = rtk1.rtkStatus
        # Per-band SNR, L5 sat count, and base-station data error rate — RTK
        # health detail (read directly; 0 is a valid reading for these metrics).
        state["rtkL1Snr"] = rtk1.l1Snr
        state["rtkL2Snr"] = rtk1.l2Snr
        state["rtkL5Snr"] = rtk1.l5Snr
        state["rtkL5Satellites"] = rtk1.l5SatelliteCount
        # Total satellites used in the RTK solution (all bands). This is the count the
        # Lymow app shows as "satellites" (~20) and the only sat metric with real spatial
        # variation. Drives the RTK Satellites sensor + the RTK Satellites heatmap layer.
        state["rtkSatellites"] = rtk1.satelliteCount
        state["rtkBaseDataErrorRate"] = round(float(rtk1.baseDataErrorRate), 3)

    rtk2 = getattr(msg, "rtkDiagnosticL2", None)
    if _has_msg(rtk2):
        state["rtkDiagnosticL2"] = rtk2
        # diffAge = age of the RTK correction data (s); high = degraded fix.
        # loraBps = RTK radio link rate per channel; cw/ant/hwDc = advanced
        # interference/antenna diagnostics (kept as attributes, not entities).
        state["rtkDiffAge"] = round(float(rtk2.diffAge), 2)
        state["rtkLoraBps"] = [rtk2.loraBps0, rtk2.loraBps1, rtk2.loraBps2]
        state["rtkCwRatio"] = [rtk2.cwRatio0, rtk2.cwRatio1, rtk2.cwRatio2]
        state["rtkAntValue"] = [rtk2.antValue0, rtk2.antValue1, rtk2.antValue2]
        state["rtkHwDc"] = [round(float(rtk2.hwDc0), 2), round(float(rtk2.hwDc1), 2), round(float(rtk2.hwDc2), 2)]

    # algoLocOutput (PbOutput field 19): localization-algo health. sensorConfidence
    # = per-sensor confidence (vision cameras + GNSS); useGnss = is GNSS in the fix.
    algo = getattr(msg, "algoLocOutput", None)
    if _has_msg(algo):
        state["algoLocOutput"] = algo
        state["useGnss"] = bool(getattr(algo, "useGnss", False))
        sc = getattr(algo, "sensorConfidence", None)
        if _has_msg(sc):
            state["cameraConfidence"] = [sc.camera1Conf, sc.camera2Conf]
            state["gnssConfidence"] = sc.gnssConf

    cr = getattr(msg, "cleanReport", None)
    if _has_msg(cr):
        state["lastCleanReport"] = cr

    return state


def _get_float(obj: Any, key: str) -> float | None:
    """Read a float from either a dict or an object attribute."""
    if obj is None:
        return None
    try:
        value = obj.get(key) if isinstance(obj, dict) else getattr(obj, key)
    except Exception:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def enu_to_lla(enu_base_point: Any, pose: Any) -> tuple[float, float] | None:
    """Convert local ENU metres to GPS lat/lon."""
    base_lat = _get_float(enu_base_point, "latitude")
    base_lon = _get_float(enu_base_point, "longitude")
    x = _get_float(pose, "x")  # east in metres
    y = _get_float(pose, "y")  # north in metres
    if base_lat is None or base_lon is None or x is None or y is None:
        return None
    lat = base_lat + (y / 111111.0)
    lon = base_lon + (x / (111111.0 * cos(radians(base_lat))))
    return lat, lon


def get_enu_base_point(state: dict[str, Any]) -> Any | None:
    ebp = state.get("enu_base_point")
    if ebp is not None:
        return ebp
    catalog = state.get("zone_catalog")
    ebp = getattr(catalog, "enu_base_point", None)
    if ebp is not None:
        return ebp
    btmap = state.get("btMap") or {}
    ebp = btmap.get("enuBasePoint") if isinstance(btmap, dict) else None
    return ebp


def get_robot_pose(state: dict[str, Any]) -> Any | None:
    for key in ("pose", "robotLoc", "robotPosePib"):
        pose = state.get(key)
        if pose is None:
            continue
        if isinstance(pose, dict):
            if pose.get("x") is not None and pose.get("y") is not None:
                return pose
        elif getattr(pose, "x", None) is not None and getattr(pose, "y", None) is not None:
            return pose
    return None


def robot_gps_from_state(state: dict[str, Any]) -> tuple[float, float] | None:
    derived = enu_to_lla(get_enu_base_point(state), get_robot_pose(state))
    if derived is not None:
        return derived

    loc = state.get("robotLocation")
    if isinstance(loc, (list, tuple)) and len(loc) >= 2:
        try:
            return float(loc[0]), float(loc[1])
        except (TypeError, ValueError):
            pass

    lla = state.get("robotLlaCoords")
    if isinstance(lla, dict):
        lat = lla.get("latitude")
        lon = lla.get("longitude")
        if lat is not None and lon is not None:
            try:
                return float(lat), float(lon)
            except (TypeError, ValueError):
                pass

    lat = state.get("latitude")
    lon = state.get("longitude")
    if lat is not None and lon is not None:
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            pass
    return None


def polygon_area(polygon: list[tuple[float, float]]) -> float:
    n = len(polygon)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) * 0.5


def point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_intersect = (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
            if x < x_intersect:
                inside = not inside
        j = i
    return inside


_M_PER_DEG_LAT = 111_320.0


def _latlon_to_local_m(lon: float, lat: float, lat0: float) -> tuple[float, float]:
    """Project (lon, lat) degrees to local planar metres about reference lat0.
    Good enough for the sub-100 m distances we test against channel polygons."""
    return (
        lon * _M_PER_DEG_LAT * cos(radians(lat0)),
        lat * _M_PER_DEG_LAT,
    )


def _point_seg_dist_m(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    """Distance from point P to segment AB (all in metres)."""
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0.0:
        return hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    t = max(0.0, min(1.0, t))
    return hypot(px - (ax + t * dx), py - (ay + t * dy))


def _dist_to_polygon_m(mlon: float, mlat: float, poly: list[tuple[float, float]]) -> float:
    """Min distance (metres) from the mower to a polygon's perimeter. `poly` is
    [(lon, lat), ...]; the mower is assumed outside (callers test inside first)."""
    n = len(poly)
    if n < 2:
        return float("inf")
    px, py = _latlon_to_local_m(mlon, mlat, mlat)
    best = float("inf")
    for i in range(n):
        alon, alat = poly[i]
        blon, blat = poly[(i + 1) % n]
        ax, ay = _latlon_to_local_m(alon, alat, mlat)
        bx, by = _latlon_to_local_m(blon, blat, mlat)
        d = _point_seg_dist_m(px, py, ax, ay, bx, by)
        if d < best:
            best = d
    return best


def _zones_from_state(state: dict[str, Any]) -> list[Any]:
    catalog = state.get("zone_catalog")
    zones = getattr(catalog, "zones", None)
    if isinstance(zones, list):
        return zones

    btmap = state.get("btMap") or {}
    zones = btmap.get("zones") if isinstance(btmap, dict) else None
    return zones if isinstance(zones, list) else []


def _nogo_zones_from_state(state: dict[str, Any]) -> list[Any]:
    catalog = state.get("zone_catalog")
    ng = getattr(catalog, "nogo_zones", None)
    if isinstance(ng, list):
        return ng
    btmap = state.get("btMap") or {}
    ng = btmap.get("nogoZones") if isinstance(btmap, dict) else None
    return ng if isinstance(ng, list) else []


def _localization_active(state: dict[str, Any]) -> bool:
    """Mower is actively positioned. Check BOTH robotStatus and workStatus: the
    mower reliably sets robotStatus (=Mowing) but often leaves workStatus unset,
    which previously made current zone/channel never resolve."""
    return (
        state.get("workStatus") in _LOCALIZE_STATUSES
        or state.get("robotStatus") in _LOCALIZE_STATUSES
    )


def _polygon_latlon(pts: list[Any], ebp: Any) -> list[tuple[float, float]]:
    """Convert ENU-metre polygon points -> [(lon, lat), ...] for WGS84 matching.
    Handles point dicts {x,y}, (x,y) tuples, or objects with .x/.y. Returns []
    if any point can't be converted."""
    out: list[tuple[float, float]] = []
    for p in pts:
        if isinstance(p, dict):
            px, py = p.get("x"), p.get("y")
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            px, py = p[0], p[1]
        else:
            px, py = getattr(p, "x", None), getattr(p, "y", None)
        ll = enu_to_lla(ebp, {"x": px, "y": py})
        if ll is None:
            return []
        out.append((ll[1], ll[0]))  # (lon, lat)
    return out


def corridor_ribbon(points: list[Any], half_width: float) -> list[tuple[float, float]]:
    """Offset a CENTRELINE path into a closed corridor RIBBON polygon of total width 2*half_width,
    mitred at each vertex (normal ⟂ the averaged in/out tangent). `points` are ENU (x, y) as
    dicts/tuples/objects; returns [(x, y), ...] ENU (left side forward + right side back), or [] if
    fewer than 2 points. Shared by the map renderer and channel detection so the corridor you SEE
    is exactly the corridor that's tested."""
    pts: list[tuple[float, float]] = []
    for p in points:
        if isinstance(p, dict):
            x, y = p.get("x"), p.get("y")
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y = p[0], p[1]
        else:
            x, y = getattr(p, "x", None), getattr(p, "y", None)
        if x is not None and y is not None:
            pts.append((float(x), float(y)))
    n = len(pts)
    if n < 2:
        return []
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    for i in range(n):
        if i == 0:
            tx, ty = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
        elif i == n - 1:
            tx, ty = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        else:
            ax, ay = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
            bx, by = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
            la = hypot(ax, ay) or 1.0
            lb = hypot(bx, by) or 1.0
            tx, ty = ax / la + bx / lb, ay / la + by / lb
        L = hypot(tx, ty) or 1.0
        nx, ny = -ty / L * half_width, tx / L * half_width
        x, y = pts[i]
        left.append((x + nx, y + ny))
        right.append((x - nx, y - ny))
    return left + right[::-1]


def derive_current_zone(state: dict[str, Any]) -> str | None:
    """Zone the mower is actively in, with overlap disambiguation.

    Geometric point-in-polygon (mower GPS vs each zone polygon, ENU→WGS84). But
    zones can overlap by many feet, and the mower never reports a single current
    zone over telemetry (curZoneId_ lives only in PbRebootResume; the official app
    also resolves this geometrically). So:
      * inside exactly ONE zone -> that zone (its exclusive area; unambiguous)
      * inside 2+ overlapping zones -> HOLD the previously-current zone if it is
        one of them ("sticky-until-exclusive": never switch *inside* an overlap —
        wait until the mower reaches a single zone's exclusive ground). If the prior
        zone isn't among them, tiebreak to the smallest (innermost) zone.
      * inside 0 zones (channel/transit gap) -> None, so the caller can fall back
        to channel detection.
    No-go zones take priority: if the mower has penetrated a no-go PAST the edge buffer
    (not just riding its edge on a perimeter lap), report "No Go: <name>" so an intrusion
    (a common get-stuck precursor) is visible rather than hidden behind the go-zone.
    Active-task zones (cleanInfo.areaInfo.cleanZoneIds) are PREFERRED, not required: in an
    overlap they win (an edge-clip into an adjacent zone while mowing resolves to the task
    zone). But a non-task zone is NEVER hidden — if the mower is genuinely inside one
    (travelling through it to a far zone, or stuck/dead there), Current Zone reports it, so
    the mower can always be located. (Hiding non-task zones would lose it in transit.)
    """
    if not _localization_active(state):
        return None
    mower = robot_gps_from_state(state)
    ebp = get_enu_base_point(state)
    if not mower or ebp is None:
        return None
    mlat, mlon = mower

    # No-go intrusion: if the mower has driven PAST the edge buffer INTO a no-go zone,
    # surface it explicitly as "No Go: <name>" — it shouldn't be there, and this is a
    # common precursor to getting stuck. Riding the no-go's edge during a perimeter lap
    # (within ZONE_EDGE_BUFFER_M of the boundary) is NOT flagged, so the mower stays
    # reported in its go-zone while lapping AROUND the obstacle; only a genuine
    # penetration past the buffer trips it.
    for i, ng in enumerate(_nogo_zones_from_state(state)):
        if isinstance(ng, dict):
            npts = ng.get("points") or []
            nname = ng.get("name") or ng.get("hashId")
        else:
            npts = getattr(ng, "polygon_points", []) or []
            nname = getattr(ng, "name", None) or getattr(ng, "hash_id", None)
        if not npts or len(npts) < 3:
            continue
        npoly = _polygon_latlon(npts, ebp)
        if len(npoly) < 3:
            continue
        if point_in_polygon(mlon, mlat, npoly) and _dist_to_polygon_m(mlon, mlat, npoly) > ZONE_EDGE_BUFFER_M:
            return f"No Go: {nname or f'#{i + 1}'}"

    task_ids = set(state.get("cleanZoneIds") or [])
    containing: list[tuple[Any, Any, float]] = []  # (name, hashId, polygon area)
    for zone in _zones_from_state(state):
        if isinstance(zone, dict):
            pts = zone.get("points") or []
            name = zone.get("name") or zone.get("hashId")
            hid = zone.get("hashId")
        else:
            pts = getattr(zone, "polygon_points", []) or []
            name = getattr(zone, "name", None) or getattr(zone, "hash_id", None)
            hid = getattr(zone, "hash_id", None)
        if not pts or len(pts) < 3:
            continue
        poly = _polygon_latlon(pts, ebp)
        if len(poly) < 3:
            continue
        # Inside the polygon OR within ZONE_EDGE_BUFFER_M of its edge — the buffer
        # keeps the zone held while the mower rides the boundary on a perimeter lap
        # (GPS on/just outside the edge would otherwise drop out to unknown).
        if point_in_polygon(mlon, mlat, poly) or _dist_to_polygon_m(mlon, mlat, poly) <= ZONE_EDGE_BUFFER_M:
            containing.append((name, hid, polygon_area(pts)))

    if not containing:
        return None  # transit / channel gap — caller falls back to channel

    # PREFER (don't restrict to) the active-task zones. In an overlap, resolve to the task
    # zone — so an edge-clip into an adjacent zone while mowing reads as the task zone. But
    # if the mower is genuinely inside a NON-task zone (travelling through it to reach a far
    # zone, or stuck / out of battery / errored there), we still report THAT zone, because
    # the whole point of Current Zone is being able to LOCATE the mower wherever it is.
    if task_ids:
        in_task = [c for c in containing if c[1] in task_ids]
        if in_task:
            containing = in_task

    if len(containing) == 1:
        return containing[0][0]

    # Overlap: hold the last unambiguous zone (sticky-until-exclusive). Only switch
    # once the mower reaches a single zone's exclusive area (handled by the len==1
    # branch above). If we entered the overlap with no prior match, pick the
    # smallest (innermost / most specific) zone.
    prev = state.get("currentZone")
    names = [c[0] for c in containing]
    if prev in names:
        return prev
    containing.sort(key=lambda c: c[2])
    return containing[0][0]


def _channels_from_state(state: dict[str, Any]) -> list[Any]:
    catalog = state.get("zone_catalog")
    chans = getattr(catalog, "channels", None)
    if isinstance(chans, list):
        return chans
    btmap = state.get("btMap") or {}
    chans = btmap.get("channels") if isinstance(btmap, dict) else None
    return chans if isinstance(chans, list) else []


def _zone_name_by_hash(state: dict[str, Any]) -> dict[str, str]:
    """Map zone hashId -> display name, for labelling channels by the zones they link."""
    out: dict[str, str] = {}
    for z in _zones_from_state(state):
        if isinstance(z, dict):
            h = z.get("hashId"); n = z.get("name") or z.get("zoneRename")
        else:
            h = getattr(z, "hash_id", None); n = getattr(z, "name", None)
        if h:
            out[h] = n or h
    return out


def derive_current_channel(state: dict[str, Any]) -> dict[str, Any] | None:
    """Channel whose polygon contains the mower's live pose (active mowing only).

    Returns {label, channel_id, zone1, zone2, is_docking} or None. The label is
    the human-readable link, e.g. "Front Left Main ↔ Backyard" — useful for
    automations that fire on a transition corridor (e.g. opening a gate)."""
    if not _localization_active(state):
        return None
    mower = robot_gps_from_state(state)
    ebp = get_enu_base_point(state)
    if not mower or ebp is None:
        return None
    mlat, mlon = mower

    bm = state.get("channel_buffer_m")
    buffer_m = float(bm) if bm is not None else DEFAULT_CHANNEL_BUFFER_M

    names = _zone_name_by_hash(state)
    best: dict[str, Any] | None = None
    best_dist = float("inf")
    for ch in _channels_from_state(state):
        if isinstance(ch, dict):
            pts = ch.get("points") or []
            hid = ch.get("hashId"); z1 = ch.get("zone1", ""); z2 = ch.get("zone2", "")
            dock = ch.get("isDockingChannel")
        else:
            pts = getattr(ch, "polygon_points", []) or []
            hid = getattr(ch, "hash_id", None)
            z1 = getattr(ch, "zone1", ""); z2 = getattr(ch, "zone2", "")
            dock = getattr(ch, "is_docking_channel", False)

        if not pts or len(pts) < 2:
            continue
        # The channel points are a CENTRELINE path — test against the corridor RIBBON (the same
        # geometry the map draws), not the raw points closed into a thin triangle. The ribbon's own
        # width replaces almost all of what the old radial buffer compensated for, so buffer_m now
        # defaults to ~0 (just extra GPS slack, still user-tunable).
        ribbon = corridor_ribbon(pts, CHANNEL_RIBBON_HALFWIDTH_M)
        poly = _polygon_latlon(ribbon, ebp)
        if len(poly) < 3:
            continue

        # Inside the corridor = distance 0. Otherwise accept when within buffer_m of its edge
        # (extra GPS slack; default 0). When several channels qualify (junctions), the nearest wins.
        if point_in_polygon(mlon, mlat, poly):
            dist = 0.0
        elif buffer_m > 0.0:
            dist = _dist_to_polygon_m(mlon, mlat, poly)
            if dist > buffer_m:
                continue
        else:
            continue

        if dist >= best_dist:
            continue
        n1 = names.get(z1, z1) or ""
        n2 = names.get(z2, z2) or ""
        if dock:
            label = f"Dock ↔ {n1 or n2}".strip()
        elif n1 and n2:
            label = f"{n1} ↔ {n2}"
        else:
            label = f"Channel {hid[:6]}" if hid else "Channel"
        best_dist = dist
        best = {
            "label": label, "channel_id": hid,
            "zone1": n1, "zone2": n2, "is_docking": bool(dock),
            "distance_m": round(dist, 2),
        }
    return best


# The charger spot sits OUTSIDE every zone and channel, so undocking/docking there would
# read as Off-Map. Treat positions within this many metres of the dock as not-a-breach.
DOCK_EXEMPT_M = 4.0


def _near_dock(state: dict[str, Any], radius: float = DOCK_EXEMPT_M) -> bool:
    """True if the robot's ENU pose is within `radius` m of the dock (reported or derived).
    Both are ENU metres, so the distance is direct."""
    pose = get_robot_pose(state)
    if pose is None:
        return False
    try:
        rx = float(pose["x"] if isinstance(pose, dict) else pose.x)
        ry = float(pose["y"] if isinstance(pose, dict) else pose.y)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    r2 = radius * radius
    for src in ("chargingStationLoc", "derived_dock"):
        dk = state.get(src)
        if not isinstance(dk, dict):
            continue
        try:
            dx, dy = float(dk["x"]), float(dk["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if (rx - dx) ** 2 + (ry - dy) ** 2 <= r2:
            return True
    return False


def resolve_location(state: dict[str, Any], docked: bool):
    """Single source of truth for WHERE the mower is — the instantaneous resolution that
    Location State, Current Zone, and Current Channel all read from (so they can never
    disagree). Precedence: No-Go > Zone > Channel > Off-Map.

    Returns (location_label, current_zone, current_channel, channel_info), or None when the
    position can't be evaluated (paused/idle/partial frame or no GPS) so the caller HOLDS the
    previous sticky values. The Off-Map result is INSTANTANEOUS — the caller debounces it
    (sustained off-map) before treating it as a real geofence breach, so GPS jitter near a
    boundary can't blip a false breach.

    States: "Docked" · "Zone: <name>" · "Channel: <label>" · "No Go: <name>" · "Off-Map".
    Current Zone mirrors it cleanly: zone name · "No Go: <name>" · "Docked" · "Transit"
    (in a channel) · "Off-Map". Current Channel: label · "None" (in a zone/docked) · "Off-Map".
    """
    if docked:
        return ("Docked", "Docked", "None", None)
    if not (_localization_active(state)
            and robot_gps_from_state(state) and get_enu_base_point(state) is not None):
        return None  # idle / paused / partial / no-fix → caller keeps previous (sticky)

    zone = derive_current_zone(state)
    if zone and str(zone).startswith("No Go"):
        return (zone, zone, "None", None)                       # no-go intrusion = breach
    if zone:
        return (f"Zone: {zone}", zone, "None", None)            # in a known zone
    channel = derive_current_channel(state)
    if channel:
        lbl = channel.get("label") or "Channel"
        return (f"Channel: {lbl}", "Transit", lbl, channel)     # in a transit corridor
    # Not in a zone or channel. If we're at/near the charger (the dock spot is outside every
    # zone and channel), this is an undock/dock maneuver, NOT a breach — hold previous.
    if _near_dock(state):
        return None
    return ("Off-Map", "Off-Map", "Off-Map", None)              # in NOTHING known → breach (debounce!)
