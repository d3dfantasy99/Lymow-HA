"""Shared base entity for all Lymow platforms."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import LymowCoordinator


class LymowEntity(CoordinatorEntity[LymowCoordinator]):
    """Base class — wires up device_info and unique_id prefix."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LymowCoordinator, unique_suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.thing_name}_{unique_suffix}"

    @property
    def _device_name(self) -> str:
        """Resolved display name of the mower device (used for the device card and
        any sub-device names that hang off it)."""
        info = self.coordinator.device_info_data or {}
        data = self.coordinator.data or {}
        return (
            self.coordinator.config_entry.data.get("device_name")
            or info.get("deviceName")
            or data.get("deviceName")
            or f"Lymow {self.coordinator.thing_name}"
        )

    @property
    def device_info(self) -> dict:
        info = self.coordinator.device_info_data or {}
        data = self.coordinator.data or {}

        name = self._device_name

        model = (
            info.get("model")
            or info.get("deviceType")
            or data.get("deviceType")
            or "Robot Mower"
        )

        # REST get_device_info returns:
        #   mcuVersion      = "app2.3.9 bl0.0.1"
        #   softwareVersion = "v2.1.42_beta"
        # In HA, sw_version should usually show the main firmware/software version.
        sw_version = (
            data.get("softwareVersion")
            or info.get("softwareVersion")
            or data.get("mcuVersion")
            or info.get("mcuVersion")
            or data.get("fwVersion")
            or info.get("fwVersion")
            or info.get("firmwareVersion")
        )

        serial_number = (
            info.get("serialNumber")
            or info.get("sn")
            or data.get("sn")
            or self.coordinator.thing_name
        )

        device_info = {
            "identifiers": {(DOMAIN, self.coordinator.thing_name)},
            "name": name,
            "manufacturer": MANUFACTURER,
            "model": model,
            "sw_version": sw_version,
            "serial_number": serial_number,
        }

        # Optional but useful if available from REST/MQTT.
        connections = set()

        mac = info.get("macAddress") or data.get("macAddress")
        if mac:
            connections.add(("mac", mac))

        if connections:
            device_info["connections"] = connections

        return device_info

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.is_online
