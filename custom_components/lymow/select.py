"""Lymow select platform.

Only command/config selects live here. The old S3 backup-map selector was
removed because the mower now provides its live map through QUERY_MAP.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    AUDIO_LABEL_TO_ID,
    AUDIO_PLAY_OPTIONS,
    CLEAN_MODE_OPTIONS,
    COVERAGE_STYLE_DEFAULT,
    COVERAGE_STYLE_OPTIONS,
    DOMAIN,
    F_CLEAN_MODE,
    MAP_LABELS_DEFAULT,
    MAP_LABELS_OPTIONS,
    MAP_RESOLUTION_DEFAULT,
    MAP_RESOLUTION_OPTIONS,
    MOWER_SIZE_DEFAULT,
    MOWER_SIZE_OPTIONS,
)
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity
from .heatmap import (
    HEATMAP_STYLE_DEFAULT,
    HEATMAP_STYLE_OPTIONS,
    MAP_LAYER_DEFAULT,
    MAP_LAYER_OPTIONS,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        LymowCleanModeSelect(coord),
        LymowVolumeSelect(coord),
        LymowDockRouteSelect(coord),
        LymowCoverageStyleSelect(coord),
        LymowMapLayerSelect(coord),
        LymowHeatmapStyleSelect(coord),
        LymowMapLabelsSelect(coord),
        LymowMapResolutionSelect(coord),
        LymowMowerSizeSelect(coord),
        LymowPlaySoundSelect(coord),
    ], update_before_add=False)


class LymowPlaySoundSelect(LymowEntity, SelectEntity):
    """Manually play one of the mower's built-in voice prompts (locate / find-my-mower).

    Momentary action menu: pick a prompt → it plays → the select snaps back to "None". For
    automations and custom Lovelace buttons, call the `lymow.play_sound` service with audio_id
    instead — that's the one-shot primitive; this select is the point-and-play manual UI.
    The prompts are spoken sentences (the mower's own voice vocabulary), not tones; the mower
    plays on command but gives NO state feedback (fire-and-forget)."""

    _attr_name = "Play Sound"
    _attr_icon = "mdi:bullhorn"
    _attr_options = AUDIO_PLAY_OPTIONS
    _attr_current_option = "None"
    _attr_extra_state_attributes = {"description": "Manually play one of the mower's built-in voice prompts (find-my-mower / locate). Pick a prompt to play it; the select then resets to 'None'. Fire-and-forget — the mower has no audio feedback. For automations or single-tap Lovelace buttons, call the lymow.play_sound service with audio_id (0-33) instead."}

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "play_sound_select")

    async def async_select_option(self, option: str) -> None:
        aid = AUDIO_LABEL_TO_ID.get(option)
        if aid is not None:                       # skip "None" (idle/reset)
            await self.coordinator.async_play_sound(aid)
        self._attr_current_option = "None"        # momentary — return to idle
        self.async_write_ha_state()


class LymowMowerSizeSelect(LymowEntity, SelectEntity):
    """How large the mower glyph is drawn on the diagnostic map."""

    _attr_name = "Map Mower Size"
    _attr_icon = "mdi:robot-mower"
    _attr_options = MOWER_SIZE_OPTIONS
    _attr_extra_state_attributes = {"description": "How large the mower marker is drawn on the diagnostic map: Small / Medium / Large / Extra Large. The glyph is anchored to the real swath (16 in cut) so it always scales with the yard — this just picks how prominent it is. Local display option — doesn't affect the mower."}

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "mower_size_select")

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get("mower_size", MOWER_SIZE_DEFAULT)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_ui_pref("mower_size", option)


class LymowMapLayerSelect(LymowEntity, SelectEntity):
    """Which data layer the map shows: Coverage or a telemetry-channel heatmap."""

    _attr_name = "Map Layer"
    _attr_icon = "mdi:layers"
    _attr_options = MAP_LAYER_OPTIONS
    _attr_extra_state_attributes = {"description": "Which data layer the diagnostic map shows: Coverage (the mowed swath), Pass Coverage (un-mowed ground marked translucent RED + single-pass amber rings + a missed-spot count, over the coverage), or a telemetry heatmap (WiFi / Cellular / LoRa Link / RTK SNR / Position Accuracy / RTK Correction Age). Local display option — doesn't affect the mower."}

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "map_layer_select")

    @property
    def current_option(self) -> str | None:
        v = (self.coordinator.data or {}).get("map_layer", MAP_LAYER_DEFAULT)
        return "RTK Correction Age" if v == "Correction Age" else v   # migrate the old name

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_ui_pref("map_layer", option)


class LymowHeatmapStyleSelect(LymowEntity, SelectEntity):
    """Render style for a data-heatmap layer (Smooth / Contour / Weak-spots / Path)."""

    _attr_name = "Map Heatmap Style"
    _attr_icon = "mdi:gradient-horizontal"
    _attr_options = HEATMAP_STYLE_OPTIONS
    _attr_extra_state_attributes = {"description": "Render style for a telemetry heatmap layer (when Map Layer is a signal channel): Smooth (interpolated thermal field), Contour (banded), Weak-spots (flags only poor cells), or Path (per-point dots). Local display option."}

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "heatmap_style_select")

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get("heatmap_style", HEATMAP_STYLE_DEFAULT)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_ui_pref("heatmap_style", option)


class LymowCoverageStyleSelect(LymowEntity, SelectEntity):
    """Render style for the coverage map (local UI preference, not a mower command)."""

    _attr_name = "Map Coverage Style"
    _attr_icon = "mdi:palette"
    _attr_options = COVERAGE_STYLE_OPTIONS
    _attr_entity_category = None
    _attr_extra_state_attributes = {"description": "How the mowed-area coverage is drawn on the map: Green Checker (crosshatch plaid, perimeter laps a uniform dark ring), Gradient (recency — dark→bright = oldest→newest), or Logical Passes (each row coloured by its mow axis). Local display option."}

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "coverage_style_select")

    @property
    def current_option(self) -> str | None:
        v = (self.coordinator.data or {}).get("coverage_style", COVERAGE_STYLE_DEFAULT)
        return "Paths Off" if v == "No Coverage" else v   # migrate the old name

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_coverage_style(option)


class LymowMapLabelsSelect(LymowEntity, SelectEntity):
    """Which polygon name labels are drawn on the map (local UI preference)."""

    _attr_name = "Map Labels"
    _attr_icon = "mdi:label-outline"
    _attr_options = MAP_LABELS_OPTIONS
    _attr_extra_state_attributes = {"description": "Which name labels are drawn on the map: Both, Zone Names only, No-Go Names only, or None. Yards with many no-go zones get their map cluttered with labels — set this to Zone Names or None to clean it up. Local display option, doesn't affect the mower."}

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "map_labels_select")

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get("map_labels", MAP_LABELS_DEFAULT)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_ui_pref("map_labels", option)


class LymowMapResolutionSelect(LymowEntity, SelectEntity):
    """Render resolution of the diagnostic map camera (local UI preference)."""

    _attr_name = "Map Resolution"
    _attr_icon = "mdi:image-size-select-large"
    _attr_options = MAP_RESOLUTION_OPTIONS
    _attr_extra_state_attributes = {"description": "Pixel size of the rendered map. Bigger = sharper, important for large properties with many zones, but heavier per render (4K is slow on low-power hosts). The map only renders when viewed. Standard 800 / Large 1600 / Extra Large 2400 / 4K 3840 px. Local display option."}

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "map_resolution_select")

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get("map_resolution", MAP_RESOLUTION_DEFAULT)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_ui_pref("map_resolution", option)


class LymowCleanModeSelect(LymowEntity, SelectEntity):
    """Select entity for mowing mode."""

    _attr_name = "Mow Mode"
    _attr_icon = "mdi:grass"
    _attr_options = CLEAN_MODE_OPTIONS

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "clean_mode_select")

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get(F_CLEAN_MODE)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_clean_mode(option)


_VOLUME_OPTIONS = ["Mute", "Low", "Medium", "High"]
_VOLUME_TO_INT = {"Mute": 0, "Low": 30, "Medium": 70, "High": 100}
_INT_TO_VOLUME = {0: "Mute", 30: "Low", 70: "Medium", 100: "High"}


class LymowVolumeSelect(LymowEntity, SelectEntity):
    """Select entity for speaker volume."""

    _attr_name = "Speaker Volume"
    _attr_icon = "mdi:volume-high"
    _attr_options = _VOLUME_OPTIONS
    _attr_extra_state_attributes = {"description": "Sets the mower's speaker volume for its audio cues — Mute / Low / Medium / High. This IS sent to the mower (changes its actual setting)."}

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "volume_select")

    @property
    def current_option(self) -> str | None:
        val = (self.coordinator.data or {}).get("audioVolume")
        if val is None:
            return None
        return _INT_TO_VOLUME.get(val, f"{val}%")

    async def async_select_option(self, option: str) -> None:
        vol = _VOLUME_TO_INT.get(option)
        if vol is not None:
            await self.coordinator.async_set_audio_volume(vol)


_DOCK_ROUTE_OPTIONS = ["Direct Route", "Follow Perimeter"]
_DOCK_ROUTE_TO_INT = {"Direct Route": 1, "Follow Perimeter": 0}
_INT_TO_DOCK_ROUTE = {0: "Follow Perimeter", 1: "Direct Route"}


class LymowDockRouteSelect(LymowEntity, SelectEntity):
    """Select entity for return-to-dock route mode."""

    _attr_name = "Return to Dock"
    _attr_icon = "mdi:home-map-marker"
    _attr_options = _DOCK_ROUTE_OPTIONS
    _attr_extra_state_attributes = {"description": "How the mower navigates back to the dock when returning to charge: Follow Perimeter (hug the boundary — the factory default) vs a more direct route. Sent to the mower (PbTaskConfig.chargingMode)."}

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "dock_route_select")

    @property
    def current_option(self) -> str | None:
        val = (self.coordinator.data or {}).get("chargingMode")
        if val is None:
            return "Follow Perimeter"  # Factory default (proto3 zero = not sent)
        return _INT_TO_DOCK_ROUTE.get(val, f"Unknown ({val})")

    async def async_select_option(self, option: str) -> None:
        mode = _DOCK_ROUTE_TO_INT.get(option)
        if mode is not None:
            await self.coordinator.async_set_charging_mode(mode)
