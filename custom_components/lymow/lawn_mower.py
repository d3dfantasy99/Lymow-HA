"""Lymow lawn mower entity.

Start / Pause / Dock are routed through state_matrix.STATE_MATRIX so
the correct userCtrl variant is always sent for the current robot state.

Services (start_zones, dock_cancel_task, cancel_task) are registered in
__init__.py via _register_services().
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import state_matrix
from .const import DOMAIN
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity
from .protocol import encode_userctrl

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowMower(coord)])


class LymowMower(LymowEntity, LawnMowerEntity):
    """Lymow robot mower entity — matrix-driven state and dispatch."""

    _attr_name = None   # use device name

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "mower")

    # ── matrix lookup ─────────────────────────────────────────────────────

    def _row(self) -> state_matrix.StateRow:
        d   = self.coordinator.data or {}
        ws  = d.get("workStatus",  0)
        rs  = d.get("robotStatus", 0)
        rch = bool(d.get("isRecharging", False))
        return state_matrix.lookup(
            work_status=ws,
            robot_status=rs,
            is_recharging=rch,
        )

    # ── HA interface ──────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def activity(self) -> LawnMowerActivity | None:
        val = self._row().activity
        return LawnMowerActivity(val) if val is not None else None

    @property
    def supported_features(self) -> LawnMowerEntityFeature:
        return state_matrix.features_for(self._row())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data or {}
        attrs: dict[str, Any] = {}
        for key in ("cleanZoneIds", "cleanPercent", "isRecharging", "mapArea", "areaOrGlobal"):
            if key in d:
                attrs[key] = list(d[key]) if key == "cleanZoneIds" else d[key]
        return attrs

    # ── commands ──────────────────────────────────────────────────────────

    async def async_start_mowing(self) -> None:
        await self._dispatch("start_mowing")

    async def async_pause(self) -> None:
        await self._dispatch("pause")

    async def async_dock(self) -> None:
        """Dock and KEEP task progress (recharge-resume).

        Use lymow.dock_cancel_task service for the destructive variant.
        """
        await self._dispatch("dock")

    async def _dispatch(self, button: str) -> None:
        row    = self._row()
        action = getattr(row, button)
        if action is None:
            d  = self.coordinator.data or {}
            raise HomeAssistantError(
                f"Mower can't {button.replace('_', ' ')} from current state "
                f"(workStatus={d.get('workStatus', 0)}, robotStatus={d.get('robotStatus', 0)})"
            )
        _LOGGER.debug("Lymow %s → userCtrl=%s", button, action)
        self.coordinator._publish(encode_userctrl(action))