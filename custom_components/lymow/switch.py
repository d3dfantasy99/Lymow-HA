"""Lymow switch platform.

Currently exposes only switches that can be written safely with the known
MQTT/protobuf command set.

Switches:
  - Auto Recharge: toggles robotConfig.rrConfig.enableRr while carrying forward
    the existing RR thresholds and time window.

Read-only booleans such as Online, Charging, WiFi/LTE connected, Theft Lock,
and Lifted belong in binary_sensor.py, not switch.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity
from .protocol import encode_set_rr_config

_LOGGER = logging.getLogger(__name__)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read key from dict or attribute object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _has_field(obj: Any, key: str) -> bool:
    """Return True when a protobuf field or dict key is present."""
    if obj is None:
        return False
    if isinstance(obj, dict):
        return key in obj and obj.get(key) is not None
    has_field = getattr(obj, "HasField", None)
    if callable(has_field):
        try:
            return bool(has_field(key))
        except Exception:
            # Proto3 scalar fields may not support HasField depending on codegen.
            return getattr(obj, key, None) is not None
    return getattr(obj, key, None) is not None


def _time_part(obj: Any, key: str, default: int = 0) -> int:
    value = _get(obj, key, default)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _rr_config_from_state(data: dict[str, Any]) -> Any | None:
    """Return rrConfig object/dict from coordinator state, if available."""
    robot_config = data.get("robotConfig")
    rr = _get(robot_config, "rrConfig")
    if rr is not None:
        return rr

    # Some merge paths may expose a flattened robotConfig dict.
    if isinstance(robot_config, dict):
        rr = robot_config.get("rrConfig")
        if rr is not None:
            return rr

    return None


def _rr_bool(data: dict[str, Any]) -> bool | None:
    """Current auto-recharge switch state."""
    if data.get("rrEnabled") is not None:
        return bool(data.get("rrEnabled"))
    rr = _rr_config_from_state(data)
    if rr is None:
        return None
    if _has_field(rr, "enableRr"):
        return bool(_get(rr, "enableRr"))
    return None


@dataclass(frozen=True, kw_only=True)
class LymowSwitchDesc(SwitchEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool | None]
    turn_on_fn: Callable[[LymowCoordinator], Any]
    turn_off_fn: Callable[[LymowCoordinator], Any]


async def _set_auto_recharge(coordinator: LymowCoordinator, enabled: bool) -> None:
    """Toggle rrConfig.enableRr without resetting the other RR fields.

    The firmware treats rrConfig as a replace-style write for nested fields, so
    we carry forward recharge/resume thresholds and resumePeriodStart/End.
    """
    data = coordinator.data or {}
    rr = _rr_config_from_state(data)

    # Prefer coordinator helper if your coordinator already provides it.
    for method_name in ("async_set_auto_recharge", "cmd_set_auto_recharge"):
        method = getattr(coordinator, method_name, None)
        if callable(method):
            await method(enabled)
            return

    if rr is None:
        raise HomeAssistantError(
            "rrConfig is not available yet. Wait for robotConfig to arrive, "
            "or press Refresh Robot Config, then retry."
        )

    recharge_bat = _get(rr, "rechargeBat", data.get("rrRechargeBat"))
    resume_bat = _get(rr, "resumeBat", data.get("rrResumeBat"))

    period_start = _get(rr, "resumePeriodStart")
    period_end = _get(rr, "resumePeriodEnd")

    raw = encode_set_rr_config(
        enable_rr=enabled,
        recharge_bat=int(recharge_bat) if recharge_bat is not None else None,
        resume_bat=int(resume_bat) if resume_bat is not None else None,
        period_start_hour=_time_part(period_start, "hour", 0),
        period_start_minute=_time_part(period_start, "minute", 0),
        period_end_hour=_time_part(period_end, "hour", 0),
        period_end_minute=_time_part(period_end, "minute", 0),
    )

    ok = coordinator._publish(raw)  # Integration-internal MQTT publish helper.
    if not ok:
        raise HomeAssistantError("MQTT publish failed while setting Auto Recharge")

    # Optimistic update so the UI moves immediately; next robotConfig broadcast
    # will confirm the real value.
    if coordinator.data is not None:
        coordinator.data["rrEnabled"] = enabled
        coordinator.async_update_listeners()


async def _auto_recharge_on(coordinator: LymowCoordinator) -> None:
    await _set_auto_recharge(coordinator, True)


async def _auto_recharge_off(coordinator: LymowCoordinator) -> None:
    await _set_auto_recharge(coordinator, False)


SWITCHES: tuple[LymowSwitchDesc, ...] = (
    LymowSwitchDesc(
        key="auto_recharge",
        name="Auto Recharge",
        icon="mdi:battery-sync",
        value_fn=_rr_bool,
        turn_on_fn=_auto_recharge_on,
        turn_off_fn=_auto_recharge_off,
        entity_registry_enabled_default=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LymowSwitch(coord, desc) for desc in SWITCHES] + [LymowHeadlightsSwitch(coord), LymowVehicleLedSwitch(coord), LymowDockOnErrorSwitch(coord), LymowRainyMowingSwitch(coord), LymowDiagnosticCaptureSwitch(coord)],
        update_before_add=False,
    )


class LymowRainyMowingSwitch(LymowEntity, SwitchEntity):
    """Rainy Mowing (taskConfig.rainCleaning): ON = keep mowing when the rain
    sensor triggers; OFF = dock and wait for dry (recommended — wet mowing clogs
    the deck and leaves grass juice on the charge contacts)."""

    _attr_name = "Rainy Mowing"
    _attr_icon = "mdi:weather-pouring"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "rainy_mowing_switch")

    @property
    def is_on(self) -> bool:
        # rainCleaning is a proto3 bool: false (the default + recommended setting)
        # is never transmitted, so it reads None after a reload. Default to False
        # so the entity has a KNOWN state and renders as the normal pill toggle —
        # not the "unknown" two-button (lightning-bolt) control. A True setting is
        # restored from the sticky store (rainCleaning in _STICKY_KEYS).
        return bool((self.coordinator.data or {}).get("rainCleaning"))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_rainy_mowing(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_rainy_mowing(False)

class LymowSwitch(LymowEntity, SwitchEntity):
    """Lymow switch entity."""

    entity_description: LymowSwitchDesc

    def __init__(self, coordinator: LymowCoordinator, desc: LymowSwitchDesc) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.value_fn(self.coordinator.data or {}) is not None

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data or {})

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.entity_description.turn_on_fn(self.coordinator)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.entity_description.turn_off_fn(self.coordinator)

class LymowHeadlightsSwitch(LymowEntity, SwitchEntity):
    """Headlights / camera LED switch."""

    _attr_name = "Headlights"
    _attr_icon = "mdi:car-light-high"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "headlights")

    @property
    def is_on(self) -> bool | None:
        d = self.coordinator.data or {}

        cam = d.get("camLedStatus")

        if cam is None:
            rc = d.get("robotConfig")
            if isinstance(rc, dict):
                cam = rc.get("camLedStatus")
            elif rc is not None:
                cam = getattr(rc, "camLedStatus", None)

        if cam is None:
            return None

        # Verified:
        # camLedStatus = 3 -> ON
        # camLedStatus = 4 -> OFF
        return int(cam) == 3

    @property
    def available(self) -> bool:
        d = self.coordinator.data or {}
        return super().available and (
            d.get("camLedStatus") is not None
            or d.get("robotConfig") is not None
        )

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_headlights(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_headlights(False)

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.data or {}
        return {
            "cam_led_status": d.get("camLedStatus"),
            "veh_led_status": d.get("vehLedStatus"),
            "open_led_time": d.get("openLedTime"),
            "close_led_time": d.get("closeLedTime"),
            "state_mapping": {
                "3": "on",
                "4": "off",
            },
        }


class LymowVehicleLedSwitch(LymowEntity, SwitchEntity):
    """Vehicle LEDs — the indicator LEDs on top of the mower plus the LCD backlight.

    Distinct from the headlight/camera LED. Unlike the camera LED, the firmware does
    NOT auto-off these on dock/charge, so they stay lit at night unless turned off —
    which makes an HA automation (off while docked, on while off-dock) useful.
    """

    _attr_name = "Vehicle LEDs"
    _attr_icon = "mdi:led-on"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "vehicle_led")

    @property
    def is_on(self) -> bool | None:
        d = self.coordinator.data or {}

        veh = d.get("vehLedStatus")

        if veh is None:
            rc = d.get("robotConfig")
            if isinstance(rc, dict):
                veh = rc.get("vehLedStatus")
            elif rc is not None:
                veh = getattr(rc, "vehLedStatus", None)

        if veh is None:
            return None

        # Verified against the cloud shadow:
        # vehLedStatus = 3 -> ON
        # vehLedStatus = 4 -> OFF   (same encoding as camLedStatus)
        return int(veh) == 3

    @property
    def available(self) -> bool:
        d = self.coordinator.data or {}
        return super().available and (
            d.get("vehLedStatus") is not None
            or d.get("robotConfig") is not None
        )

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_vehicle_led(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_vehicle_led(False)

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.data or {}
        return {
            "veh_led_status": d.get("vehLedStatus"),
            "state_mapping": {
                "3": "on",
                "4": "off",
            },
        }


class LymowDockOnErrorSwitch(LymowEntity, SwitchEntity):
    """Switch to control whether mower returns to dock on error."""

    _attr_name = "Dock On Error"
    _attr_icon = "mdi:home-alert"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "dock_on_error_switch")

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get("dockOnError")
        return bool(val) if val is not None else None

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_dock_on_error(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_dock_on_error(False)


class LymowDiagnosticCaptureSwitch(LymowEntity, SwitchEntity):
    """Diagnostic Capture — writes the map's render-input snapshot to a file you can send to the
    developers to reproduce a map issue. Local toggle, off by default, NOT sent to the mower."""

    _attr_name = "Diagnostic Capture"
    _attr_icon = "mdi:bug-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_extra_state_attributes = {"description": "When ON, writes the map's render-input data (zones, channels, coverage trail, pass-coverage analysis, obstacles, anomalies, settings) to /config/lymow_diagnostic_<device>.json, refreshed every ~5 s. Flip on, reproduce the issue, flip off, download the file (Samba / File editor / VS Code add-on) and send it to the developers. Contains your yard's GPS layout — share only with devs. Off by default; resets off on restart."}

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "diagnostic_capture")

    @property
    def is_on(self) -> bool:
        return bool(getattr(self.coordinator, "_diag_capture", False))

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_diag_capture(True)

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_diag_capture(False)
