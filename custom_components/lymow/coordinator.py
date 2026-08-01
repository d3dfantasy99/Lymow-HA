"""DataUpdateCoordinator for Lymow — MQTT push-driven.

State arrives via MQTT /pboutput broadcasts.
Commands are published to /pbinput as protobuf messages.

Startup + periodic refresh:
  On connect:
    build_initial_query_packets() → map, path, schedules, cleaning info,
                                      appConnect, debug profile/WiFi, robot config, net detail, WiFi/4G, RTK L1/L2

  Every _REFRESH_INTERVAL (default 90s):
    build_refresh_query_packets()
    (ensures IP address, signal info and RTK stay current without the app open)

  On MQTT disconnect:
    Refresh AWS credentials (they expire after ~1h causing the disconnect)
    Re-create paho connection with a new presigned URL
    Re-fire startup queries

REST poll every 15 min: device online/offline fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from typing import Any
import re

try:
    from google.protobuf.json_format import MessageToDict as _pb_to_dict
except Exception:  # pragma: no cover
    _pb_to_dict = None

# Debug capture: when True, every frame + QUERY_PATH pull is logged (timestamped,
# with full telemetry + raw cut/planned chunks) to lymow_pathcap_<thing>.jsonl in
# the HA config dir, for offline path/chunk-decode analysis. Flip OFF (or delete
# the captured file) once the path decode is solved. Over-capture by design.
# Dev override only. NORMAL operation: capture is driven by the user-facing #4
# Diagnostic Capture switch (set_diag_capture) — flip it ON before a mow to record the
# full raw 333/444 stream + pose + zone for offline validation, OFF when done. The
# static perimeter (444) is de-duped per session so a multi-zone mow stays small.
DEBUG_PATHCAP_ENABLED = False
_PATHCAP_MAX_BYTES = 150 * 1024 * 1024  # safety cap (~150 MB; de-dup keeps real files far smaller)
from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.util import dt as dt_util
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import CognitoAuth, LymowClient, LymowError
from .const import (
    audio_label,
    COVERAGE_STYLE_DEFAULT,
    DOMAIN,
    error_label,
    F_DEVICE_STATE,
    F_IS_CHARGING,
    MOWING_STATUSES,
    WORK_STATUS_CHARGING,
    WORK_STATUS_CHARGING_FULL,
    WORK_STATUS_DOCKING,
    WORK_STATUS_OFFLINE,
    WORK_STATUS_PAUSE_DOCKING,
)
from .mqtt import MqttClient
from .protocol import (
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_FORCE_REINIT,
    USER_CTRL_PAUSE,
    USER_CTRL_PAUSE_DOCK,
    USER_CTRL_RECHARGE_DOCK,
    USER_CTRL_RESUME,
    USER_CTRL_RESUME_DOCK,
    build_initial_query_packets,
    build_refresh_query_packets,
    encode_query_map,
    encode_query_path,
    encode_query_schedules,
    encode_query_robot_config,
    decode_pboutput_envelope,
    parse_query_path,
    parse_zone_catalog,
    parse_schedules,
    encode_start_zones,
    encode_start_schedule_task_full,
    encode_userctrl,
    encode_set_rr_config,
    encode_set_headlights,
    encode_set_vehicle_led,

    encode_remote_control,
    encode_remote_stop,
)
from .state import (
    _ACTIVE_TASK_WORK_STATUSES,
    _localization_active,
    derive_current_zone,
    derive_current_channel,
    resolve_location,
    get_enu_base_point,
    get_robot_pose,
    merge_pboutput,
    polygon_area,
    robot_gps_from_state,
)
from .map_tuning import (
    COMPLETE_FRAC, DWELL_RADIUS_M, DWELL_TIME_S,
    DWELL_DISREGARD_S, DWELL_DISREGARD_TURNS, DWELL_EXCESS_TURNS,
    DWELL_SPIN_TURNS, DWELL_JITTER_PATH_M, DWELL_STRUGGLE_PATH_M,
)
from .state_matrix import lookup as lookup_state_row
from .path_engine import classify_segments, CutAccumulator, BreadcrumbAccumulator
from .obstacles import detect_obstacles
from .pass_coverage import analyze_pass_coverage
from .zone_stats import assign_to_zones, point_in_polygon
from .coverage_worker import compute_coverage
from .zone_coverage import ZoneCoverageHistory, cells_for_points

_LOGGER = logging.getLogger(__name__)

# Sustained seconds the mower must resolve to Off-Map (in no zone, no channel, while active)
# before we declare a geofence breach — guards against GPS jitter blipping at a boundary.
LOCATION_OFFMAP_DEBOUNCE_S = 8.0

_REST_POLL_INTERVAL = timedelta(minutes=15)
_REFRESH_INTERVAL   = 30          # seconds — periodic config/net/RTK refresh
_RECONNECT_DELAY    = 5           # seconds — wait before reconnect attempt
_WATCHDOG_TIMEOUT   = 5.0

_PATH_REFRESH_INTERVAL = 15



_REMOTE_LINEAR_SPEED = 0.25
_REMOTE_ANGULAR_SPEED = 0.35
_REMOTE_PULSE_SECONDS = 0.35



class LymowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push-only coordinator for a single Lymow robot."""

    # Persisted across restarts (config entry). Device IDs that arrive in rare
    # full-deviceInfo frames, plus chargingMode (dock route) which the mower only
    # sends in taskConfig — without this the Return-to-Dock select reverts to its
    # "Follow Perimeter" default on every reload until taskConfig is re-received.
    _STICKY_KEYS = ("rtkSn", "wheelVer", "knifeVer", "rtkPowerMode", "chargingMode",
                    "rainCleaning", "coverage_style", "map_layer", "heatmap_style",
                    "map_labels", "map_resolution", "mower_size", "session_percent_display")

    def __init__(
        self,
        hass: HomeAssistant,
        auth: CognitoAuth,
        client: LymowClient,
        thing_name: str,
        region: str,
        email: str,
        password: str,
        config_entry: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{thing_name}",
            update_interval=None,   # push-only
        )
        self.auth       = auth
        self.client     = client
        self.thing_name = thing_name
        self._region    = region
        self._email     = email
        self._password  = password
        self._client_uuid = str(uuid.uuid4())
        self._config_entry = config_entry

        # Firmware OTA (cloud AWS IoT Job) — job id + background poll task.
        self._ota_job_id: str | None = None
        self._ota_poll_task: asyncio.Task | None = None

        self._state: dict[str, Any] = {}
        self.device_info_data: dict = {}

        # Restore sticky device info from config entry (survives restarts)
        if config_entry and config_entry.data.get("sticky_device_info"):
            self._state.update(config_entry.data["sticky_device_info"])
        self._state.setdefault("coverage_style", COVERAGE_STYLE_DEFAULT)
        self._state.setdefault("map_layer", "Coverage")
        self._state.setdefault("heatmap_style", "Smooth")
        self._state.setdefault("map_labels", "Both")
        self._state.setdefault("map_resolution", "Large")
        # Persistent per-zone mow history (last_mowed / mow_count / minutes / area),
        # restored from the config entry. Survives restarts across sessions.
        if config_entry and config_entry.data.get("zone_history"):
            self._state["zone_history"] = dict(config_entry.data["zone_history"])
        self._state.setdefault("zone_history", {})
        # Migrate legacy history to the unified per-zone schema: drop the old name-keyed
        # duplicate entries (no zone_id), remap renamed fields, and drop dead keys. Keeps
        # released users' data intact while collapsing to one record per zone (hashId key).
        _ZH_REMAP = {"last_mowed_at": "last_mowed", "last_end_type": "end_type",
                     "session_total_duration_s": "mowing_minutes",
                     "session_total_area_m2": "area_covered_m2",
                     "total_minutes": "session_minutes", "last_coverage_points": "coverage_points",
                     "area_m2": "zone_area_m2"}
        _ZH_DROP = {"last_clean_start_time", "last_session_event_id"}

        def _zh_remap(v):
            return {_ZH_REMAP.get(k, k): vv for k, vv in v.items() if k not in _ZH_DROP}
        _items = list((self._state.get("zone_history") or {}).items())
        _zh_mig, _by_name = {}, {}
        _n_canon = _n_folded = _n_kept = 0
        for _k, _v in _items:                       # pass 1: canonical hashId-keyed records
            if isinstance(_v, dict) and _v.get("zone_id"):
                _zh_mig[_k] = _zh_remap(_v)
                _by_name[_zh_mig[_k].get("zone_name")] = _k
                _n_canon += 1
        for _k, _v in _items:                       # pass 2: fold legacy name-keyed halves in
            if isinstance(_v, dict) and not _v.get("zone_id"):
                _tgt = _by_name.get(_k)             # legacy key WAS the zone name
                if _tgt:
                    for _kk, _vv in _zh_remap(_v).items():
                        _zh_mig[_tgt].setdefault(_kk, _vv)
                    _n_folded += 1
                elif _k not in _zh_mig:
                    # No canonical hashId record to fold into. KEEP it rather than drop it —
                    # never lose a user's history on an older/unforeseen format. It stays under
                    # its original key (hashId-keyed entities won't surface it, but the data is
                    # preserved + persisted + recoverable). [hardening 2026-06-22]
                    _zh_mig[_k] = _zh_remap(_v)
                    _n_kept += 1
        self._state["zone_history"] = _zh_mig
        if _n_kept:
            _LOGGER.warning(
                "zone_history migration preserved %d legacy record(s) with no canonical "
                "hashId match (kept, NOT dropped) — %d canonical, %d folded. If zone history "
                "looks wrong after upgrade, please report with this log.",
                _n_kept, _n_canon, _n_folded)
        elif _items:
            _LOGGER.debug("zone_history migration: %d canonical, %d legacy folded",
                          _n_canon, _n_folded)
        # Derived dock location (captured from the mower's pose while charging) — restored
        # so the dock marker is present immediately, even before the mower reports it.
        if config_entry and config_entry.data.get("derived_dock"):
            self._state["derived_dock"] = dict(config_entry.data["derived_dock"])
        self.history: list[dict]    = []

        self._rest_online: bool    = False
        self._last_mqtt_ts: float  = 0.0
        self._prev_work_status: int | None = None
        self._shutting_down        = False

        self.mqtt: MqttClient | None = None

        self._rest_poll_task:    asyncio.Task | None = None
        self._refresh_task:      asyncio.Task | None = None
        self._reconnect_task:    asyncio.Task | None = None
        self._path_refresh_task: asyncio.Task | None = None

        self._state_event = asyncio.Event()

        # Path engine: accumulates the session actual/cut track across QUERY_PATH pulls.
        # Persisted to disk so an HA restart mid-mow doesn't lose the coverage history.
        self._cut_accumulator = CutAccumulator()
        self._cut_store = Store(self.hass, 1, f"lymow_cut_{thing_name}")
        # Planned route (projected path) persistence. The planned coords are
        # otherwise live-only (recomputed each QUERY_PATH, dropped on a new
        # session), so a restart loses them and completed runs can't be
        # re-analyzed against the projection. Persist them alongside the cut track.
        self._planned_store = Store(self.hass, 1, f"lymow_planned_{thing_name}")
        # Live-position breadcrumb track (time-ordered, telemetry-tagged): the
        # high-fidelity actual path that avoids the QUERY_PATH chunk-reorder
        # scramble. Persisted, and reset per-mow on the mowing-start transition.
        self._breadcrumbs = BreadcrumbAccumulator()
        self._breadcrumb_store = Store(self.hass, 1, f"lymow_breadcrumb_{thing_name}")
        # Session-scoped annotations (deviations + obstacle events) persisted so a
        # reload keeps them until a NEW task starts (then cleared).
        self._annot_store = Store(self.hass, 1, f"lymow_annot_{thing_name}")
        # PERSISTENT per-zone coverage masks (zone_coverage.py). Unlike the cut/breadcrumb
        # tracks above (per-session, reset on a new task), these survive restarts AND new
        # tasks: each zone keeps its last mow on the map until that zone is itself re-mowed
        # (copy-on-write). Bounded by zone AREA, not mow time — a half-acre yard ≈ tens of KB.
        self._zone_coverage = ZoneCoverageHistory()
        self._zonecov_store = Store(self.hass, 1, f"lymow_zonecov_{thing_name}")
        self._zonecov_save_counter = 0
        self._offmap_since: float | None = None   # debounce timer for the Off-Map geofence breach
        self._mow_session_key: int = 0
        self._was_active: bool = False
        self._breadcrumb_save_counter = 0
        # Dwell / stuck anomaly detector (#5): the current tight pose cluster + the index of its
        # in-progress anomaly event (so it updates/escalates in place until the mower moves on).
        self._dwell_cluster: list[tuple] = []      # (x, y, theta, monotonic_t) while mowing
        self._dwell_evidx: int | None = None
        # Diagnostic Capture switch (#4): write the render-input snapshot while ON.
        self._diag_capture: bool = False
        self._diag_last_write: float = 0.0
        self._diag_path = self.hass.config.path(f"lymow_diagnostic_{thing_name}.json")
        # Per-zone PLAN HISTORY: the mower hands us a fresh planned route per zone/replan,
        # and _planned_store keeps only the LATEST (overwritten each zone). To enable
        # actual-vs-planned deviation/miss detection we must keep EVERY zone's plan,
        # tagged with zone + time + ENU base. Append-only; snapshot on plan-change only.
        self._plan_hist_path = self.hass.config.path(f"lymow_plan_history_{thing_name}.jsonl")
        self._plan_hist_fp: tuple | None = None
        # Debug session recorder buffer (flushed to JSONL).
        self._pathcap_buf: list[dict] = []
        self._pathcap_path = self.hass.config.path(f"lymow_pathcap_{thing_name}.jsonl")
        self._pathcap_last_audio: int | None = None
        # Perimeter (444) fingerprints already logged this capture session — the static
        # perimeter re-streams every pull, so we log its full points ONCE per fingerprint
        # and just note recurrence after that (keeps a multi-zone capture small + clean).
        self._pathcap_perim_fps: set = set()
        self._pathcap_truncated: bool = False   # set once if we ever hit the size cap
        self._last_bc_wall: float | None = None  # wall-clock of last breadcrumb append (burst detect)
        self._backprop_until: float = 0.0        # tag rapid poses as back-prop until this wall-clock
        self._activity_phase: str = "perimeter"  # what the mower is cutting now (perimeter/main)
        self._main_pts_prev: int = 0             # 333 main-cut point total last pull (growth = active)
        self._perim_pts_prev: int = 0            # 444 perimeter point total last pull (growth = active)
        # Last planned-route total point count, to detect when the route has
        # finished building (plateaued) before accumulating the cut track.
        self._last_planned_total: int | None = None
        # Previous pull's large-segment fingerprints, for staticity-based cut/planned
        # classification (classify_segments). Reset on the docked→mowing edge.
        self._prev_large_fps: set = set()
        # Perimeter/Structural display debounce: hold the last value confirmed stable
        # across ≥2 consecutive pulls, and never drop to 0 mid-mow — kills the
        # build-phase flicker (a growing segment momentarily stalls, briefly counts,
        # then grows and drops back to 0). Reset on the docked→mowing edge.
        self._perim_stable: int = 0
        self._perim_prev: int = 0
        self._obstacle_scan: int = 0   # throttle counter for coverage-hole obstacle scans
        # Heavy coverage attribution is GIL-bound pure Python that blocks HA's loop
        # 0.5–1.8s/tick on a large lawn (see bench.py). Offload it to a spawn subprocess.
        # Single-flight: never queue a second compute while one is running. Gated by the
        # render_multiprocessing option. [eve]
        self._cov_executor = None          # ProcessPoolExecutor | None (lazy, in async_setup)
        self._cov_inflight: bool = False
        self._pass_cov_bootstrapped: bool = False  # one-shot pass-coverage compute on startup
        # Zone-visit accumulator (this mow): per-zone mow-ONLY seconds + battery, plus
        # session travel time. Time accrues only while actively mowing inside a real zone,
        # so transit / a mid-mow recharge / docking naturally pause the per-zone timer.
        self._zone_visit: dict = {}        # zone_name -> {mow_seconds, batt_in, batt_last}
        self._zv_last_ts = None            # last frame timestamp (for dt)
        self._zv_travel_s: float = 0.0     # session seconds NOT mowing-in-a-zone (transit/charge)
        self._zv_mow_batt_prev = None      # battery at the previous mowing frame (per-zone drain)
        self._zv_mow_zone = None           # zone of that previous mowing frame
        self._mow_start_ts = None      # session start time, for per-zone mow duration

    # ── Setup / teardown ────────────────────────────────────────

    async def async_setup(self) -> None:
        """Authenticate, load REST/S3 map fallback, connect MQTT, fire startup queries."""
        self._shutting_down = False
        # Restore the in-progress coverage track so a restart mid-mow doesn't reset it.
        try:
            self._cut_accumulator.load_dict(await self._cut_store.async_load())
            # Surface the restored track in state immediately so the coverage map
            # isn't blank after a restart (QUERY_PATH only repopulates once mowing).
            restored = self._cut_accumulator.points
            if restored:
                self._state["coverage_track"] = restored
        except Exception:
            _LOGGER.debug("No saved cut track to restore for %s", self.thing_name)
        # Restore the planned route too, so the projected path survives a restart
        # and stays available for rendering/re-analysis until the next mow rebuilds it.
        try:
            saved_planned = await self._planned_store.async_load()
            if saved_planned and saved_planned.get("planned_segments"):
                self._state["planned_path_segments"] = saved_planned["planned_segments"]
        except Exception:
            _LOGGER.debug("No saved planned route to restore for %s", self.thing_name)
        # Restore the breadcrumb track + held annotations, and seed the mow-session
        # counter / active flag so a mid-mow restart neither loses the track nor
        # spuriously resets it on the first frame back.
        try:
            self._breadcrumbs.load_dict(await self._breadcrumb_store.async_load())
            if isinstance(self._breadcrumbs.session_key, int):
                self._mow_session_key = self._breadcrumbs.session_key
            if self._breadcrumbs.points:
                self._state["breadcrumb_track"] = self._breadcrumbs.points
        except Exception:
            _LOGGER.debug("No saved breadcrumb track for %s", self.thing_name)
        try:
            annot = await self._annot_store.async_load()
            if annot:
                if annot.get("obstacle_events"):
                    self._state["obstacle_events"] = annot["obstacle_events"]
        except Exception:
            _LOGGER.debug("No saved annotations for %s", self.thing_name)
        # Restore the persistent per-zone coverage masks so every zone's last mow is
        # on the map immediately after a restart / HACS reload (the whole point of #4).
        try:
            self._zone_coverage.load_dict(await self._zonecov_store.async_load())
            # Seed/refresh last_mowed from the mower's per-zone history — but ONLY from genuine
            # COMPLETED mows. zone_history stamps last_mowed even on a rained-out / cancelled run
            # (end_type != "completed"), so gating on completed keeps that rain-out timestamp from
            # leaking in and falsely marking a zone "mowed" on the next restart. seed_last_mowed
            # only ADVANCES (monotonic), and the enu_base fix means a zone now keeps its own
            # completion-gated date across reconciles — so this is bootstrap, never a regression.
            _seed = {}
            for _zid, _h in (self._state.get("zone_history") or {}).items():
                if _h.get("end_type") == "completed" and _h.get("last_mowed"):
                    try:
                        _seed[_zid] = {
                            "last_mowed": datetime.fromisoformat(_h["last_mowed"]).timestamp(),
                            "name": _h.get("zone_name"),
                        }
                    except (TypeError, ValueError):
                        pass
            if self._zone_coverage.seed_last_mowed(_seed):
                self.hass.async_create_task(
                    self._zonecov_store.async_save(self._zone_coverage.to_dict()))
            self._state["zone_coverage_history"] = self._zone_coverage.render_masks()
            self._state["zone_last_mowed"] = self._zone_coverage.last_mowed_map()
            self._state["mow_interval_days"] = self._zone_coverage.mow_interval_days
            self._state["dim_by_age"] = self._zone_coverage.dim_by_age
        except Exception:
            _LOGGER.debug("No saved per-zone coverage for %s", self.thing_name)
        self._was_active = _localization_active(self._state)
        await self.auth.ensure_valid(self._email, self._password)

        # REST metadata first: device_info gives IP/firmware/location fallback.
        await self._do_rest_poll()

        await self._connect_mqtt()

        self._rest_poll_task = self.hass.async_create_background_task(
            self._rest_poll_loop(), name=f"lymow_rest_poll_{self.thing_name}"
        )
        self._refresh_task = self.hass.async_create_background_task(
            self._refresh_loop(), name=f"lymow_refresh_{self.thing_name}"
        )
        self._path_refresh_task = self.hass.async_create_background_task(
            self._path_refresh_loop(),
            name=f"lymow_path_refresh_{self.thing_name}",
        )
        # Spawn the coverage-compute subprocess (one worker; spawn, not fork — fork is
        # unsafe alongside asyncio/threads). Gated by the render_multiprocessing option.
        use_mp = True
        if self._config_entry is not None:
            use_mp = self._config_entry.options.get("render_multiprocessing", True)
        if use_mp and self._cov_executor is None:
            try:
                self._cov_executor = ProcessPoolExecutor(
                    max_workers=1, mp_context=multiprocessing.get_context("spawn")
                )
            except Exception:
                _LOGGER.warning("Could not start coverage subprocess for %s — running inline",
                                self.thing_name, exc_info=True)
                self._cov_executor = None

    async def async_shutdown(self) -> None:
        """Disconnect MQTT and cancel all background tasks."""
        self._shutting_down = True
        # Flush any buffered debug-capture records + breadcrumb track.
        if self._pathcap_buf:
            buf, self._pathcap_buf = self._pathcap_buf, []
            try:
                await self.hass.async_add_executor_job(self._flush_pathcap, buf)
            except Exception:
                pass
        for task_attr in ("_rest_poll_task", "_refresh_task",  "_path_refresh_task", "_reconnect_task"):
            task = getattr(self, task_attr)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, task_attr, None)
        if self.mqtt:
            await self.mqtt.disconnect()
            self.mqtt = None
        if self._cov_executor is not None:
            self._cov_executor.shutdown(wait=False)
            self._cov_executor = None

    async def _connect_mqtt(self) -> None:
        """Create and connect a new MqttClient with current credentials."""
        # Disconnect old client if any
        if self.mqtt:
            try:
                await self.mqtt.disconnect()
            except Exception:
                pass
            self.mqtt = None

        self.mqtt = MqttClient(
            thing_name=self.thing_name,
            host=self.client._ep["iotDomain"].replace("https://", "").rstrip("/"),
            region=self._region,
            on_pboutput=self._handle_pboutput,
            on_notify_app=self._handle_notify_app,
            on_disconnect_cb=self._handle_disconnect,
        )
        await self.mqtt.connect(
            access_key=self.auth.access_key_id,
            secret_key=self.auth.secret_access_key,
            session_token=self.auth.session_token,
        )
        _LOGGER.debug("MQTT connected for %s — firing startup queries", self.thing_name)
        self._fire_startup_queries()

    async def _path_refresh_loop(self) -> None:
        """Frequently query path while the mower is actively working."""
        while True:
            try:
                await asyncio.sleep(_PATH_REFRESH_INTERVAL)

                if not self.mqtt or not self.mqtt.is_connected:
                    continue

                if self.work_status in _ACTIVE_TASK_WORK_STATUSES:
                    _LOGGER.debug("Path refresh query for %s", self.thing_name)
                    self._publish(encode_query_path())

            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Path refresh loop error for %s", self.thing_name)

    def _fire_startup_queries(self) -> None:
        """Publish all startup queries. Also called after reconnect."""
        for raw in build_initial_query_packets(client_uuid=self._client_uuid):
            self._publish(raw)

        self.hass.async_create_task(self._delayed_query_schedules(3))
        # Robot only responds to robotConfig query after being online for a few seconds
        self.hass.async_create_task(self._delayed_robot_config(4))
        # The startup map query is sometimes dropped; retry until the map loads.
        self.hass.async_create_task(self._delayed_map_query(5))

    def _fire_refresh_queries(self) -> None:
        """Periodic refresh — keeps IP, signal, RTK and config up to date."""
        for raw in build_refresh_query_packets(client_uuid=self._client_uuid):
            self._publish(raw)
        # Self-heal the map: it can get lost mid-session (catalog emptied or GPS
        # origin dropped), which makes current zone/channel impossible. The
        # startup retry returns once loaded and won't catch a mid-mow loss, so
        # re-request the map here whenever it's incomplete.
        cat = self._state.get("zone_catalog")
        has_map = (
            cat is not None
            and (getattr(cat, "channels", None) or getattr(cat, "zones", None))
            and get_enu_base_point(self._state) is not None
        )
        if not has_map:
            self._publish(encode_query_map())
    
    async def _delayed_robot_config(self, delay: int) -> None:
        """Query robotConfig on startup, retrying with backoff until it lands.

        The robot only answers a robotConfig query after it's been online for a
        few seconds, so a single early query can be silently dropped — leaving
        Speaker Volume / recharge config "unknown" until a manual set or the next
        30s refresh. Retry until audioVolume populates (or attempts run out)."""
        for wait in (delay, 6, 10, 20):
            await asyncio.sleep(wait)
            if self._state.get("audioVolume") is not None:
                return
            self._publish(encode_query_robot_config())

    async def _delayed_map_query(self, delay: int) -> None:
        """Retry the map query until the zone map loads. The startup query_map is
        sometimes dropped (like robotConfig) and the 30s refresh never re-requests
        it, leaving zone_catalog empty — which makes current zone/channel
        (point-in-polygon vs the map) impossible until a manual Refresh Map."""
        for wait in (delay, 8, 15, 30):
            await asyncio.sleep(wait)
            cat = self._state.get("zone_catalog")
            if cat is not None and (getattr(cat, "channels", None) or getattr(cat, "zones", None)):
                return
            self._publish(encode_query_map())
    
    async def _delayed_query_schedules(self, delay: int) -> None:
        """Send robot schedules query with delay — mirrors app behavior (1s + 5s)."""
        await asyncio.sleep(delay)
        self._publish(encode_query_schedules())
    # ── Properties ──────────────────────────────────────────────

    @property
    def work_status(self) -> int:
        return self._state.get("workStatus", WORK_STATUS_OFFLINE)
    
    @property
    def robot_status(self) -> int:
        return self._state.get("robotStatus", WORK_STATUS_OFFLINE)

    @property
    def is_online(self) -> bool:
        if not self._state:
            return False
        return (
            self._state.get("isOnline", False)
            or self._state.get(F_DEVICE_STATE) == "online"
            or self.work_status not in (WORK_STATUS_OFFLINE, -1)
        )
    
    @property
    def mow_path(self) -> list[tuple[float, float]]:
        """ENU points accumulated during the current/last mowing session."""
        return []

    @property
    def state_dict(self) -> dict[str, Any]:
        """Compatibility alias used by helper entities."""
        return self._state
    
    def _normalize_fw_version(self, version: str | None) -> str | None:
        """Remove Lymow date suffix from firmware version.

        Examples:
        v2.1.48_beta_20260512 -> v2.1.48_beta
        v2.1.46_20260510      -> v2.1.46
        """
        if not version:
            return None

        return re.sub(r"_\d{8}$", "", str(version).strip())
    
    def _zone_name_by_id(self, zone_id: str) -> str | None:
        catalog = self._state.get("zone_catalog")
        zones = getattr(catalog, "zones", None)

        if isinstance(zones, list):
            for z in zones:
                if str(getattr(z, "hash_id", "")) == str(zone_id):
                    return getattr(z, "name", None)

        btmap = self._state.get("btMap") or {}
        zones = btmap.get("zones") if isinstance(btmap, dict) else []

        for z in zones or []:
            if str(z.get("hashId")) == str(zone_id):
                return z.get("name")

        return None

    def _merge_state(self, new_state: dict[str, Any]) -> None:
        """Merge MQTT/REST state without losing sticky subfields.

        Many PbOutput messages are partial. Dict submessages are merged so a
        packet carrying just one field does not wipe previously-known IP, map,
        RTK or configuration details. btMap.enuBasePoint is kept sticky because
        QUERY_PATH/QUERY_MAP can share the same branch.
        """
        for k, v in new_state.items():
            if isinstance(v, dict) and isinstance(self._state.get(k), dict):
                if k == "btMap":
                    old = self._state[k]
                    # Preserve ENU anchor and zone catalog when a partial path
                    # response arrives without the full map payload.
                    if "enuBasePoint" in old and "enuBasePoint" not in v:
                        v = {**v, "enuBasePoint": old["enuBasePoint"]}
                    if old.get("zones") and not v.get("zones"):
                        v = {**v, "zones": old["zones"]}
                    if old.get("zone_count") and not v.get("zone_count"):
                        v = {**v, "zone_count": old["zone_count"]}
                self._state[k].update(v)
            else:
                self._state[k] = v

        btmap = self._state.get("btMap") or {}
        if isinstance(btmap, dict) and isinstance(btmap.get("enuBasePoint"), dict):
            self._state["enu_base_point"] = btmap["enuBasePoint"]

    def _derive_state(self) -> None:
        """Derive convenience fields used by HA entities."""
        gps = robot_gps_from_state(self._state)
        if gps is not None:
            self._state["derivedLatitude"] = gps[0]
            self._state["derivedLongitude"] = gps[1]
            # Keep top-level aliases useful for sensors, but do not overwrite
            # explicit robotLlaCoords if the firmware ever emits them.
            self._state.setdefault("latitude", gps[0])
            self._state.setdefault("longitude", gps[1])

        # Current zone vs channel are mutually exclusive: the mower is in exactly
        # one at a time, so the OTHER reads "Clear". Channel wins when both match
        # (the transit corridor is the signal automations want). This gives clean
        # edges — e.g. channel Clear -> "Front Left Main <-> Backyard" = entered
        # the corridor (open a gate); back to Clear = left it.
        # Sticky: on a partial frame (no position/map) keep the last reading
        # rather than flickering to unknown. None when not mowing (parked).
        _docked = (
            self._state.get("robotStatus") in (WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL)
            or self._state.get("workStatus") in (WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL)
            or bool(self._state.get("isCharging"))
        )
        # Single source of truth: resolve WHERE the mower is once (No-Go > Zone > Channel >
        # Off-Map), then Location State + Current Zone + Current Channel all read from it so
        # they can never disagree (which was the unknown-flicker bug). Off-Map (in no zone,
        # no channel, while active) = the negative-space geofence breach — DEBOUNCED so GPS
        # jitter at a boundary can't blip a false breach.
        _loc = resolve_location(self._state, _docked)
        if _loc is not None:
            _label, _zone, _chan, _chan_info = _loc
            if _label == "Off-Map":
                now = time.monotonic()
                if self._offmap_since is None:
                    self._offmap_since = now
                if now - self._offmap_since >= LOCATION_OFFMAP_DEBOUNCE_S:
                    self._state["locationState"] = _label
                    self._state["currentZone"] = _zone
                    self._state["currentChannel"] = _chan
                    self._state["currentChannelInfo"] = _chan_info
                # else: within the debounce window — HOLD previous (don't commit a breach yet)
            else:
                self._offmap_since = None
                self._state["locationState"] = _label
                self._state["currentZone"] = _zone
                self._state["currentChannel"] = _chan
                self._state["currentChannelInfo"] = _chan_info
        else:
            # paused / idle / partial / no-fix — keep previous (sticky); not an active breach.
            self._offmap_since = None
        # Docking (returning) IS localization-active, so it re-resolves above even
        # after a cleanReport reset currentZone=None on task cancel.


        # Flatten runTimeConfig from btMap to top-level (blade height sensor)
        btmap = self._dict_or_empty(self._state.get("btMap"))
        for k in ("cutHeight", "cutSpeed", "moveSpeed"):
            if btmap.get(k) is not None and self._state.get(k) is None:
                self._state[k] = btmap[k]


    # ── Inbound MQTT handlers ────────────────────────────────────

    def _dict_or_empty(self, value):
        return value if isinstance(value, dict) else {}

    def _apply_coverage_results(self, zones, zone_stats, obstacle_events, pcov, do_obstacle) -> None:
        """Apply heavy-attribution results to state (runs on the loop). Mirrors the original
        inline tail: set zone_stats, then on an obstacle tick filter flags to flaggable zones."""
        if zone_stats is not None:
            self._state["zone_stats"] = zone_stats
        if do_obstacle:
            _obs2, pcov2 = self._filter_to_flaggable(
                obstacle_events, pcov, zones, self._state.get("zone_stats"))
            self._state["obstacle_events"] = _obs2
            if pcov2:
                self._state["pass_coverage"] = pcov2

    async def _async_compute_coverage(self, zones, gz, ng, xy, bp_xy, do_obstacle) -> None:
        """Run the heavy attribution in the subprocess, then apply results on the loop.
        Single-flight (see _cov_inflight) so a slow compute can't queue up behind itself."""
        try:
            loop = asyncio.get_running_loop()
            zone_stats, obstacle_events, pcov = await loop.run_in_executor(
                self._cov_executor, compute_coverage, zones, gz, ng, xy, bp_xy, do_obstacle)
            self._apply_coverage_results(zones, zone_stats, obstacle_events, pcov, do_obstacle)
            self.async_set_updated_data(self._state)
        except Exception:
            _LOGGER.debug("offloaded coverage compute failed for %s", self.thing_name, exc_info=True)
        finally:
            self._cov_inflight = False

    def _handle_pboutput(self, raw_envelope: bytes) -> None:
        """Decode one MQTT /pboutput packet and merge it into coordinator state.

        `protocol.decode_pboutput_envelope()` now returns a real protobuf
        `PbOutput`. Normal fields are merged in `state.merge_pboutput()`;
        the rich map/catalog branch inside `btMap.queryAck` is parsed
        separately because that inner blob is not fully exposed by the pb2
        schema.
        """
        self._last_mqtt_ts = time.monotonic()

        try:
            msg = decode_pboutput_envelope(raw_envelope)
        except Exception:
            _LOGGER.exception("Failed to decode PbOutput for %s", self.thing_name)
            return

        if msg is None:
            _LOGGER.debug("Empty pboutput decode for %s", self.thing_name)
            return

        try:
            merge_pboutput(self._state, msg)
        except Exception:
            _LOGGER.exception("Failed to merge PbOutput for %s", self.thing_name)
            return

        self._update_dwell_anomaly()                          # #5 stuck/spin detection
        if self._diag_capture and (time.monotonic() - self._diag_last_write) > 5.0:
            self._diag_last_write = time.monotonic()          # #4 throttled snapshot while ON
            self.hass.async_add_executor_job(self._write_diag_snapshot)

        # Debug: capture EVERY btMap response (size + flags) so we can see why
        # QUERY_PATH responses aren't hitting the path parse branch — does the
        # mower send a queryPath btMap at all, is it under the 200-byte gate, or
        # is the queryPath flag not echoed in the response?
        if self._pathcap_active():
            try:
                _bm = msg.btMap
                if _bm.ByteSize() > 0:
                    _rec = {
                        "type": "btmap", "bytes": _bm.ByteSize(),
                        "queryMap": getattr(_bm, "queryMap", None),
                        "queryPath": getattr(_bm, "queryPath", None),
                        "queryIndex": getattr(_bm, "queryIndex", None),
                    }
                    # For queryPath responses, dump RAW bytes (base64) + the full
                    # parse_query_path diagnostics regardless of points_count. The
                    # decoder returns 0 points on this firmware; the only way to fix
                    # the wire walk is to see the payload + where it stalls
                    # (queryAck_found / inner_found / inner_field_numbers).
                    if getattr(_bm, "queryPath", False):
                        import base64 as _b64
                        _rec["raw_b64"] = _b64.b64encode(_bm.SerializeToString()).decode("ascii")
                        try:
                            _pr = parse_query_path(_bm)
                            _rec["parse"] = {k: _pr.get(k) for k in (
                                "queryAck_found", "inner_found", "inner_bytes",
                                "inner_field_numbers", "raw_points_count",
                                "points_count", "error")}
                        except Exception as _e:
                            _rec["parse_err"] = f"{type(_e).__name__}: {_e}"
                    self._pathcap_record(_rec)
            except Exception:
                pass

        # QUERY_MAP rich response. PbOutput is decoded with protobuf, but the
        # nested btMap.queryAck map payload still needs manual wire parsing.
        try:
            # Path responses can be small (80–346B observed); the >200 floor only
            # guards against partial-MAP packets clobbering a complete catalog, so
            # apply it to queryMap only and let any queryPath response through.
            _bm_sz = msg.btMap.ByteSize()
            if (_bm_sz > 200) or (_bm_sz > 20 and getattr(msg.btMap, "queryPath", False)):
                if getattr(msg.btMap, "queryMap", False) and _bm_sz > 200:
                    catalog = parse_zone_catalog(msg.btMap)

                    # Persistent map: once we hold a COMPLETE catalog (zones +
                    # channels + GPS origin), never replace it with a less-complete
                    # packet. A partial query_map response (zones but missing
                    # channels/origin) was silently clobbering the good map
                    # mid-mow, killing current zone/channel. Only a complete update
                    # (or the Refresh Map button forcing a fresh query) replaces it.
                    existing = self._state.get("zone_catalog")
                    existing_complete = bool(
                        existing and getattr(existing, "zones", None)
                        and getattr(existing, "channels", None)
                        and getattr(existing, "enu_base_point", None)
                    )
                    new_complete = bool(
                        catalog.zones and catalog.channels and catalog.enu_base_point
                    )
                    if catalog.zones and (new_complete or not existing_complete):
                        self._state["zone_catalog"] = catalog
                        self._state["btMap"] = catalog.to_btmap_dict()
                        self._state["backupMapDownloadError"] = None
                        # #4: now that we have an AUTHORITATIVE full zone list, reconcile the
                        # persistent coverage masks (drop deleted zones / invalidate on RTK
                        # base relocation). Safe here — never on a partial telemetry frame.
                        self._reconcile_zone_coverage_map()
                        # One-shot: zones are now available and the breadcrumb trail was
                        # restored at setup, so compute pass-coverage once — the Double
                        # Coverage sensor / Pass Coverage layer then populate from the LAST
                        # mow on startup, instead of staying blank until the user re-selects
                        # the layer (a live mow recomputes it continuously via the path branch).
                        if (not self._pass_cov_bootstrapped
                                and len(self._state.get("breadcrumb_track") or []) >= 200):
                            self._pass_cov_bootstrapped = True
                            self.hass.async_create_task(self.async_refresh_pass_coverage())

                    if catalog.enu_base_point:
                        self._state["enu_base_point"] = catalog.enu_base_point

                    if catalog.charging_station_loc:
                        self._state["chargingStationLoc"] = catalog.charging_station_loc

                    if catalog.runtime_config:
                        self._state["runTimeConfig"] = catalog.runtime_config
                        for key in ("cutHeight", "cutSpeed", "moveSpeed"):
                            if key in catalog.runtime_config:
                                self._state[key] = catalog.runtime_config[key]

                    _LOGGER.debug(
                        "Parsed Lymow zone catalog for %s: zones=%s channels=%s noGO=%s ebp=%s runtime=%s",
                        self.thing_name,
                        len(catalog.zones),
                        len(catalog.channels),
                        len(catalog.nogo_zones),
                        bool(catalog.enu_base_point),
                        bool(catalog.runtime_config),
                    )
               # QUERY_PATH: traiettoria / coverage / path
                elif getattr(msg.btMap, "queryPath", False):
                    path = parse_query_path(msg.btMap)
                    # Breadcrumb: proves this branch is actually reached and shows the
                    # points_count the MAIN branch sees (the debug hook above parses
                    # fine, but no querypath/state ever materialised — find out why).
                    if self._pathcap_active():
                        self._pathcap_record({
                            "type": "pathdbg", "stage": "elif_path", "sz": _bm_sz,
                            "points": path.get("points_count"),
                            "raw_points": path.get("raw_points_count"),
                            "qack": path.get("queryAck_found"),
                            "ifields": path.get("inner_field_numbers"),
                        })

                    if path.get("points_count", 0) > 0:
                        segments = path.get("segments") or []

                        # Debug capture: log the RAW per-pull chunks (333 cut /
                        # 444 planned) with their full points + markers, plus the
                        # robot pose + msgId at pull time — the ground truth for
                        # solving the chunk-ordering/stitch decode offline.
                        if self._pathcap_active():
                            _pp = get_robot_pose(self._state) or {}
                            # `segments` are plain point-lists; the per-segment marker
                            # context (333/444) lives in path["segment_markers"], index
                            # aligned. (The old code called sg.get() on a list → an
                            # AttributeError that the outer handler swallowed, silently
                            # killing the whole path branch whenever debug was on.)
                            _smk = path.get("segment_markers") or []
                            self._pathcap_record({
                                "type": "querypath",
                                "msgId": self._state.get("msgId"),
                                "rx": _pp.get("x"), "ry": _pp.get("y"), "rth": _pp.get("theta"),
                                "marker_sequence": path.get("marker_sequence"),
                                "zone": self._state.get("currentZone") or self._state.get("current_zone"),
                                "enu_base_point": self._state.get("enu_base_point"),
                                "segments": self._capture_segments(segments, _smk),
                            })

                        # I marker 333/444 vengono già rimossi da parse_query_path().
                        # Ogni segmento valido con almeno 3 punti può essere disegnato come poligono.
                        mowed_polygons = [
                            seg for seg in segments
                            if isinstance(seg, list) and len(seg) >= 3
                        ]

                        self._state["mowed_area_polygons"] = mowed_polygons

                        # Delete old path
                        self._state.pop("planned_path", None)
                        self._state.pop("path_data", None)

                        # Baseline path_engine straight from the parsed path so the
                        # coverage/planned sensors publish REAL data immediately — the
                        # richer engine below (accumulation, deviations, zone stats)
                        # then refines it. Wrapped so an engine-side error can never
                        # block this basic publish again.
                        self._state.setdefault("path_engine", {})
                        self._state["path_engine"] = {
                            "planned_points": 0,
                            "obstacle_count": (self._state.get("path_engine") or {}).get("obstacle_count", 0),
                        }

                        # ── Path engine (Phase 1) ──────────────────────────────
                        # Split cut(actual) vs planned by STATICITY (not size): the
                        # planned route is identical pull-to-pull; the live cut delta
                        # changes. `accumulate` is True only when a stable planned
                        # route is present and nothing is still building/spiking — so a
                        # cut-delta spike past the size threshold can no longer be
                        # misread as planned (which used to inflate planned_points and
                        # wipe coverage to 0). Verified offline vs the 2026-06-03
                        # backyard run: 0 spurious resets, planned locked at 1131.
                        cut_segs, planned_segs, _large_fps, accumulate = classify_segments(
                            segments, self._prev_large_fps
                        )
                        self._prev_large_fps = _large_fps
                        # Activity phase from the REAL 333/444 markers (order-independent):
                        # whichever stream is actively ACCUMULATING this pull is what the mower
                        # is cutting right now. 444 = perimeter laps, 333 = main-area cut. The
                        # full path re-parses every pull, so the stream being cut is the one whose
                        # point-count grew. Robust to ANY order incl. main-first / perimeters-last.
                        _smk_act = path.get("segment_markers") or []
                        _perim_now = sum(m.get("n", 0) for m in _smk_act
                                         if 444 in (m.get("before"), m.get("after")))
                        _main_now = sum(m.get("n", 0) for m in _smk_act
                                        if 333 in (m.get("before"), m.get("after")))
                        _dmain = _main_now - self._main_pts_prev
                        _dperim = _perim_now - self._perim_pts_prev
                        self._main_pts_prev, self._perim_pts_prev = _main_now, _perim_now
                        if _dmain > 0 and _dmain >= _dperim:
                            self._activity_phase = "main"
                        elif _dperim > 0:
                            self._activity_phase = "perimeter"
                        # DIAGNOSTIC (for fixing traveled-path reconstruction): each
                        # QUERY_PATH cut segment is the recent path back-propagated
                        # from current position to the last update. Log each raw chunk's
                        # ordering — length, first/last points, and max consecutive step
                        # — so we can tell its direction (current→old vs old→current) and
                        # how chunks abut, then stitch them into the true ordered path
                        # instead of the scrambled accumulation that forced dot rendering.
                        for _ci, _seg in enumerate(cut_segs):
                            if len(_seg) >= 2:
                                _steps = [
                                    ((_seg[k][0] - _seg[k - 1][0]) ** 2 + (_seg[k][1] - _seg[k - 1][1]) ** 2) ** 0.5
                                    for k in range(1, len(_seg))
                                ]
                                _LOGGER.debug(
                                    "Lymow cut-chunk[%s] for %s: n=%s first=%s last=%s max_step=%.2f mean_step=%.2f",
                                    _ci, self.thing_name, len(_seg),
                                    [round(v, 2) for v in _seg[0]], [round(v, 2) for v in _seg[-1]],
                                    max(_steps), sum(_steps) / len(_steps),
                                )
                        planned_total = sum(len(s) for s in planned_segs)
                        self._last_planned_total = planned_total
                        # Debounce the Perimeter/Structural display: only adopt a value
                        # once it repeats across 2 consecutive pulls (truly frozen), and
                        # never let it fall back to 0 mid-mow. Removes the build-phase
                        # 0↔value flicker while still climbing across multiple laps.
                        if planned_total > 0 and planned_total == self._perim_prev:
                            self._perim_stable = planned_total
                        self._perim_prev = planned_total
                        # COVERAGE now comes from the breadcrumb pose-trail (the robot's
                        # actual position, sampled every frame): ~10x denser than the
                        # sparse, overlapping QUERY_PATH deltas, time-ordered (no chunk
                        # scramble), and inherently ORDER-INDEPENDENT — the perimeter may
                        # be mowed first OR last. The large STATIC QUERY_PATH segment is
                        # the PERIMETER / structural lap, not a precomputed plan (proven
                        # 2026-06-03 by a perimeter-last run where it appeared at 85%).
                        coverage_track = self._state.get("breadcrumb_track") or []
                        xy = [(p["x"], p["y"]) for p in coverage_track
                              if isinstance(p, dict) and "x" in p and "y" in p]
                        # Back-prop poses = LOW-CONFIDENCE coverage (sparse burst after a comms
                        # blackout) → passed to obstacle detection so sampling-gap holes there
                        # aren't misread as objects the mower drove around.
                        bp_xy = [(p["x"], p["y"]) for p in coverage_track
                                 if isinstance(p, dict) and p.get("conn") == "backprop"
                                 and "x" in p and "y" in p]
                        btm = self._state.get("btMap") or {}
                        zones = btm.get("zones") or []
                        # #4: maintain the PERSISTENT per-zone coverage masks (clear+rebuild a
                        # zone only when it's on the task list AND the actively-mowed zone).
                        # Stateful (mask lives on self._zone_coverage) → stays on-loop; it's
                        # incremental and cheap relative to the attribution below.
                        self._update_zone_coverage(zones, xy)
                        # HEAVY attribution: per-zone point-in-polygon (assign_to_zones) plus the
                        # throttled, zone-rasterising obstacle/pass-coverage scan. On a large lawn
                        # this is 0.5–1.8s of GIL-bound pure Python (see bench.py), so it's run in
                        # a subprocess to keep HA's event loop responsive. Single-flight: skip a
                        # tick rather than queue a second compute. Inline fallback when the
                        # subprocess is disabled/unavailable (preserves original behavior). [eve]
                        self._obstacle_scan += 1
                        do_obstacle = len(xy) >= 200 and self._obstacle_scan % 12 == 0
                        if zones and xy:
                            def _poly(z):
                                return [((p.get("x"), p.get("y")) if isinstance(p, dict) else (p[0], p[1]))
                                        for p in (z.get("points") or [])]
                            gz = [{"name": z.get("name"), "polygon": _poly(z),
                                   "double": (z.get("zoneConfig") or {}).get("cleanMode") == 3}
                                  for z in zones if z.get("points")]
                            ng = [_poly(z) for z in (btm.get("nogoZones") or []) if z.get("points")]
                            if self._cov_executor is not None:
                                if not self._cov_inflight:
                                    self._cov_inflight = True
                                    self.hass.async_create_task(
                                        self._async_compute_coverage(zones, gz, ng, xy, bp_xy, do_obstacle))
                                # else: a compute is still running — skip; next tick catches up.
                            else:
                                try:
                                    _zs, _ob, _pc = compute_coverage(zones, gz, ng, xy, bp_xy, do_obstacle)
                                    self._apply_coverage_results(zones, _zs, _ob, _pc, do_obstacle)
                                except Exception:
                                    _LOGGER.debug("inline coverage compute failed for %s", self.thing_name, exc_info=True)
                        obstacle_events = self._state.get("obstacle_events") or []
                        self._state["planned_path_segments"] = planned_segs
                        self._state["coverage_track"] = coverage_track
                        self._state["path_engine"] = {
                            "planned_points": self._perim_stable,    # perimeter/structural (debounced)
                            "obstacle_count": len(obstacle_events),
                        }
                        # Diagnostic: raw per-segment marker context (333/444), so the
                        # next mow reveals the true cut/planned semantics and we can
                        # replace the point-count classification heuristic.
                        self._state["segment_markers"] = path.get("segment_markers") or []
                        # Persist the cut track so a restart mid-mow keeps the history.
                        self.hass.async_create_task(
                            self._cut_store.async_save(self._cut_accumulator.to_dict())
                        )
                        # Persist the planned route (projected path) too, so completed
                        # runs stay fully re-analyzable (cut + planned), not cut-only.
                        if planned_total > 0:
                            self.hass.async_create_task(
                                self._planned_store.async_save({
                                    "session_key": self._mow_session_key,
                                    "planned_total": planned_total,
                                    "planned_segments": planned_segs,
                                })
                            )
                            # Plan-history: append THIS zone's plan when it changes (new zone
                            # or replan) so we don't lose it to the next overwrite. The fp
                            # (zone, total, segment-count) changes exactly when the plan does.
                            _zone = self._state.get("currentZone") or self._state.get("current_zone")
                            _fp = (_zone, planned_total, len(planned_segs))
                            if _fp != self._plan_hist_fp:
                                self._plan_hist_fp = _fp
                                try:
                                    import json as _pj
                                    _rec = {
                                        "t": time.time(),
                                        "zone": _zone,
                                        "session_key": self._mow_session_key,
                                        "enu_base_point": self._state.get("enu_base_point"),
                                        "planned_total": planned_total,
                                        "planned_segments": planned_segs,
                                    }
                                    _line = _pj.dumps(_rec, default=str) + "\n"
                                    self.hass.async_add_executor_job(
                                        self._append_jsonl, self._plan_hist_path, _line)
                                except Exception:
                                    _LOGGER.debug("plan-history append failed for %s",
                                                  self.thing_name, exc_info=True)

                        _LOGGER.debug(
                            "Parsed Lymow QUERY_PATH as mowed area for %s: polygons=%s points=%s markers=%s",
                            self.thing_name,
                            len(mowed_polygons),
                            path.get("points_count"),
                            path.get("marker_count"),
                        )
        except Exception as _btmap_exc:
            _LOGGER.exception("Failed to parse btMap zone catalog for %s", self.thing_name)
            # No live HA log on this box — also record the throw to the pathcap file
            # so the next mow pinpoints where the btMap/path branch died.
            if self._pathcap_active():
                try:
                    import traceback as _tb
                    self._pathcap_record({
                        "type": "pathdbg", "stage": "exception",
                        "err": f"{type(_btmap_exc).__name__}: {_btmap_exc}",
                        "tb": _tb.format_exc()[-600:],
                    })
                except Exception:
                    pass

        #Parse Schedule
        try:
            if msg.schedule.ByteSize() > 0:
                schedules = parse_schedules(msg.schedule)

                self._state["schedules"] = schedules
                self._state["schedules_data"] = {
                    "task_count": len(schedules),
                    "enabled_count": sum(1 for s in schedules if s.enabled),
                    "disabled_count": sum(1 for s in schedules if not s.enabled),
                    "tasks": [s.to_dict() for s in schedules],
                }
        except Exception:
            _LOGGER.exception("Failed to parse bpSchedule for %s", self.thing_name)

        #Parse Clean Report
        try:
            if msg.cleanReport.ByteSize() > 0:
                report = msg.cleanReport
                report_ts = int(report.cleanStartTime or 0)

                if report_ts and self._state.get("lastCleanReportTs") != report_ts:
                    # NOTE: do NOT reset currentZone here. A cleanReport fires when
                    # a task ends — including Cancel Task, which stops the mower in
                    # place. Zeroing the zone then showed a misleading "unknown"
                    # while the mower was physically still sitting in that zone.
                    # The sticky/Docked/derive model already reports the right
                    # state (last zone when stopped, re-derived on the return).
                    # mowEndType is a CLOSED 3-value enum (verified by decompiling
                    # the app's Hermes bundle, v3.0.7: MowEndType only defines
                    # MOW_END_NONE=0, MOW_END_100=1, MOW_END_USER_CANCEL=2). There
                    # is no rain or low-battery end code — a rain-out/low-batt dock
                    # produces no distinct mowEndType. So this table is complete;
                    # there are no higher codes to capture.
                    end_labels = {
                        0: "none",
                        1: "completed",
                        2: "cancelled",
                    }

                    event_data = {
                        "thing_name": self.thing_name,
                        "clean_start_time": report.cleanStartTime,
                        "start_time": (
                            datetime.fromtimestamp(report.cleanStartTime, UTC).isoformat()
                            if report.cleanStartTime
                            else None
                        ),
                        "clean_time": report.cleanInfo.cleanTime,
                        "duration_s": report.cleanInfo.cleanTime,
                        "clean_area": report.cleanInfo.cleanArea,
                        "area_m2": round(float(report.cleanInfo.cleanArea), 1)
                        if report.cleanInfo.cleanArea is not None
                        else None,
                        "clean_percent": report.cleanInfo.cleanPercent,
                        "mow_end_type": report.mowEndType,
                        "end_type": end_labels.get(report.mowEndType, "unknown"),
                        "used_battery": report.usedBattery,
                        "zones": list(report.cleanInfo.areaInfo.cleanZoneIds),
                        # cleanReport.errorList (proto field 4) = the errors that
                        # fired DURING this mow session, each with the clean-percent
                        # at which it occurred. Distinct from the live robotInfo
                        # error code (current fault) — this is the per-session
                        # history. Labelled via the shared ERROR_CODES table.
                        "errors": [
                            {
                                "code": e.code,
                                "label": error_label(e.code),
                                "percent": round(float(e.percent), 1)
                                if e.percent is not None
                                else None,
                            }
                            for e in report.errorList
                        ],
                        "error_count": len(report.errorList),
                    }

                    zone_history = self._state.setdefault("zone_history", {})

                    # NOTE: The Lymow cloud telemetry does NOT provide a per-zone
                    # breakdown of area/time/percent. PbCleanReport.cleanInfo carries
                    # a single SESSION-LEVEL summary (cleanTime/cleanArea/cleanPercent),
                    # and PbAreaInfo only lists which zone IDs were part of the task
                    # (cleanZoneIds) plus an areaOrGlobal flag. There is no message in
                    # the proto that attributes area/time/percent to an individual zone.
                    #
                    # Previously this loop copied the whole-session totals onto EVERY
                    # zone, which made each zone falsely report the entire session's
                    # 141m2 / 73s / 30%. That was fabricated per-zone data. We now only
                    # record what is actually true per zone (that it participated in
                    # this session, when it ran, and how the session ended) and keep the
                    # session totals in a clearly session-scoped block — NOT divided
                    # across zones.
                    num_zones = len(event_data["zones"])

                    for zone_id in event_data["zones"]:
                        zone_name = self._zone_name_by_id(str(zone_id))
                        # MERGE the cloud half into the single per-zone record (keyed by
                        # hashId). The breadcrumb half (_record_zone_history) fills the rest.
                        # NOTE: cleanTime is MINUTES of blade-down mowing (cloud), distinct
                        # from session_minutes (wall-clock incl. travel) added by the other
                        # writer. cleanArea is total blade coverage incl. chess overlap,
                        # distinct from zone_area_m2 (the zone's geometric area). The cloud
                        # never breaks these down per zone (per_zone_stats_available=False);
                        # on a multi-zone task they're the session total across all zones.
                        h = dict(zone_history.get(str(zone_id)) or {})
                        h.update({
                            "zone_id": str(zone_id),
                            "zone_name": zone_name or str(zone_id),
                            "end_type": event_data["end_type"],
                            "mowing_minutes": event_data["duration_s"],   # cloud cleanTime (minutes, session total)
                            "session_area_m2": event_data["area_m2"],     # cloud cleanArea (SESSION total, all zones, incl. overlap)
                            "session_clean_percent": event_data["clean_percent"],
                            "session_zone_count": num_zones,
                            "per_zone_stats_available": False,
                        })
                        # last_mowed + the lifetime mow_count are stamped ONLY when the task
                        # actually COMPLETED (mowEndType==1) — exactly like the coverage-mask
                        # snapshot below. A cancelled/rained task records the attempt (end_type +
                        # session stats) but the zone stays "due". This is the single, completion-
                        # gated owner of last_mowed/count; the breadcrumb half only enriches stats
                        # and must never stamp them. [Nate: only task-list zones AND only when each
                        # completed — don't falsely flag transited/unfinished zones]
                        if report.mowEndType == 1:
                            h["last_mowed"] = event_data["start_time"]
                            h["mow_count"] = (h.get("mow_count") or 0) + 1
                        zone_history[str(zone_id)] = h

                    self._state["lastCleanReport"] = report
                    self._state["lastCleanReportTs"] = report_ts
                    self._state["lastSessionEvent"] = event_data
                    self._state["lastSessionEventId"] = report_ts
                    # #4: authoritative per-zone coverage capture + last_mowed stamp.
                    # mowEndType: 1=completed (stamp), 2=cancelled / 0=unknown (keep mask,
                    # don't advance timestamp → zone stays "due").
                    self._snapshot_report_zones(
                        event_data["zones"],
                        completed=(report.mowEndType == 1),
                        ts=report_ts)

                    _LOGGER.info(
                        "Lymow session completed for %s: area=%s time=%s end_type=%s",
                        self.thing_name,
                        event_data["area_m2"],
                        event_data["duration_s"],
                        event_data["end_type"],
                    )

        except Exception:
            _LOGGER.exception("Failed to parse cleanReport for %s", self.thing_name)

        self._derive_state()

        # ── Live-position breadcrumb capture ───────────────────────────────
        # Append the robot's ENU pose (time-ordered) with telemetry on every
        # frame while actively driving. On the docked→mowing transition we start
        # a new mow session: bump the key (resets the track) and clear the held
        # annotations from the previous run. Fully guarded so it can never break
        # the main state flow.
        try:
            # Audio events are mode-transition / intent markers (mow start, charging
            # start, blade-stop on a clump/object, restart, slip) — capture on change
            # regardless of mowing state, as correlation points for path anomalies.
            aid = self._state.get("audioId")
            if isinstance(aid, int) and aid not in (0, 33) and aid != self._pathcap_last_audio:
                self._pathcap_last_audio = aid
                self._pathcap_record({
                    "type": "audio", "audioId": aid, "label": audio_label(aid),
                    "ws": self._state.get("workStatus"), "rs": self._state.get("robotStatus"),
                })
            active = _localization_active(self._state)
            if active and not self._was_active:
                self._mow_session_key += 1
                self._mow_start_ts = dt_util.utcnow()
                self._state.pop("obstacle_events", None)
                self._state.pop("pass_coverage", None)
                self.hass.async_create_task(self._annot_store.async_save({}))
                # Reset the CUT accumulator on a fresh mow too. It otherwise only
                # resets inside ingest() when the planned-route signature changes —
                # but ingest only fires once a planned route is present AND stable, so
                # a mow that never produces a stable plan (e.g. a tiny zone) would
                # show STALE coverage carried over from the previous run. Resetting on
                # the docked→mowing edge makes coverage reflect only the current mow.
                self._cut_accumulator.reset()
                self._last_planned_total = 0
                self._prev_large_fps = set()
                self._perim_stable = 0
                self._perim_prev = 0
                self._obstacle_scan = 0
                self._zone_visit = {}            # fresh per-zone time/battery tally
                self._zv_last_ts = None
                self._zv_travel_s = 0.0
                self._zv_mow_batt_prev = None
                self._zv_mow_zone = None
                self._state.pop("obstacle_events", None)
                self._state.pop("coverage_track", None)
                self._state.pop("zone_stats", None)
                # Also clear the rest of the PREVIOUS session's derived state — otherwise the
                # last-mowed zone keeps its red flags + its persisted perimeter laps draw as
                # ghost outlines (2-3 rings around the backyard) over the fresh map.
                self._state.pop("flaggable_zone_keys", None)      # stale red-flag gate
                self._state.pop("planned_path_segments", None)    # last zone's perimeter laps -> ghost outlines
                self._state.pop("mowed_area_polygons", None)      # last zone's drawn perimeter polygons
                self._state.pop("anomaly_events", None)           # last run's dwell/stuck markers
                self._dwell_cluster = []
                self._dwell_evidx = None
                self._prog_display = 0                             # reset session progress for the new mow
                self._state["session_percent_display"] = 0
                self._last_bc_wall = None                          # don't mistake the mow-start gap for a blackout
                self._backprop_until = 0.0
                self._activity_phase = "perimeter"
                self._main_pts_prev = 0
                self._perim_pts_prev = 0
                self.hass.async_create_task(
                    self._cut_store.async_save(self._cut_accumulator.to_dict())
                )
                # Also wipe the PERSISTED planned route so a restart mid-next-session can't
                # restore the old perimeter and redraw the ghost outlines.
                self.hass.async_create_task(self._planned_store.async_save({}))
            elif self._was_active and not active:
                # Mow just ended (active → idle/docked): stamp per-zone history while
                # this session's zone_stats are still fresh.
                self._record_zone_history()
            self._was_active = active
            # Persist the dock location from the mower's own pose while charging. The
            # mower doesn't reliably broadcast chargingStationLoc (it drops mid-mow), so
            # this derived dock survives and re-captures each time it docks — auto-tracking
            # a moved charging station. Camera prefers chargingStationLoc, falls back to this.
            charging_now = (
                bool(self._state.get(F_IS_CHARGING)) or bool(self._state.get("isRecharging"))
                or self._state.get("workStatus") in (WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL)
                or self._state.get("robotStatus") in (WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL)
            )
            if charging_now and not active:
                dp = get_robot_pose(self._state)
                if dp and dp.get("x") is not None and dp.get("y") is not None:
                    dock = {"x": round(float(dp["x"]), 2), "y": round(float(dp["y"]), 2)}
                    prev = self._state.get("derived_dock")
                    self._state["derived_dock"] = dock
                    if (self._config_entry and (not prev
                            or abs(prev.get("x", 0) - dock["x"]) > 0.5
                            or abs(prev.get("y", 0) - dock["y"]) > 0.5)):
                        self.hass.config_entries.async_update_entry(
                            self._config_entry,
                            data={**self._config_entry.data, "derived_dock": dock})
            if active:
                self._pathcap_record(self._pathcap_frame())
                self._accumulate_zone_visit()
                pose = get_robot_pose(self._state)
                if pose and pose.get("x") is not None and pose.get("y") is not None:
                    _ri = self._state.get("robotInfo")
                    _lora = self._state.get("rtkLoraBps")
                    _l2 = self._state.get("rtkDiagnosticL2")
                    _nd = self._state.get("netDetailInfo")

                    def _nd_get(k):
                        if isinstance(_nd, dict):
                            return _nd.get(k)
                        return getattr(_nd, k, None) if _nd is not None else None

                    # Read WiFi/cell the SAME way the WiFi/LTE sensors do (top-level merged
                    # field, then netDetailInfo) so the heatmap matches the sensors exactly.
                    # robotInfo.* alone was unreliable — older mows captured no wifi at all.
                    _wifi = (self._state.get("wifiSignalQuality")
                             or _nd_get("wifiSignal")
                             or (getattr(_ri, "wifiSignalQuality", None) if _ri is not None else None))
                    _cell = (self._state.get("lteSignalQuality")
                             or _nd_get("simSignal")
                             or (getattr(_ri, "lteSignalQuality", None) if _ri is not None else None))
                    # Correction Age: sensor reads top-level rtkDiffAge; the L2 object is a
                    # fallback only (mirror the sensor so the heatmap matches it).
                    _diffage = self._state.get("rtkDiffAge")
                    if _diffage is None and _l2 is not None:
                        _diffage = getattr(_l2, "diffAge", None)
                    # Connection type for this pose. A comms blackout ends with a RAPID BURST of
                    # buffered poses (dt << live cadence) — those are BACK-PROPAGATED, not live
                    # (verified 2026-06-07: live ~0.62s/frame, catch-up burst ~0.03s after a >20s
                    # blackout). Tag them so the Connection Type heatmap shows recovered vs live.
                    _now = time.time()
                    _dt = _now - (self._last_bc_wall or _now)
                    self._last_bc_wall = _now
                    if _dt > 20:                              # blackout ended -> catch-up burst follows
                        self._backprop_until = _now + 30      # tag rapid poses for the next ~30s
                    # A back-filled pose = arrives FAR faster than the mower can move (dt << live
                    # ~0.62s), within the post-blackout window. The window gates out RTK-jitter
                    # jumps (which come at live cadence), and tolerates brief pauses inside a burst.
                    _backprop = _dt < 0.3 and _now < (self._backprop_until or 0.0)
                    _conn = ("backprop" if _backprop
                             else "wifi" if self._state.get("wifiWorking")
                             else "cell" if self._state.get("lteWorking")
                             else None)
                    # Activity for this pose: in a channel or no zone = TRAVEL; otherwise the
                    # perimeter/main phase set by the 333/444 marker-growth logic in the
                    # QUERY_PATH handler (order-independent, so main-first mows tag correctly).
                    _zn = self._state.get("currentZone")
                    _ch = self._state.get("currentChannel")
                    if (_ch and _ch not in ("None", "Clear")) or _zn in (None, "None", "Docked", "Clear"):
                        _act = "travel"
                    else:
                        _act = self._activity_phase
                    tele = {
                        "gnss_conf": self._state.get("gnssConfidence"),
                        "pos_q": self._state.get("gnssPositionQuality"),
                        # RTK total (all-band) sat count — the value the app shows (~20)
                        # and the one with real spatial variation; numSatellites is flat ~10.
                        "sats": self._state.get("rtkSatellites"),
                        # Heatmap channels. RSSI is negative dBm (0 = unset). hAcc in metres.
                        "wifi": _wifi if _wifi else None,
                        "cell": _cell if _cell else None,
                        "lora": (sum(_lora) / len(_lora)) if isinstance(_lora, (list, tuple)) and _lora else None,
                        "rtk_snr": self._state.get("rtkL1Snr"),
                        "hacc": self._state.get("gnssHorizontalAccuracy"),
                        "diffage": _diffage,
                        "conn": _conn,   # wifi / cell / backprop (recovered burst after a blackout)
                        "act": _act,     # travel / perimeter / main (activity-state path coloring)
                    }
                    if self._breadcrumbs.append(
                        pose["x"], pose["y"], tele, session_key=self._mow_session_key
                    ):
                        self._state["breadcrumb_track"] = self._breadcrumbs.points
                        self._breadcrumb_save_counter += 1
                        if self._breadcrumb_save_counter % 15 == 0:
                            self.hass.async_create_task(
                                self._breadcrumb_store.async_save(self._breadcrumbs.to_dict())
                            )
        except Exception:
            _LOGGER.debug("breadcrumb capture skipped for %s", self.thing_name, exc_info=True)

        self._update_session_progress_display()

        _LOGGER.debug(
            "State update %s: workStatus=%s battery=%s catalog=%s outputCtrl=%s",
            self.thing_name,
            self._state.get("workStatus"),
            self._state.get("battery"),
            bool(self._state.get("zone_catalog")),
            self._state.get("outputCtrl"),
        )

        self._state_event.set()
        self.async_set_updated_data(self._state)
        self._persist_sticky_values()

    # ── Debug session recorder (path/chunk-decode capture) ──────────────
    def _msg2dict(self, obj):
        if obj is None or isinstance(obj, (int, float, str, bool, list)):
            return obj
        if isinstance(obj, dict):
            return obj
        if _pb_to_dict is not None:
            try:
                return _pb_to_dict(obj, preserving_proto_field_name=True)
            except Exception:
                return None
        return None

    def _capture_segments(self, segments: list, smk: list) -> list[dict]:
        """Build the per-pull segment records for the pathcap, with PERIMETER (444)
        de-dup. 333 (main-area cut delta) is logged with full points every pull — it's
        the new data. 444 (static perimeter) is logged with full points ONCE per
        fingerprint per session; recurrences just carry points_ref (proves it re-streamed
        unchanged without re-dumping ~556 pts × every pull → the old 84 MB bloat)."""
        out: list[dict] = []
        for i, seg in enumerate(segments):
            mb = smk[i].get("before") if i < len(smk) else None
            ma = smk[i].get("after") if i < len(smk) else None
            rec = {"mb": mb, "ma": ma, "n": len(seg)}
            if (mb == 444 or ma == 444) and len(seg) >= 2:
                fp = (len(seg), round(seg[0][0], 1), round(seg[0][1], 1),
                      round(seg[-1][0], 1), round(seg[-1][1], 1))
                if fp in self._pathcap_perim_fps:
                    rec["points_ref"] = list(fp)
                    out.append(rec)
                    continue
                self._pathcap_perim_fps.add(fp)
            rec["points"] = seg
            out.append(rec)
        return out

    def _pathcap_active(self) -> bool:
        """Capture is on when the user flips the #4 Diagnostic Capture switch (or the dev
        override constant). Drives every _pathcap_record + the record-building gates."""
        return DEBUG_PATHCAP_ENABLED or self._diag_capture

    def _pathcap_record(self, rec: dict) -> None:
        if not self._pathcap_active():
            return
        rec["t"] = round(time.time(), 3)
        self._pathcap_buf.append(rec)
        if len(self._pathcap_buf) >= 25:
            buf, self._pathcap_buf = self._pathcap_buf, []
            self.hass.async_add_executor_job(self._flush_pathcap, buf)

    def _flush_pathcap(self, buf: list[dict]) -> None:
        try:
            import os
            if os.path.exists(self._pathcap_path) and os.path.getsize(self._pathcap_path) > _PATHCAP_MAX_BYTES:
                # Hit the cap. Don't silently drop data forever (that's what bit us
                # 2026-06-04: the 80 MB cap stopped capture and nobody knew). Warn LOUDLY
                # and stamp a marker into the file the first time, so a truncated capture
                # is always obvious on analysis.
                if not self._pathcap_truncated:
                    self._pathcap_truncated = True
                    _LOGGER.warning(
                        "Lymow %s: pathcap hit the %d MB cap — capture TRUNCATED, further "
                        "records dropped. Toggle the Diagnostic Capture switch off/on for a "
                        "fresh file, or raise _PATHCAP_MAX_BYTES.",
                        self.thing_name, _PATHCAP_MAX_BYTES // (1024 * 1024))
                    try:
                        with open(self._pathcap_path, "a") as f:
                            f.write(json.dumps({"type": "capture_truncated",
                                                "cap_mb": _PATHCAP_MAX_BYTES // (1024 * 1024),
                                                "t": round(time.time(), 3)}) + "\n")
                    except Exception:
                        pass
                return
            with open(self._pathcap_path, "a") as f:
                for rec in buf:
                    f.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            _LOGGER.debug("pathcap flush failed", exc_info=True)

    def _pathcap_frame(self) -> dict:
        s = self._state
        pose = get_robot_pose(s) or {}
        return {
            "type": "frame",
            "ts": dt_util.utcnow().isoformat(),   # timestamp → enables per-zone duration / travel / battery analysis
            "msgId": s.get("msgId"),
            "ws": s.get("workStatus"), "rs": s.get("robotStatus"),
            "batt": s.get("battery"), "heading": s.get("mowerHeading"),
            "x": pose.get("x"), "y": pose.get("y"), "th": pose.get("theta"),
            "lat": s.get("derivedLatitude"), "lon": s.get("derivedLongitude"),
            "zone": s.get("current_zone") or s.get("currentZone"),
            "chan": s.get("current_channel") or s.get("currentChannel"),
            "rtkStatus": s.get("rtkStatus"), "rtkL1Snr": s.get("rtkL1Snr"),
            "rtkL2Snr": s.get("rtkL2Snr"), "loraBps": s.get("rtkLoraBps"),
            "gnssConf": s.get("gnssConfidence"), "camConf": s.get("cameraConfidence"),
            "rtkSats": s.get("rtkSatellites"), "posQ": s.get("gnssPositionQuality"),
            "hAcc": s.get("gnssHorizontalAccuracy"), "vAcc": s.get("gnssVerticalAccuracy"),
            "cleanPct": s.get("cleanPercent"), "cleanArea": s.get("cleanArea"),
            # Full nested telemetry blocks (best-effort; None if not present this frame)
            "rtkL1": self._msg2dict(s.get("rtkDiagnosticL1")),
            "rtkL2": self._msg2dict(s.get("rtkDiagnosticL2")),
            "robotInfo": self._msg2dict(s.get("robotInfo")),
            "netDetail": self._msg2dict(s.get("netDetailInfo")),
            "baseOutput": self._msg2dict(s.get("baseOutput")),
            "localization": self._msg2dict(s.get("localizationInfo")),
            # algoLocOutput = source of Camera/GNSS Confidence. Capturing it during a
            # mow tells us whether sensorConfidence is cloud-delivered or BLE-only.
            "algoLoc": self._msg2dict(s.get("algoLocOutput")),
            "camConf": s.get("cameraConfidence"),
            "gnssConf2": s.get("gnssConfidence"),
            # promptInfo results (mutateRet = Last Command Result, selfCheckingRet,
            # zoneRet). Both Last Command Result and Self Check have never populated;
            # capture whatever the mower actually puts here to confirm dead vs rare.
            "mutateResult": s.get("mutateResult"),
            "selfCheck": s.get("selfCheck"),
            "zoneResult": s.get("zoneResult"),
        }

    async def async_set_coverage_style(self, style: str) -> None:
        """Set the coverage map render style (local UI preference, persisted)."""
        await self.async_set_ui_pref("coverage_style", style)

    def _accumulate_zone_visit(self) -> None:
        """Per-frame: add elapsed time to the CURRENT zone's mow-only tally, but only while
        actively MOWING inside a real zone. Transit (channel), a mid-mow recharge, docking,
        or a No-Go intrusion don't accrue — so a zone's timer pauses across a charge and
        resumes when mowing continues. Also tracks battery in/out per zone + session travel."""
        now = dt_util.utcnow()
        last = self._zv_last_ts
        self._zv_last_ts = now
        if last is None:
            return
        dt = (now - last).total_seconds()
        if dt <= 0 or dt > 60:          # ignore restarts / long stalls
            return
        cz = self._state.get("currentZone")
        batt = self._state.get("battery")
        mowing = (self._state.get("workStatus") in MOWING_STATUSES
                  or self._state.get("robotStatus") in MOWING_STATUSES)
        real_zone = isinstance(cz, str) and cz and not cz.startswith("No Go:")
        if mowing and real_zone:
            v = self._zone_visit.setdefault(cz, {"mow_seconds": 0.0, "batt_used": 0.0})
            v["mow_seconds"] += dt
            # Battery drain attributed per-zone, only across CONSECUTIVE mowing frames in the
            # SAME zone — so drain while mowing ANOTHER zone (or charging/transit) never leaks
            # in. (The old batt_in..batt_last span double-counted other zones' drain when a
            # zone was visited early and late — Backyard read 85% on an 84% total run.)
            if batt is not None:
                if (self._zv_mow_zone == cz and self._zv_mow_batt_prev is not None
                        and batt < self._zv_mow_batt_prev):
                    v["batt_used"] += self._zv_mow_batt_prev - batt
                self._zv_mow_batt_prev = batt
                self._zv_mow_zone = cz
        else:
            self._zv_travel_s += dt          # transit / charge / dock / pause
            self._zv_mow_batt_prev = None    # break battery continuity across non-mowing

    def _record_zone_history(self) -> None:
        """On mow-end, stamp persistent per-zone history (last_mowed / count / minutes /
        area) from this session's zone_stats, and save it to the config entry."""
        # Guard: only record a session that actually started and ran long enough —
        # _localization_active can flicker on transient pauses, which would over-count.
        if not self._mow_start_ts:
            return
        now = dt_util.utcnow()
        dur_min = round((now - self._mow_start_ts).total_seconds() / 60.0, 1)
        self._mow_start_ts = None   # one record per started session
        stats = self._state.get("zone_stats") or {}
        if dur_min < 1.0 or not stats:
            return
        hist = dict(self._state.get("zone_history") or {})

        def _xy(p):
            return (p.get("x"), p.get("y")) if isinstance(p, dict) else (p[0], p[1])

        def _area(pts):
            n = len(pts)
            if n < 3:
                return 0.0
            a = 0.0
            for i in range(n):
                x1, y1 = _xy(pts[i]); x2, y2 = _xy(pts[(i + 1) % n])
                a += x1 * y2 - x2 * y1
            return abs(a) / 2.0

        zones = (self._state.get("btMap") or {}).get("zones") or []
        areas = {z.get("name"): round(_area(z.get("points") or []), 1)
                 for z in zones if z.get("points")}
        # Per-zone path spacing (cm, from the mower's zoneConfig) + cut-overlap % vs the 16 in
        # (40.64 cm) blade: positive = overlap (full coverage), negative = a gap between passes.
        spacings = {z.get("name"): (z.get("zoneConfig") or {}).get("pathSpacing")
                    for z in zones if z.get("points")}
        changed = False
        # Only record zones ON THE ACTIVE TASK (cleanZoneIds). Driving THROUGH a zone to reach
        # the task zone racks up >30 transit points and was being falsely recorded as a mow —
        # e.g. mow Front Left Strip, but the transit bumps Front Left Main + Backyard
        # (last_mowed, mow_count). Matches the coverage-mask transit-proofing. Empty/unknown
        # task list → fall back to coverage-only. [Nate caught this 2026-06-22]
        task_ids = set(self._state.get("cleanZoneIds") or [])
        for zid, s in stats.items():   # zid = zone hashId — SAME key the cloud writer uses,
            cov = s.get("coverage_points", 0)   # so the two halves merge into one record
            if cov < 30:   # ignore trivial coverage (transit through a zone)
                continue
            if task_ids and zid not in task_ids:
                continue   # only transited this zone to reach the task — not a mow of it
            name = s.get("name") or zid
            h = dict(hist.get(zid) or {})
            h["zone_id"] = zid
            h["zone_name"] = name
            # last_mowed + mow_count are owned by the cloud half (cleanReport), gated on
            # completion (mowEndType==1). This half ONLY enriches the per-zone stats below —
            # it must NEVER stamp last_mowed/count, else a transited or unfinished zone would be
            # falsely recorded as mowed. [Nate 2026-06-22]
            # PER-SESSION metrics (match the last_mowed timestamp; lifetime = mow_count only):
            # session_minutes = THIS mow's wall-clock undock→dock (incl. travel), distinct from
            # mowing_minutes (cloud blade-down session total) merged in by the cleanReport half.
            h["session_minutes"] = dur_min
            h["coverage_points"] = cov
            if name in areas:
                h["zone_area_m2"] = areas[name]   # geometric zone area
            _sp = spacings.get(name)
            if _sp:
                # Expose BOTH units so metric users aren't stuck reading inches. pathSpacing is
                # native cm; path_spacing_in kept for back-compat (existing dashboards). [Nate]
                h["path_spacing_cm"] = round(_sp, 1)
                h["path_spacing_in"] = round(_sp / 2.54, 1)              # cm -> in
                h["cut_overlap_pct"] = round(100.0 * (40.64 - _sp) / 40.64)  # vs 16 in cut (unit-agnostic %)
            # area_covered_m2 = THIS zone's real mowed footprint (rasterised pose-trail), NOT
            # the cloud session total (which the cleanReport half now stores as session_area_m2).
            if s.get("covered_m2") is not None:
                cov_m2 = s["covered_m2"]
                if areas.get(name):
                    cov_m2 = min(cov_m2, areas[name])   # can't cover more ground than the zone holds
                h["area_covered_m2"] = cov_m2
            # TRUE per-zone metrics from the visit accumulator (mow-only time, battery) —
            # this is the per-zone breakdown the cloud never gives, so flip the flag.
            visit = self._zone_visit.get(name) or {}
            if visit.get("mow_seconds"):
                h["mowing_minutes_derived"] = round(visit["mow_seconds"] / 60.0, 1)
                h["per_zone_stats_available"] = True
            if visit.get("batt_used") is not None:
                h["battery_used_pct"] = round(visit["batt_used"], 1)   # per-zone-continuous drain
            hist[zid] = h
            changed = True
        if self._zv_travel_s:
            self._state["last_session_travel_minutes"] = round(self._zv_travel_s / 60.0, 1)
        if changed:
            self._state["zone_history"] = hist
            if self._config_entry:
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data={**self._config_entry.data, "zone_history": hist},
                )
            self.async_set_updated_data(self._state)

    def _update_session_progress_display(self) -> None:
        """Mow progress for display (-> session_percent_display), derived from OUR coverage:
        the % of the active task's zone-AREA we've actually covered. This is robust to comms
        gaps the cloud cleanPercent misses — the cloud loses track during WiFi/RTK dropouts
        and undercounts (2026-06-07: a fully-finished 2-zone mow read the cloud's 73%), but
        our breadcrumb (+ back-prop) coverage knows both zones got mowed (read 100%). Falls
        back to the cloud cleanPercent only when we lack zone geometry/coverage. Monotonic
        within a session; a new task clears zone_stats so coverage drops to ~0 and resets.
        """
        task_ids = set(self._state.get("cleanZoneIds") or [])
        if not task_ids:
            return                                         # no active task -> HOLD the last value
        #                                                    (reset to 0 happens on the new-mow edge)
        zones = (self._state.get("btMap") or {}).get("zones") or []
        zstats = self._state.get("zone_stats") or {}
        total = covered = 0.0
        for z in zones:
            if z.get("hashId") not in task_ids:
                continue
            pts = z.get("points") or []
            if len(pts) < 3:
                continue
            poly = [((p.get("x"), p.get("y")) if isinstance(p, dict) else (p[0], p[1])) for p in pts]
            area = polygon_area(poly)
            if area <= 0:
                continue
            # Chess / double-pass zones (zoneConfig.cleanMode == 3) need TWO crosshatch passes,
            # so they count as 2x the work — otherwise progress hits 100% after the first pass.
            passes = 2 if (z.get("zoneConfig") or {}).get("cleanMode") == 3 else 1
            total += area * passes
            covered += min((zstats.get(z.get("hashId")) or {}).get("covered_m2") or 0.0, area)
        if total <= 0:
            return                                         # no geometry/coverage yet -> hold
        # Add the double-covered area (the 2nd pass): double_pct = fraction of the mowed area
        # covered twice (pass-coverage analysis). So a double-pass zone reads ~50% at
        # first-pass-done, then climbs to 100% as the crosshatch fills in. Single-pass tasks
        # have ~0 double coverage, so this is a no-op for them.
        double_frac = ((self._state.get("pass_coverage") or {}).get("double_pct") or 0.0) / 100.0
        covered_work = covered * (1.0 + double_frac)
        pct = int(round(min(covered_work, total) / total * 100))
        prev = getattr(self, "_prog_display", None)
        if prev is None:
            prev = self._state.get("session_percent_display") or 0
        disp = max(0, min(100, max(pct, prev)))            # monotonic; reset to 0 on the new-mow edge
        self._prog_display = disp
        self._state["session_percent_display"] = disp

    async def async_set_ui_pref(self, key: str, value) -> None:
        """Set a local UI preference (map_layer, heatmap_style, …), persisted via sticky."""
        self._state[key] = value
        self._persist_sticky_values()
        self.async_set_updated_data(self._state)
        # Selecting Pass Coverage when idle: analyse the LAST mow's restored trail so the
        # give-up rings show immediately instead of waiting for a live mow.
        if key == "map_layer" and value == "Pass Coverage":
            self.hass.async_create_task(self.async_refresh_pass_coverage())

    def _update_zone_coverage(self, zones: list, xy: list) -> None:
        """#4: maintain the persistent per-zone coverage masks during a mow.

        A zone goes 'live' (old mask cleared copy-on-write, then rebuilt from this
        session) ONLY when it is on the task list (cleanZoneIds) AND is the actively-mowed
        current zone — so transiting an unselected zone never wipes it. Zones not mowed
        this session keep their stored mask. The authoritative final capture + last_mowed
        stamp happens at cleanReport (_snapshot_report_zones)."""
        if not zones:
            return
        enu_base = self._state.get("enu_base_point")
        # NOTE: map reconciliation (drop deleted zones / invalidate on base relocation) is
        # done at the authoritative catalog parse (_reconcile_zone_coverage_map), NOT here —
        # a transiently partial telemetry frame must never delete a good mask.
        self._zone_coverage.begin_session(self._mow_session_key)

        # The actively-mowed zone (sticky current zone) that's on a KNOWN task list → live.
        cur = self._state.get("currentZone")
        cur = cur if (cur and cur not in ("Docked", "None")) else None
        task_ids = set(self._state.get("cleanZoneIds") or [])
        if cur and task_ids:                       # require a known task list (transit-safe)
            for z in zones:
                if z.get("name") == cur and z.get("points") and z.get("hashId") in task_ids:
                    self._zone_coverage.note_active(
                        z.get("hashId") or z.get("name"), z.get("name"),
                        self._mow_session_key, enu_base)
                    break

        # Rebuild live zones' masks from the current breadcrumb trail.
        live = self._zone_coverage.live
        if live and xy:
            cells_by_key = {}
            for z in zones:
                key = z.get("hashId") or z.get("name")
                if key in live and z.get("points"):
                    poly = [((p.get("x"), p.get("y")) if isinstance(p, dict) else (p[0], p[1]))
                            for p in z["points"]]
                    cells_by_key[key] = cells_for_points(xy, poly)
            if cells_by_key:
                self._zone_coverage.update_live(cells_by_key)

        self._state["zone_last_mowed"] = self._zone_coverage.last_mowed_map()
        # Rebuild the (heavier) mask render only while something is actively changing.
        if live or "zone_coverage_history" not in self._state:
            self._state["zone_coverage_history"] = self._zone_coverage.render_masks()
        # Checkpoint debounced (Store.async_save is non-blocking).
        self._zonecov_save_counter += 1
        if live and self._zonecov_save_counter % 12 == 0:
            self.hass.async_create_task(
                self._zonecov_store.async_save(self._zone_coverage.to_dict()))

    def _reconcile_zone_coverage_map(self) -> None:
        """#4: align persistent masks to an AUTHORITATIVE full zone catalog — drop masks
        for zones removed from the map, and invalidate masks captured against a different
        RTK base (their world coords no longer line up). Call ONLY from the catalog parse."""
        zones = (self._state.get("btMap") or {}).get("zones") or []
        if not zones:
            return
        self._zone_coverage.invalidate_on_base_change(self._state.get("enu_base_point"))
        self._zone_coverage.drop_zones(
            {(z.get("hashId") or z.get("name")) for z in zones if z.get("points")})
        self._state["zone_last_mowed"] = self._zone_coverage.last_mowed_map()
        self._state["zone_coverage_history"] = self._zone_coverage.render_masks()

    def _snapshot_report_zones(self, zone_ids: list, completed: bool, ts: float) -> None:
        """#4: authoritative per-zone mask capture at cleanReport. For each zone in the
        report, rebuild its mask from the final breadcrumb trail — but only OVERWRITE when
        the zone was substantially covered (≥10% of area), so a cancel/transit doesn't wipe
        a good prior mask. Stamp last_mowed only on genuine completion."""
        if not zone_ids:
            return
        zones = (self._state.get("btMap") or {}).get("zones") or []
        if not zones:
            return
        xy = [(p["x"], p["y"]) for p in (self._state.get("breadcrumb_track") or [])
              if isinstance(p, dict) and "x" in p and "y" in p]
        enu_base = self._state.get("enu_base_point")
        byid = {(z.get("hashId") or z.get("name")): z for z in zones if z.get("points")}
        cell2 = self._zone_coverage.cell_m ** 2
        completed_keys = []
        for zid in zone_ids:
            z = byid.get(zid)
            if not z:
                continue
            key = z.get("hashId") or z.get("name")
            poly = [((p.get("x"), p.get("y")) if isinstance(p, dict) else (p[0], p[1]))
                    for p in z["points"]]
            cells = cells_for_points(xy, poly)
            area = polygon_area(poly)
            if area > 0 and len(cells) * cell2 >= 0.10 * area:
                # note_active is a no-op if the live path already cleared it this session;
                # update_live then writes the final mask either way.
                self._zone_coverage.note_active(key, z.get("name"), self._mow_session_key, enu_base)
                self._zone_coverage.update_live({key: cells})
            if completed:
                completed_keys.append(key)
        if completed_keys:
            self._zone_coverage.mark_completed(completed_keys, ts)
        self._state["zone_last_mowed"] = self._zone_coverage.last_mowed_map()
        self._state["zone_coverage_history"] = self._zone_coverage.render_masks()
        self.hass.async_create_task(
            self._zonecov_store.async_save(self._zone_coverage.to_dict()))

    def _flaggable_zones(self, zones, zone_stats):
        """Zones eligible for flagging (obstacles / missed / single-pass / red background).

        A zone qualifies only if ALL hold:
          (a) it's on the active task list — hashId in cleanZoneIds (or we have no task list);
          (b) it's NOT the zone being mowed RIGHT NOW (still in progress);
          (c) it's substantially mowed — covered footprint / area >= COMPLETE_FRAC, so a zone
              that's half-done (a mid-mow recharge) or merely TRANSITED on the way elsewhere
              isn't judged on ground it never tried to cut.
        Returns (list_of_polygons, set_of_zone_keys). This single gate replaces the old
        current-zone-only suppression and covers every premature-flag case + transit zones.
        """
        task_ids = set(self._state.get("cleanZoneIds") or [])
        cur = self._state.get("currentZone")
        cur = cur if (cur and cur not in ("Docked", "None")) else None
        zstats = zone_stats or {}
        # FAIL CLOSED: flagging requires POSITIVE evidence a zone is substantially mowed.
        # With no coverage computed yet (just left the dock, or right after a restart) zstats
        # is empty and zone areas/coverage read 0 — flag NOTHING in that state, otherwise the
        # old inverted check (area>0 AND cov/area<FRAC) fell through to FLAG and turned every
        # zone red the moment the mower left the dock.
        if not zstats:
            return [], set()
        polys, keys = [], set()
        for z in zones:
            pts = z.get("points")
            if not pts:
                continue
            hid = z.get("hashId"); name = z.get("name")
            if task_ids and hid not in task_ids:
                continue                                  # not on the menu this session
            if cur and name == cur:
                continue                                  # actively mowing it
            poly = [((p.get("x"), p.get("y")) if isinstance(p, dict) else (p[0], p[1])) for p in pts]
            area = polygon_area(poly)
            key = hid or name
            cov = (zstats.get(key) or {}).get("covered_m2") or 0.0
            # Only flag a zone we have real evidence is substantially mowed. area<=0 (no
            # geometry yet) or sparse coverage => not enough to judge => leave it alone.
            if area <= 0 or cov < COMPLETE_FRAC * area:
                continue
            polys.append(poly); keys.add(key)
        return polys, keys

    def _filter_to_flaggable(self, obstacles, pcov, zones, zone_stats):
        """Keep obstacle/missed/single flags ONLY inside flaggable zones; recompute pcov counts.
        Records state['flaggable_zone_keys'] so the camera red-background gate stays consistent."""
        try:
            polys, keys = self._flaggable_zones(zones, zone_stats)
            self._state["flaggable_zone_keys"] = list(keys)

            # Transit corridors: a flag inside (or hugging) a channel is the mower driving THROUGH
            # without laying a swath — e.g. the dock-approach neck — not a real miss/obstacle. Drop it.
            chans = []
            for ch in ((self._state.get("btMap") or {}).get("channels") or []):
                cp = [((p.get("x"), p.get("y")) if isinstance(p, dict) else (p[0], p[1]))
                      for p in (ch.get("points") or [])]
                if len(cp) >= 3:
                    chans.append(cp)
            CH_BUF = 0.7   # m — also catch the sliver just inside the zone at the channel mouth

            def _seg_d(x, y, ax, ay, bx, by):
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy
                t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
                return ((x - (ax + t * dx)) ** 2 + (y - (ay + t * dy)) ** 2) ** 0.5

            def _in_channel(c):
                if not c:
                    return False
                for cp in chans:
                    if point_in_polygon(c[0], c[1], cp):
                        return True
                    n = len(cp)
                    if any(_seg_d(c[0], c[1], cp[i][0], cp[i][1], cp[(i + 1) % n][0], cp[(i + 1) % n][1]) <= CH_BUF
                           for i in range(n)):
                        return True
                return False

            def _in(c):
                return bool(c) and any(point_in_polygon(c[0], c[1], p) for p in polys) and not _in_channel(c)

            obstacles = [o for o in (obstacles or []) if _in(o.get("center"))]
            if pcov:
                cl = [c for c in (pcov.get("clusters") or []) if _in(c.get("center"))]
                pcov = dict(pcov)
                pcov["clusters"] = cl
                pcov["missed_count"] = sum(1 for c in cl if c.get("kind") == "missed")
                pcov["missed_m2"] = round(
                    sum(c.get("area_m2") or 0 for c in cl if c.get("kind") == "missed"), 2)
            return obstacles, pcov
        except Exception:
            return obstacles, pcov

    @staticmethod
    def _classify_dwell(dur, turns, path):
        """Classify a confirmed dwell by movement (path) + rotation (turns). None = disregard."""
        if dur < DWELL_DISREGARD_S and turns < DWELL_DISREGARD_TURNS:
            return None
        if turns >= DWELL_SPIN_TURNS:
            return "spin"
        if turns > DWELL_EXCESS_TURNS:
            return "excess-turn"
        if path >= DWELL_JITTER_PATH_M:
            return "jitter"
        if path >= DWELL_STRUGGLE_PATH_M:
            return "struggle"
        return "paused"

    def _zone_at(self, x, y) -> str:
        for z in ((self._state.get("btMap") or {}).get("zones") or []):
            pts = z.get("points")
            if pts and len(pts) >= 3:
                poly = [((p.get("x"), p.get("y")) if isinstance(p, dict) else (p[0], p[1])) for p in pts]
                if point_in_polygon(x, y, poly):
                    return z.get("name") or "zone"
        return "channel/transit"

    def _update_dwell_anomaly(self) -> None:
        """Detect a 'stuck spot' WHILE MOWING: a tight cluster of poses held within DWELL_RADIUS_M.
        Once it lasts DWELL_TIME_S it's classified by path + turns (spin / jitter / struggle /
        excess-turn / paused, or disregarded) and stored in state['anomaly_events'] with
        center/kind/duration/path/turns/zone — updated in place as the dwell grows, finalised when
        the mower moves on. All tunables in map_tuning."""
        try:
            import math as _m
            st = self._state
            mowing = (st.get("workStatus") in MOWING_STATUSES
                      or st.get("robotStatus") in MOWING_STATUSES)
            pose = st.get("pose") or {}
            x, y, th = pose.get("x"), pose.get("y"), pose.get("theta")
            if not (mowing and x is not None and y is not None and th is not None):
                self._dwell_cluster = []
                self._dwell_evidx = None
                return
            now = time.monotonic(); x, y, th = float(x), float(y), float(th)
            cl = self._dwell_cluster
            test = cl + [(x, y, th, now)]
            cx = sum(p[0] for p in test) / len(test); cy = sum(p[1] for p in test) / len(test)
            if cl and max(_m.hypot(p[0] - cx, p[1] - cy) for p in test) >= DWELL_RADIUS_M:
                self._dwell_cluster = [(x, y, th, now)]      # broke tight cluster → mower moved on
                self._dwell_evidx = None
                return
            cl.append((x, y, th, now)); self._dwell_cluster = cl
            if len(cl) < 8 or (cl[-1][3] - cl[0][3]) < DWELL_TIME_S:
                return
            dur = cl[-1][3] - cl[0][3]
            cx = sum(p[0] for p in cl) / len(cl); cy = sum(p[1] for p in cl) / len(cl)
            path = sum(_m.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(cl, cl[1:]))
            net = 0.0
            for a, b in zip(cl, cl[1:]):
                d = b[2] - a[2]
                while d > _m.pi: d -= 2 * _m.pi
                while d < -_m.pi: d += 2 * _m.pi
                net += d
            turns = abs(_m.degrees(net)) / 360.0
            kind = self._classify_dwell(dur, turns, path)
            if kind is None:
                return
            ev = {"center": [round(cx, 2), round(cy, 2)], "kind": kind,
                  "duration_s": int(dur), "path_m": round(path, 1), "turns": round(turns, 1),
                  "zone": self._zone_at(cx, cy), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
            evs = st.get("anomaly_events") or []
            if self._dwell_evidx is not None and 0 <= self._dwell_evidx < len(evs):
                evs[self._dwell_evidx] = ev                  # escalate/refresh the ongoing event
            else:
                evs.append(ev); evs = evs[-30:]
                self._dwell_evidx = len(evs) - 1
                _LOGGER.info("Lymow %s anomaly (%s) at %s", self.thing_name, kind, ev["center"])
            st["anomaly_events"] = evs
        except Exception:
            _LOGGER.debug("dwell anomaly check failed for %s", self.thing_name, exc_info=True)

    def set_diag_capture(self, on: bool) -> None:
        """Toggle Diagnostic Capture (#4). While ON we record EVERYTHING needed to validate
        the path model offline:
          • lymow_diagnostic_<thing>.json — render-input snapshot, refreshed every ~5 s.
          • lymow_pathcap_<thing>.jsonl — the full per-pull raw stream: every QUERY_PATH
            response's 333 (main-area cut) + 444 (perimeter cut) segments with points +
            markers, plus pose + zone + ENU base, plus per-frame telemetry. 444 is de-duped
            per session (logged once per fingerprint) so a multi-zone mow stays small.
        Flip ON before a mow, OFF when docked, then pull the .jsonl for analysis."""
        was = self._diag_capture
        self._diag_capture = bool(on)
        if self._diag_capture and not was:
            # Fresh capture session: clear perimeter de-dup memory and rotate any old
            # pathcap aside so this mow's file is clean and self-contained.
            self._pathcap_perim_fps = set()
            self._pathcap_truncated = False
            try:
                import os
                if os.path.exists(self._pathcap_path):
                    os.replace(self._pathcap_path, self._pathcap_path + ".prev")
            except Exception:
                _LOGGER.debug("pathcap rotate failed", exc_info=True)
            self._pathcap_record({"type": "capture_start", "thing": self.thing_name})
            self.hass.async_add_executor_job(self._write_diag_snapshot)
            _LOGGER.info("Lymow %s: Diagnostic Capture ON → %s", self.thing_name, self._pathcap_path)
        elif was and not self._diag_capture:
            # Flush whatever's buffered so the file is complete the moment it's turned off.
            # Append the stop marker directly (the gate is now closed, so _pathcap_record
            # would drop it).
            self._pathcap_buf.append({"type": "capture_stop", "thing": self.thing_name,
                                      "t": round(time.time(), 3)})
            buf, self._pathcap_buf = self._pathcap_buf, []
            self.hass.async_add_executor_job(self._flush_pathcap, buf)
            _LOGGER.info("Lymow %s: Diagnostic Capture OFF (flushed)", self.thing_name)
        self.async_set_updated_data(self._state)

    def _append_jsonl(self, path: str, line: str) -> None:
        """Append one pre-serialized line to a file. Runs in the executor — the caller
        builds the line on the loop, the blocking write happens off it."""
        with open(path, "a") as f:
            f.write(line)

    def _write_diag_snapshot(self) -> None:
        """Write the JSON-safe render-input state so a map issue can be reproduced offline.
        Always invoke via async_add_executor_job — it opens a file (blocking I/O)."""
        try:
            import json as _j
            keys = ("btMap", "breadcrumb_track", "coverage_track", "planned_path_segments",
                    "segment_markers",  # raw 333/444 per-segment context — lets us re-verify
                                        # the 444=planned / 333=cut marker mapping on any run
                    "pass_coverage", "obstacle_events", "zone_stats", "flaggable_zone_keys",
                    "anomaly_events", "map_layer", "coverage_style", "heatmap_style", "mower_size",
                    "currentZone", "currentChannel", "cleanZoneIds", "auto_recharge_battery",
                    "battery", "workStatus", "robotStatus", "latitude", "longitude", "pose",
                    "enu_base_point", "session_percent_display", "channel_buffer_m")
            snap = {"_captured_at": time.strftime("%Y-%m-%d %H:%M:%S"), "_thing": self.thing_name}
            for k in keys:
                v = self._state.get(k)
                try:
                    _j.dumps(v)
                    snap[k] = v
                except (TypeError, ValueError):
                    pass
            with open(self._diag_path, "w") as f:
                _j.dump(snap, f)
        except Exception:
            _LOGGER.debug("diag snapshot write failed for %s", self.thing_name, exc_info=True)

    async def async_refresh_pass_coverage(self) -> None:
        """Compute pass-coverage from the current breadcrumb trail (works when idle)."""
        try:
            bc = self._state.get("breadcrumb_track") or []
            xy = [(p["x"], p["y"]) for p in bc if isinstance(p, dict) and "x" in p and "y" in p]
            btm = self._state.get("btMap") or {}
            zones = btm.get("zones") or []
            if len(xy) < 200 or not zones:
                return

            def _poly(z):
                return [((p.get("x"), p.get("y")) if isinstance(p, dict) else (p[0], p[1]))
                        for p in (z.get("points") or [])]
            gz_poly = [_poly(z) for z in zones if z.get("points")]
            gz_named = [{"name": z.get("name"), "polygon": _poly(z)} for z in zones if z.get("points")]
            gz_double = [_poly(z) for z in zones
                         if z.get("points") and (z.get("zoneConfig") or {}).get("cleanMode") == 3]
            ng = [_poly(z) for z in (btm.get("nogoZones") or []) if z.get("points")]

            def _work():
                obs = detect_obstacles(gz_named, ng, xy)
                pcov = analyze_pass_coverage(xy, gz_poly, ng, obstacles=obs, double_polys=gz_double)
                # per-zone coverage needed for the flaggable-completeness gate (not computed by
                # the live tick after a restart, so compute it here on the restored trail).
                zstats = assign_to_zones(zones, xy, [])
                return obs, pcov, zstats

            obs, pcov, zstats = await self.hass.async_add_executor_job(_work)
            self._state["zone_stats"] = zstats
            # Store the obstacles too (the live mow stores them, but after a restart the
            # bootstrap is the only thing that recomputes them) so the map marks them. Keep only
            # flags inside flaggable (task-list + finished + not-current) zones.
            obs, pcov = self._filter_to_flaggable(obs or [], pcov, zones, zstats)
            self._state["obstacle_events"] = obs
            if pcov:
                self._state["pass_coverage"] = pcov
            self.async_set_updated_data(self._state)
        except Exception:
            _LOGGER.debug("on-demand pass-coverage failed for %s", self.thing_name, exc_info=True)

    def _persist_sticky_values(self) -> None:
        """Save one-shot device info to config entry so it survives restarts."""
        if not self._config_entry:
            return
        sticky = {k: self._state[k] for k in self._STICKY_KEYS if self._state.get(k)}
        existing = self._config_entry.data.get("sticky_device_info", {})
        if sticky and sticky != existing:
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={**self._config_entry.data, "sticky_device_info": sticky},
            )

    def _handle_notify_app(self, payload: dict) -> None:
        # Diagnostic: the notify-app channel is the most likely carrier of a
        # command result/ack (PbMutateResult is NOT a PbOutput field, so it does
        # not arrive on the telemetry topic). We currently use only robotState;
        # log the whole payload so a future "command accepted but nothing happened"
        # (e.g. the OTA trigger that returns to 'Update Available' without
        # installing) shows whatever the mower reported back. Verified 2026-06-02:
        # OTA fired userCtrl=26, workStatus stayed 1 (Waiting), never 11 (Updating).
        _LOGGER.debug("Lymow notify-app payload for %s: %s", self.thing_name, payload)
        # Capture the full notify-app JSON: if the app's push notifications arrive on
        # any topic HA can see, this is the candidate (they are NOT a PbOutput field).
        self._pathcap_record({"type": "notify_app", "payload": payload})
        rs = payload.get("robotState")
        if rs == "online":
            self._rest_online = True
            self._state.update({"deviceState": "online", "isOnline": True})
        elif rs == "offline":
            self._rest_online = False
            self._state.update({"deviceState": "offline", "isOnline": False})
        self.async_set_updated_data(self._state)

    def _handle_disconnect(self) -> None:
        """Called when paho reports a disconnect.

        AWS IoT temporary credentials expire after ~1 hour, causing the broker
        to close the connection. We must refresh credentials and reconnect with
        a new presigned URL — paho's built-in reconnect cannot do this.
        """
        if self._shutting_down:
            return
        _LOGGER.warning(
            "MQTT disconnected for %s — will refresh credentials and reconnect",
            self.thing_name,
        )
        # Schedule reconnect in the HA event loop (we're in paho's thread here)
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = self.hass.async_create_background_task(
                self._reconnect_with_fresh_creds(),
                name=f"lymow_reconnect_{self.thing_name}"
            )

    async def _reconnect_with_fresh_creds(self) -> None:
        """Refresh AWS credentials and re-create the MQTT connection."""
        await asyncio.sleep(_RECONNECT_DELAY)
        if self._shutting_down:
            return
        try:
            _LOGGER.info("Refreshing AWS credentials for %s", self.thing_name)
            try:
                await self.auth.ensure_valid(self._email, self._password)
            except LymowError as auth_err:
                # Token refresh hard-failed mid-run (refresh token expired/revoked) and
                # there's no stored password to recover with → escalate to HA's reauth
                # flow now, instead of leaving the user stale until the next restart.
                # async_start_reauth is idempotent (HA won't open duplicate flows).
                if self._config_entry and not (self._email and self._password):
                    _LOGGER.warning(
                        "Token refresh failed for %s — starting re-authentication", self.thing_name
                    )
                    self._config_entry.async_start_reauth(self.hass)
                    return
                raise
            await self._connect_mqtt()
            _LOGGER.info("MQTT reconnected for %s", self.thing_name)
        except Exception:
            _LOGGER.exception("MQTT reconnect failed for %s — will retry on next disconnect", self.thing_name)

    # ── Background loops ─────────────────────────────────────────

    async def _refresh_loop(self) -> None:
        """Periodically query config/net/RTK to keep state current."""
        while True:
            try:
                await asyncio.sleep(_REFRESH_INTERVAL)
                if self.mqtt and self.mqtt.is_connected:
                    _LOGGER.debug("Periodic refresh queries for %s", self.thing_name)
                    self._fire_refresh_queries()
                    if self.work_status in (2, 8, 9):  # mowing, resume, zone partition
                        self._publish(encode_query_path())
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Refresh loop error for %s", self.thing_name)

    async def _rest_poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_REST_POLL_INTERVAL.total_seconds())
                await self._do_rest_poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("REST poll failed for %s", self.thing_name)

    async def _do_rest_poll(self) -> None:
        try:
            await self.auth.ensure_valid(self._email, self._password)
            info = await self.client.get_device_info(self.thing_name)
            if info:
                self.device_info_data = info
                ds = info.get("deviceState") or info.get("device_state") or "offline"
                self._rest_online = ds == "online"

                rest_state: dict[str, Any] = {
                    "deviceInfoRest": info,
                    "deviceState": ds,
                    "isOnline": self._rest_online,
                }
                for src, dst in [
                    ("ipAddress", "ipAddress"),
                    ("sn", "sn"),
                    ("macAddress", "macAddress"),
                    ("simId", "simId"),
                    ("rtkSn", "rtkSn"),
                    ("wheelVer", "wheelVer"),
                    ("knifeVer", "knifeVer"),
                    ("mcuVersion", "fwVersion"),
                    ("mcuVersion", "appFwVersion"),
                    ("softwareVersion", "mcuVersion"),
                    ("softwareVersion", "softwareVersion"),
                ]:
                    if val := info.get(src):
                        rest_state[dst] = val.strip() if isinstance(val, str) else val

                loc = info.get("robotLocation")
                if isinstance(loc, list) and len(loc) >= 2:
                    rest_state["robotLocation"] = loc
                    rest_state["latitude"] = loc[0]
                    rest_state["longitude"] = loc[1]

                self._merge_state(rest_state)


                # Lightweight feature data: theft/find settings, notification switches.
                try:
                    features = await self.client.get_device_feature(self.thing_name)
                    if features:
                        self._merge_state({
                            "deviceFeature": features,
                            "theftDetectionSwitch": features.get("theftDetectionSwitch"),
                            "findRobotSwitch": features.get("findRobotSwitch"),
                            "mobileNotificationSwitch": features.get("mobileNotificationSwitch"),
                            "theftLock": features.get("theftLock"),
                        })
                except Exception:
                    _LOGGER.debug("Feature poll error for %s", self.thing_name, exc_info=True)

                self._derive_state()
            else:
                self._rest_online = False
        except Exception:
            _LOGGER.debug("REST poll error for %s", self.thing_name, exc_info=True)

        # ── Firmware update check (outside main try so REST errors don't skip it)
        try:
            update_info = await self.client.check_update(self.thing_name)
            if isinstance(update_info, dict):
                latest = (
                    self._normalize_fw_version(update_info.get("latestVersion"))
                )
                note = (
                    update_info.get("releaseNote")
                )
                # Device-type prefix + RAW latest version build the OTA objectKey
                # (<prefix><rawVersion>). The cloud only returns a non-empty
                # prefix once the firmware object is actually staged/released to
                # THIS device (staged rollout) — until then the update is merely
                # announced and cannot be installed (the official app bails the
                # same way). Stash both so readiness is observable.
                prefix = update_info.get("prefix")
                raw_latest = update_info.get("latestVersion")
                if latest:
                    self._merge_state({
                        "latestFw": latest,
                        "releaseNote": note,
                        "fwPrefix": prefix,
                        "fwLatestRaw": raw_latest,
                    })
                    if prefix:
                        _LOGGER.info(
                            "OTA available + staged for %s: %s (prefix=%s)",
                            self.thing_name, raw_latest, prefix,
                        )
                    else:
                        _LOGGER.debug(
                            "OTA announced but not yet staged for %s (%s): empty prefix",
                            self.thing_name, raw_latest,
                        )
        except Exception:
            _LOGGER.debug("Firmware update check failed for %s", self.thing_name, exc_info=True)

        self.async_set_updated_data(self._state)

    # ── Publish helpers ──────────────────────────────────────────

    def _publish(self, raw: bytes) -> bool:
        if not self.mqtt or not self.mqtt.is_connected:
            return False
        return self.mqtt.publish(raw)

    async def _wait_state_update(self, timeout: float = _WATCHDOG_TIMEOUT) -> bool:
        self._state_event.clear()
        try:
            await asyncio.wait_for(self._state_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _preflight_query_map(self, timeout: float = 3.0) -> None:
        """Ask for a fresh map/state snapshot before user commands.

        The mower often emits a useful state echo after QUERY_MAP. The command
        methods do not fail if the preflight times out; they just proceed with
        the best state currently available.
        """
        if self.mqtt and self.mqtt.is_connected:
            self._publish(encode_query_map())
            await self._wait_state_update(timeout=timeout)

    def _state_row(self):
        return lookup_state_row(
            work_status=self._state.get("workStatus"),
            robot_status=self._state.get("robotStatus"),
            is_recharging=self._state.get("isRecharging"),
        )

    # ── Commands ─────────────────────────────────────────────────

    async def async_start_mow(self, zone_ids: list[str] | None = None) -> bool:
        await self.auth.ensure_valid(self._email, self._password)
        await self._preflight_query_map()
        if zone_ids:
            # Clear stale mowed-area coverage from a previous session before starting
            self._state.pop("mowed_area_polygons", None)
            raw = encode_start_zones(zone_ids)
        else:
            row = self._state_row()
            raw = encode_userctrl(row.start_mowing or USER_CTRL_CLEAN)
        ok  = self._publish(raw)
        await self._wait_state_update()
        return ok

    async def async_pause(self) -> bool:
        await self.auth.ensure_valid(self._email, self._password)
        await self._preflight_query_map()
        row = self._state_row()
        ctrl = row.pause or (USER_CTRL_PAUSE_DOCK if self.work_status in (WORK_STATUS_DOCKING, WORK_STATUS_PAUSE_DOCKING) else USER_CTRL_PAUSE)
        ok   = self._publish(encode_userctrl(ctrl))
        await self._wait_state_update()
        return ok

    async def async_resume(self) -> bool:
        await self.auth.ensure_valid(self._email, self._password)
        await self._preflight_query_map()
        ctrl = USER_CTRL_RESUME_DOCK if self.work_status == WORK_STATUS_PAUSE_DOCKING else USER_CTRL_RESUME
        ok   = self._publish(encode_userctrl(ctrl))
        await self._wait_state_update()
        return ok

    async def async_dock(self) -> bool:
        await self.auth.ensure_valid(self._email, self._password)
        await self._preflight_query_map()
        row = self._state_row()
        ok = self._publish(encode_userctrl(row.dock or USER_CTRL_RECHARGE_DOCK))
        await self._wait_state_update()
        return ok

    async def async_dock_cancel_task(self) -> bool:
        await self.auth.ensure_valid(self._email, self._password)
        await self._preflight_query_map()
        ok = self._publish(encode_userctrl(USER_CTRL_DOCK))
        await self._wait_state_update()
        return ok

    async def async_stop(self) -> bool:
        await self.auth.ensure_valid(self._email, self._password)
        await self._preflight_query_map()
        ok = self._publish(encode_userctrl(USER_CTRL_FORCE_REINIT))
        await self._wait_state_update()
        return ok

    async def async_clear_error(self) -> bool:
        """Clear/acknowledge a mower error so a task can resume. The Lymow app
        implements its contextual 'Clear Error' button as a plain Pause
        (userCtrl=3) — there is no dedicated clear-error opcode (confirmed via
        app bytecode disassembly)."""
        await self.auth.ensure_valid(self._email, self._password)
        ok = self._publish(encode_userctrl(USER_CTRL_PAUSE))
        await self._wait_state_update()
        return ok

    def _set_ota_state(
        self,
        *,
        status: str,
        progress: int | None = None,
        job_id: str | None = None,
        target: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Publish OTA status into coordinator state so the update entity and
        any sensors can render it. Progress comes from MQTT downloadProgress;
        status/phase comes from the cloud IoT-Job summary."""
        patch: dict[str, Any] = {"ota_status": status}
        if progress is not None:
            patch["ota_progress"] = int(progress)
        if job_id is not None:
            patch["ota_job_id"] = job_id
        if target is not None:
            patch["ota_target"] = target
        patch["ota_detail"] = detail
        self._merge_state(patch)
        self.async_set_updated_data(self._state)

    async def async_ota_update(self) -> bool:
        """Trigger a firmware OTA the way the official app does it.

        The app does NOT send a userCtrl opcode for OTA (opcode 26 is an unused
        enum constant — a bare userCtrl=26 is a no-op, which is why earlier
        attempts silently failed). The real flow is 100% cloud REST + an AWS IoT
        Job:
          1. GET /check-update            -> prefix + latestVersion (raw)
          2. GET /create-ota-job          -> objectKey=<prefix><version> -> jobId
             (the cloud creates an IoT Job; the mower pulls & installs it)
          3. poll GET /get-ota-job-summary for status (background task)

        The live percentage is read separately from MQTT telemetry
        (PbDebugSetting.downloadProgress), surfaced by the update entity.
        """
        await self.auth.ensure_valid(self._email, self._password)

        # 1. Fresh check-update for the device-type prefix + RAW target version.
        #    (objectKey must use the raw latestVersion, not our display-normalized
        #    version, or the cloud will not find the firmware object.)
        info = await self.client.check_update(self.thing_name)
        prefix = (info or {}).get("prefix") or ""
        target = (info or {}).get("latestVersion")
        if not target:
            _LOGGER.warning("OTA aborted for %s: no latestVersion from check-update", self.thing_name)
            self._set_ota_state(status="error", detail="no update available")
            return False
        # objectKey = <prefix><rawVersion>. The app proceeds even when prefix is
        # empty — its create-job path guards only the version, never the prefix —
        # so the prefix is OPTIONAL (objectKey collapses to the raw version).
        # Match the app exactly rather than refusing.
        if not prefix:
            _LOGGER.info(
                "OTA for %s: empty prefix from check-update; objectKey=%s (matches app)",
                self.thing_name, target,
            )
        object_key = f"{prefix}{target}"
        _LOGGER.info("OTA: creating cloud job for %s objectKey=%s", self.thing_name, object_key)

        # 2. Create the OTA job.
        job = await self.client.create_ota_job(self.thing_name, object_key)
        _LOGGER.info(
            "OTA create-ota-job raw response for %s (objectKey=%s): %s",
            self.thing_name, object_key, job,
        )
        job_id = None
        if isinstance(job, dict):
            job_id = job.get("jobId") or job.get("jobID") or job.get("id")
        if not job_id:
            _LOGGER.error("OTA: create-ota-job returned no jobId for %s: %s", self.thing_name, job)
            self._set_ota_state(status="error", detail="failed to create OTA job")
            return False

        self._ota_job_id = job_id
        self._set_ota_state(status="queued", progress=0, job_id=job_id, target=target, detail=None)

        # 3. Poll job status in the background (non-blocking).
        if self._ota_poll_task is None or self._ota_poll_task.done():
            self._ota_poll_task = self.hass.async_create_background_task(
                self._poll_ota_job(job_id, target),
                name=f"lymow_ota_poll_{self.thing_name}",
            )
        return True

    async def _poll_ota_job(self, job_id: str, target: str | None) -> None:
        """Poll the cloud IoT-Job summary until terminal or timeout (10 min,
        matching the app). Status drives the phase; the percentage shown comes
        from live MQTT downloadProgress merged into state by the telemetry path."""
        deadline = time.monotonic() + 600
        terminal = {"SUCCEEDED", "FAILED", "CANCELED"}
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(10)
                try:
                    summary = await self.client.get_ota_job_summary(self.thing_name, job_id)
                except Exception:
                    _LOGGER.debug("OTA poll error for %s", self.thing_name, exc_info=True)
                    continue
                status = (summary or {}).get("status") or "IN_PROGRESS"
                pct = int(self._state.get("downloadProgress") or 0)
                _LOGGER.debug(
                    "OTA poll %s: status=%s downloadProgress=%s%% raw=%s",
                    job_id, status, pct, summary,
                )
                detail = None
                if status == "FAILED":
                    details = (summary or {}).get("statusDetails") or {}
                    dmap = details.get("detailsMap") if isinstance(details, dict) else None
                    if isinstance(dmap, dict):
                        detail = dmap.get("reason")
                self._set_ota_state(
                    status=status.lower(), progress=pct, job_id=job_id, target=target, detail=detail,
                )
                if status in terminal:
                    _LOGGER.info("OTA job %s for %s finished: %s", job_id, self.thing_name, status)
                    break
            else:
                _LOGGER.warning("OTA job %s for %s timed out after 10 min", job_id, self.thing_name)
                self._set_ota_state(status="error", job_id=job_id, target=target, detail="timed out")
        finally:
            self._ota_job_id = None

    async def async_abort_ota(self) -> bool:
        """Stop tracking an in-flight OTA.

        Note: the official app provides NO user-initiated OTA cancel — once the
        cloud IoT Job is created the firmware applies it; CANCELED is only ever a
        status the cloud reports. The legacy userCtrl=27 opcode is unused/no-op.
        We therefore just stop our local poll and clear OTA state."""
        if self._ota_poll_task and not self._ota_poll_task.done():
            self._ota_poll_task.cancel()
        self._ota_job_id = None
        self._set_ota_state(status="idle", progress=0, detail=None)
        return True

    async def async_set_blade_height(self, height_mm: int) -> None:
        """Set global blade cut height via PbMap.runTimeConfig."""
        from .protocol import encode_set_cut_height
        self._publish(encode_set_cut_height(height_mm))
        # Aggiorna lo stato locale immediatamente (ottimistic update)
        if self.data:
            self.data["cutHeight"] = height_mm
            btmap = self.data.get("btMap") or {}
            btmap["cutHeight"] = height_mm
            self.data["btMap"] = btmap
        self.async_update_listeners()

    async def async_set_clean_mode(self, mode: str) -> bool:
        """Set global clean mode via PbInput.robotConfig (MQTT only).

        Per live wire captures, cleanMode sits at field 7 of PbRobotConfig
        (PbInput field 13). See encode_set_clean_mode() for encoding details.
        """
        from .protocol import encode_set_clean_mode, CLEAN_MODE_STR
        mode_int = CLEAN_MODE_STR.get(mode)
        if mode_int is None:
            _LOGGER.warning("Unknown clean mode: %s", mode)
            return False
        self._publish(encode_set_clean_mode(mode_int))
        if self.data:
            self.data["cleanMode"] = mode
            self.data["robotCleanMode"] = mode_int
        self.async_update_listeners()
        return True

    def _current_task_config(self) -> dict[str, Any]:
        """Snapshot the live PbTaskConfig fields so a partial write can resend
        the ones it isn't changing. The mower applies the WHOLE taskConfig, so
        sending a single field zeroes the rest (toggling Rainy Mowing was
        resetting chargingMode → Follow Perimeter). Verified on hardware
        2026-06-02."""
        d = self.data or {}
        return {
            "charging_mode": int(d.get("chargingMode") or 0),
            "rain_cleaning": bool(d.get("rainCleaning") or False),
            "zone_order": int(d.get("zoneOrder") or 0),
            "disable_charging_park": bool(d.get("disableChargingPark") or False),
        }

    async def async_set_rainy_mowing(self, enabled: bool) -> bool:
        """Toggle Rainy Mowing (taskConfig.rainCleaning) — preserving siblings."""
        from .protocol import encode_set_task_config
        cfg = self._current_task_config()
        cfg["rain_cleaning"] = bool(enabled)
        ok = self._publish(encode_set_task_config(**cfg))
        if ok and self.data is not None:
            self.data["rainCleaning"] = bool(enabled)
            self.async_update_listeners()
        return ok

    async def async_set_charging_mode(self, mode: int) -> bool:
        """Set return-to-dock route (0=Follow Perimeter, 1=Direct Route —
        verified against the app 2026-06-02; matches select.py) — preserving
        siblings so Rainy Mowing etc. are not reset."""
        from .protocol import encode_set_task_config
        cfg = self._current_task_config()
        cfg["charging_mode"] = int(mode)
        ok = self._publish(encode_set_task_config(**cfg))
        if ok and self.data is not None:
            self.data["chargingMode"] = mode
            self.async_update_listeners()
        return ok

    async def async_set_audio_volume(self, volume: int) -> bool:
        """Set speaker volume (0=Mute, 30=Low, 70=Medium, 100=High)."""
        from .protocol import encode_set_audio_volume
        ok = self._publish(encode_set_audio_volume(volume))
        self._publish(encode_query_robot_config())
        await self._wait_state_update(timeout=3.0)
        return ok

    async def async_play_sound(self, audio_id: int) -> bool:
        """Play a built-in voice prompt on the mower by AudioId (PbInput.audioId) — the
        locate / find-my-mower trigger. Fire-and-forget: the mower plays the prompt but does
        NOT echo a commanded id back in telemetry, so there's no state feedback (confirmed live
        2026-06-22). Shared by the Play Sound select + the lymow.play_sound service."""
        from .protocol import encode_play_audio
        return bool(self._publish(encode_play_audio(int(audio_id))))

    async def async_set_dock_on_error(self, enabled: bool) -> bool:
        """Set whether the mower returns to dock on error."""
        from .protocol import encode_set_dock_on_error
        ok = self._publish(encode_set_dock_on_error(enabled))
        self._publish(encode_query_robot_config())
        await self._wait_state_update(timeout=3.0)
        return ok

    async def async_set_schedule(self, schedules: list[dict]) -> bool:
        _LOGGER.warning("set_schedule not implemented for MQTT path yet")
        return False
    
    async def async_start_schedule_task(self, schedule_id: int) -> bool:
        """Start one decoded schedule manually."""
        await self.auth.ensure_valid(self._email, self._password)

        tasks = ((self._state.get("schedules_data") or {}).get("tasks") or [])
        task = next((t for t in tasks if int(t.get("id", -1)) == int(schedule_id)), None)

        if not task:
            _LOGGER.warning("Schedule task %s not found for %s", schedule_id, self.thing_name)
            return False

        zone_hash_ids = task.get("zoneHashIds") or []
        if not zone_hash_ids:
            _LOGGER.warning("Schedule task %s has no zones", schedule_id)
            return False

        # Stato fresco prima del comando
        self._publish(encode_query_map())
        await self._wait_state_update(timeout=3.0)

        raw = encode_start_schedule_task_full(task)

        # opzionale: pulisci vecchia area tagliata
        self._state.pop("mowed_area_polygons", None)
        ok = self._publish(raw)
        await self._wait_state_update()
        return ok
    
    async def async_set_headlights(self, enabled: bool) -> bool:
        """Turn headlights on or off."""
        await self.auth.ensure_valid(self._email, self._password)

        raw = encode_set_headlights(enabled)

        ok = self._publish(raw)
        await self._wait_state_update()

        # Ask for robot config again, so camLedStatus updates quickly.
        self._publish(encode_query_robot_config())
        await self._wait_state_update(timeout=3.0)

        return ok

    async def async_set_vehicle_led(self, enabled: bool) -> bool:
        """Turn the vehicle LEDs (top indicators + LCD backlight) on or off."""
        await self.auth.ensure_valid(self._email, self._password)

        raw = encode_set_vehicle_led(enabled)

        ok = self._publish(raw)
        await self._wait_state_update()

        # Ask for robot config again, so vehLedStatus updates quickly.
        self._publish(encode_query_robot_config())
        await self._wait_state_update(timeout=3.0)

        return ok

    async def _send_rr_update(
        self,
        *,
        enable_rr: bool | None = None,
        recharge_bat: int | None = None,
        resume_bat: int | None = None,
    ) -> bool:
        """Update rrConfig while preserving existing values."""

        await self.auth.ensure_valid(self._email, self._password)

        d = self._state or {}

        current_enable = d.get("rrEnabled")
        current_recharge = d.get("rrRechargeBat")
        current_resume = d.get("rrResumeBat")

        # Default sicuri se il robotConfig non è ancora arrivato
        if current_enable is None:
            current_enable = True
        if current_recharge is None:
            current_recharge = 10
        if current_resume is None:
            current_resume = 98

        # Orari RR già flattenati nello state.py, se presenti
        rr_start = d.get("rrResumePeriodStart") or {}
        rr_end = d.get("rrResumePeriodEnd") or {}

        start_h = int(rr_start.get("hour", 0)) if isinstance(rr_start, dict) else 0
        start_m = int(rr_start.get("minute", 0)) if isinstance(rr_start, dict) else 0
        end_h = int(rr_end.get("hour", 0)) if isinstance(rr_end, dict) else 0
        end_m = int(rr_end.get("minute", 0)) if isinstance(rr_end, dict) else 0

        raw = encode_set_rr_config(
            enable_rr=bool(current_enable if enable_rr is None else enable_rr),
            recharge_bat=int(current_recharge if recharge_bat is None else recharge_bat),
            resume_bat=int(current_resume if resume_bat is None else resume_bat),
            period_start_hour=start_h,
            period_start_minute=start_m,
            period_end_hour=end_h,
            period_end_minute=end_m,
        )

        ok = self._publish(raw)
        await self._wait_state_update()
        return ok


    async def async_set_auto_recharge(self, enabled: bool) -> bool:
        """Enable/disable auto recharge."""
        return await self._send_rr_update(enable_rr=enabled)


    async def cmd_set_auto_recharge(self, enabled: bool) -> bool:
        """Alias used by switch entities."""
        return await self.async_set_auto_recharge(enabled)


    async def async_set_recharge_threshold(self, value: int) -> bool:
        """Set battery percentage where mower should go recharge."""
        value = max(1, min(100, int(value)))
        return await self._send_rr_update(recharge_bat=value)


    async def cmd_set_recharge_threshold(self, value: int) -> bool:
        """Alias used by number entities."""
        return await self.async_set_recharge_threshold(value)


    async def async_set_resume_threshold(self, value: int) -> bool:
        """Set battery percentage where mower should resume mowing."""
        value = max(1, min(100, int(value)))
        return await self._send_rr_update(resume_bat=value)

    @callback
    def set_channel_buffer_m(self, value: float) -> None:
        """Local-only setting: how far (metres) outside a channel polygon the
        mower still counts as 'in' the channel. Not sent to the device — only
        affects current-channel derivation. Pushes an update so the next derive
        and the number entity both see it."""
        self._state["channel_buffer_m"] = max(0.0, float(value))
        self.async_set_updated_data(self._state)

    def set_mow_interval_days(self, value: float) -> None:
        """Local-only setting (#2): how often each zone should be mowed. Drives the
        mow-age colour ramp + the Overdue Zones sensor. Persisted with the coverage
        masks so it survives restarts. Not sent to the device."""
        v = max(1.0, float(value))
        self._zone_coverage.mow_interval_days = v
        self._state["mow_interval_days"] = v
        self.hass.async_create_task(
            self._zonecov_store.async_save(self._zone_coverage.to_dict()))
        self.async_set_updated_data(self._state)

    def set_dim_by_age(self, on: bool) -> None:
        """Local-only setting (#2): when ON, dim the stripe coverage styles (Green Checker /
        Logical Passes / Gradient / Activity) per zone by mow-age — older zones go darker/
        duller while the stripes stay visible. Persisted with the coverage masks; not sent
        to the device."""
        self._zone_coverage.dim_by_age = bool(on)
        self._state["dim_by_age"] = bool(on)
        self.hass.async_create_task(
            self._zonecov_store.async_save(self._zone_coverage.to_dict()))
        self.async_set_updated_data(self._state)


    async def cmd_set_resume_threshold(self, value: int) -> bool:
        """Alias used by number entities."""
        return await self.async_set_resume_threshold(value)
    
    async def async_set_rr_start_time(self, hour: int, minute: int) -> bool:
        """Set RR resume period start time."""
        await self.auth.ensure_valid(self._email, self._password)

        d = self._state or {}

        current_enable = d.get("rrEnabled")
        current_recharge = d.get("rrRechargeBat")
        current_resume = d.get("rrResumeBat")

        if current_enable is None:
            current_enable = True
        if current_recharge is None:
            current_recharge = 10
        if current_resume is None:
            current_resume = 98

        rr_end = d.get("rrResumePeriodEnd") or {}
        end_h = int(rr_end.get("hour", 0)) if isinstance(rr_end, dict) else 0
        end_m = int(rr_end.get("minute", 0)) if isinstance(rr_end, dict) else 0

        raw = encode_set_rr_config(
            enable_rr=bool(current_enable),
            recharge_bat=int(current_recharge),
            resume_bat=int(current_resume),
            period_start_hour=max(0, min(23, int(hour))),
            period_start_minute=max(0, min(59, int(minute))),
            period_end_hour=end_h,
            period_end_minute=end_m,
        )

        ok = self._publish(raw)
        await self._wait_state_update()
        return ok


    async def async_set_rr_end_time(self, hour: int, minute: int) -> bool:
        """Set RR resume period end time."""
        await self.auth.ensure_valid(self._email, self._password)

        d = self._state or {}

        current_enable = d.get("rrEnabled")
        current_recharge = d.get("rrRechargeBat")
        current_resume = d.get("rrResumeBat")

        if current_enable is None:
            current_enable = True
        if current_recharge is None:
            current_recharge = 10
        if current_resume is None:
            current_resume = 98

        rr_start = d.get("rrResumePeriodStart") or {}
        start_h = int(rr_start.get("hour", 0)) if isinstance(rr_start, dict) else 0
        start_m = int(rr_start.get("minute", 0)) if isinstance(rr_start, dict) else 0

        raw = encode_set_rr_config(
            enable_rr=bool(current_enable),
            recharge_bat=int(current_recharge),
            resume_bat=int(current_resume),
            period_start_hour=start_h,
            period_start_minute=start_m,
            period_end_hour=max(0, min(23, int(hour))),
            period_end_minute=max(0, min(59, int(minute))),
        )

        ok = self._publish(raw)
        await self._wait_state_update()
        return ok

    async def async_remote_control(
        self,
        *,
        linear_speed: float = 0.0,
        angular_speed: float = 0.0,
    ) -> bool:
        """Send a raw remote-control movement command."""
        await self.auth.ensure_valid(self._email, self._password)

        linear_speed = max(-1.0, min(1.0, float(linear_speed)))
        angular_speed = max(-1.0, min(1.0, float(angular_speed)))

        return self._publish(
            encode_remote_control(
                linear_speed=linear_speed,
                angular_speed=angular_speed,
            )
        )


    async def async_remote_stop(self) -> bool:
        """Stop remote/manual movement."""
        await self.auth.ensure_valid(self._email, self._password)
        return self._publish(encode_remote_stop())


    async def async_remote_pulse(
        self,
        *,
        linear_speed: float = 0.0,
        angular_speed: float = 0.0,
        duration: float = _REMOTE_PULSE_SECONDS,
    ) -> bool:
        """Move briefly, then stop automatically.

        Useful for Home Assistant ButtonEntity because buttons do not expose
        press/release events.
        """
        ok = await self.async_remote_control(
            linear_speed=linear_speed,
            angular_speed=angular_speed,
        )

        await asyncio.sleep(max(0.05, min(2.0, float(duration))))

        await self.async_remote_stop()
        return ok


    async def async_remote_forward(self) -> bool:
        return await self.async_remote_pulse(
            linear_speed=_REMOTE_LINEAR_SPEED,
            angular_speed=0.0,
        )


    async def async_remote_backward(self) -> bool:
        return await self.async_remote_pulse(
            linear_speed=-_REMOTE_LINEAR_SPEED,
            angular_speed=0.0,
        )


    async def async_remote_left(self) -> bool:
        return await self.async_remote_pulse(
            linear_speed=0.0,
            angular_speed=_REMOTE_ANGULAR_SPEED,
        )


    async def async_remote_right(self) -> bool:
        return await self.async_remote_pulse(
            linear_speed=0.0,
            angular_speed=-_REMOTE_ANGULAR_SPEED,
        )

    # ── One-time fetches ─────────────────────────────────────────

    async def async_refresh_device_info(self) -> None:
        try:
            await self.auth.ensure_valid(self._email, self._password)
            self.device_info_data = await self.client.get_device_info(self.thing_name)
        except LymowError as err:
            _LOGGER.warning("Cannot fetch device info for %s: %s", self.thing_name, err)
    
    async def async_refresh_schedules(self) -> bool:
        await self.auth.ensure_valid(self._email, self._password)
        ok = self._publish(encode_query_schedules())
        await self._wait_state_update()
        return ok

    async def async_refresh_history(self, count: int = 10) -> list[dict]:
        try:
            await self.auth.ensure_valid(self._email, self._password)
            data = await self.client.get_clean_history(self.thing_name, size=count)
            if isinstance(data, dict):
                self._merge_state({
                    "cleanHistory": data.get("clean_history") or [],
                    "cleanHistorySummary": data.get("clean_summary") or {},
                    "cleanHistoryTotalRecords": data.get("total_records"),
                })
                self.history = data.get("clean_history") or []
            else:
                self.history = data or []
            self.async_set_updated_data(self._state)
            return self.history
        except LymowError as err:
            _LOGGER.warning("History fetch failed: %s", err)
            return []

    # ── DataUpdateCoordinator shim ───────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        return self._state