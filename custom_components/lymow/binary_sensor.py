"""Lymow binary sensor platform."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    F_IS_CHARGING,
    F_IS_ONLINE,
    F_LTE_WORKING,
    F_WIFI_WORKING,
    WORK_STATUS_CHARGING,
    WORK_STATUS_CHARGING_FULL,
    WORK_STATUS_ERROR,
    WORK_STATUS_EMERGENCY_STOP,
    MOWING_STATUSES,
)
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity


@dataclass(frozen=True, kw_only=True)
class LymowBinDesc(BinarySensorEntityDescription):
    value_fn: Callable[[dict], bool] = lambda d: False


def _read(obj: Any, key: str, default: Any = None) -> Any:
    """Read a key from a dict or protobuf object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


BINARY_SENSORS: tuple[LymowBinDesc, ...] = (
    LymowBinDesc(
        key="online",
        name="Online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:robot-mower",
        # isOnline field OR deviceState == "online" OR workStatus not offline
        value_fn=lambda d: bool(
            d.get(F_IS_ONLINE)
            or d.get("deviceState") == "online"
            or (d.get("workStatus", -1) not in (-1,))
        ),
    ),
    LymowBinDesc(
        key="charging",
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        icon="mdi:battery-charging",
        value_fn=lambda d: (
            bool(d.get(F_IS_CHARGING) or d.get("isRecharging"))
            or d.get("workStatus") in (WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL)
        ),
    ),
    LymowBinDesc(
        key="mowing",
        name="Mowing",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:grass",
        value_fn=lambda d: d.get("workStatus") in MOWING_STATUSES,
    ),
    LymowBinDesc(
        key="error",
        name="Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:alert",
        value_fn=lambda d: (
            d.get("workStatus") in (WORK_STATUS_ERROR, WORK_STATUS_EMERGENCY_STOP)
            or bool(d.get("errorCode") and d.get("errorCode") != 0)
        ),
    ),
    LymowBinDesc(
        key="wifi_connected",
        name="WiFi Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:wifi",
        value_fn=lambda d: bool(d.get(F_WIFI_WORKING))
            or _read(d.get("netDetailInfo"), "currentNet", d.get("currentNet")) == 1,
        entity_registry_enabled_default=False,
    ),
    LymowBinDesc(
        key="lte_connected",
        name="4G Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:signal-4g",
        value_fn=lambda d: bool(d.get(F_LTE_WORKING))
            or _read(d.get("netDetailInfo"), "currentNet", d.get("currentNet")) == 2,
        entity_registry_enabled_default=False,
    ),
    LymowBinDesc(
        key="rain_delay",
        name="Rain Delay",
        device_class=BinarySensorDeviceClass.MOISTURE,
        icon="mdi:weather-rainy",
        value_fn=lambda d: bool(d.get("rainDelay") or d.get("rain_delay")),
        entity_registry_enabled_default=False,
    ),

    LymowBinDesc(
        key="theft_detection",
        name="Theft Detection",
        device_class=BinarySensorDeviceClass.SAFETY,
        icon="mdi:shield-lock",
        value_fn=lambda d: bool(d.get("theftDetectionSwitch")),
        entity_registry_enabled_default=False,
    ),
    LymowBinDesc(
        key="theft_lock",
        name="Theft Lock",
        device_class=BinarySensorDeviceClass.LOCK,
        icon="mdi:lock-alert",
        value_fn=lambda d: bool(d.get("theftLock")),
        entity_registry_enabled_default=False,
    ),
    LymowBinDesc(
        key="lifted",
        name="Lifted",
        device_class=BinarySensorDeviceClass.TAMPER,
        icon="mdi:hand-back-right",
        # Lift is detected via errorCodes[] and warningCodes[] — there is no
        # dedicated boolean field in the shadow. Verified from APK protobuf enums:
        #   errorCodes:   7 = ERROR_FIRST_LIFT_BLOCKED
        #                 8 = ERROR_SECOND_LIFT_BLOCKED
        #   warningCodes: 5 = WARNING_FIRST_LIFT_TIMEOUT
        #                 6 = WARNING_SECOND_LIFT_TIMEOUT
        # Also checks the single errorCode field as fallback.
        value_fn=lambda d: (
            any(c in (d.get("errorCodes") or []) for c in (7, 8))
            or any(c in (d.get("warningCodes") or []) for c in (5, 6))
            or d.get("errorCode") in (7, 8)
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LymowBinarySensor(coord, desc) for desc in BINARY_SENSORS],
        update_before_add=False,
    )


class LymowBinarySensor(LymowEntity, BinarySensorEntity):
    """Lymow binary sensor."""

    entity_description: LymowBinDesc

    def __init__(self, coordinator: LymowCoordinator, desc: LymowBinDesc) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def is_on(self) -> bool:
        return self.entity_description.value_fn(self.coordinator.data or {})
