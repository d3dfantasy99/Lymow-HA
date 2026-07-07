"""Lymow Robot Mower integration — MQTT push-driven."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CognitoAuth, LymowClient
from .const import (
    AUTH_METHOD_GOOGLE,
    CONF_AUTH_METHOD,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    DOMAIN,
)
from .config_flow import LymowOAuthStartView
from .coordinator import LymowCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BUTTON,
    Platform.EVENT,
    Platform.LAWN_MOWER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.CAMERA,
    Platform.DEVICE_TRACKER,
    Platform.UPDATE,
    Platform.SWITCH,
    Platform.TIME
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register OAuth view early so it's available during config flow."""
    hass.http.register_view(LymowOAuthStartView)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lymow from a config entry."""
    email      = entry.data.get(CONF_EMAIL, "")
    password   = entry.data.get(CONF_PASSWORD, "")
    region     = entry.data[CONF_REGION]
    thing_name = entry.data["thing_name"]
    auth_method = entry.data.get(CONF_AUTH_METHOD, "password")

    # Register OAuth helper view (idempotent)
    hass.http.register_view(LymowOAuthStartView)

    session = async_get_clientsession(hass)
    auth    = CognitoAuth(region, session)

    # Restore stored tokens to avoid re-login on every HA restart
    if entry.data.get("refresh_token"):
        auth.from_dict(entry.data)
        try:
            await auth.ensure_valid(email or None, password or None)
        except Exception as err:
            if auth_method == AUTH_METHOD_GOOGLE:
                # Refresh token expired/revoked — Google users have no stored
                # credential to silently re-login with. Raise ConfigEntryAuthFailed
                # so HA starts the reauth flow (re-authenticate in place, keep history).
                _LOGGER.warning(
                    "Google OAuth session expired for %s — re-authentication required",
                    thing_name,
                )
                raise ConfigEntryAuthFailed(
                    "Google OAuth session expired — please re-authenticate"
                ) from err
            _LOGGER.warning("Stored tokens invalid for %s — re-logging in", thing_name)
            try:
                await auth.login(email, password)
                await auth.get_aws_credentials()
            except Exception as err2:
                raise ConfigEntryAuthFailed(
                    "Lymow login failed — please re-authenticate"
                ) from err2
    elif email and password:
        try:
            await auth.login(email, password)
            await auth.get_aws_credentials()
        except Exception as err:
            raise ConfigEntryAuthFailed(
                "Lymow login failed — please re-authenticate"
            ) from err
    else:
        raise ConfigEntryAuthFailed(
            "No stored credentials — please re-authenticate"
        )

    client = LymowClient(region, auth, session)

    coordinator = LymowCoordinator(
        hass=hass,
        auth=auth,
        client=client,
        thing_name=thing_name,
        region=region,
        email=email,
        password=password,
        config_entry=entry,
    )

    # Store reference so entity platforms can find it
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Connect MQTT and fire startup queries
    await coordinator.async_setup()

    # Static device info (IP for camera, serial, fw version)
    await coordinator.async_refresh_device_info()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_services(hass)

    # Persist updated tokens
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, **auth.to_dict()},
    )

    return True

def _register_services(hass: HomeAssistant) -> None:
    """Register Lymow services — idempotent, registers only once per HA run."""
    from homeassistant.exceptions import HomeAssistantError
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv, device_registry as dr
    from .protocol import encode_userctrl, encode_start_zones, encode_play_audio
    from .const import USER_CTRL_RECHARGE_DOCK, USER_CTRL_FORCE_REINIT
 
    if hass.services.has_service(DOMAIN, "start_zones"):
        return
 
    def _get_coordinator(call: ServiceCall) -> LymowCoordinator:
        """Resolve coordinator from call.target.device_id or call.data.device_id."""
        target = getattr(call, "target", None) or {}
        target_ids = (target.get("device_id") if isinstance(target, dict) else None)
        device_id = None
        if target_ids:
            device_id = target_ids if isinstance(target_ids, str) else next(iter(target_ids), None)
        if not device_id:
            di = call.data.get("device_id")
            device_id = di if isinstance(di, str) else (di[0] if isinstance(di, list) and di else None)
        if not device_id:
            # Fallback: pick the first (and usually only) coordinator
            coords = list(hass.data.get(DOMAIN, {}).values())
            if len(coords) == 1:
                return coords[0]
            raise HomeAssistantError(
                "Multiple Lymow devices found — specify a target device."
            )
        device_reg = dr.async_get(hass)
        device = device_reg.async_get(device_id)
        if device:
            for domain, identifier in device.identifiers:
                if domain == DOMAIN:
                    for coord in hass.data.get(DOMAIN, {}).values():
                        if getattr(coord, "thing_name", None) == identifier:
                            return coord
        raise HomeAssistantError("Lymow device not found.")
 
    async def _handle_start_zones(call: ServiceCall) -> None:
        coord = _get_coordinator(call)
        raw_zones = call.data.get("zones", [])
        if isinstance(raw_zones, str):
            raw_zones = [raw_zones]
 
        # Resolve zone names → hashIds
        btmap = (coord.data or {}).get("btMap") or {}
        zones = btmap.get("zones") or []
        name_map = {
            (z.get("name") or "").lower(): z.get("hashId")
            for z in zones if z.get("hashId")
        }
        hash_id_set = {z.get("hashId") for z in zones if z.get("hashId")}
 
        resolved: list[str] = []
        for zid in raw_zones:
            if zid in hash_id_set:
                resolved.append(zid)
            else:
                hid = name_map.get(zid.lower())
                if hid:
                    resolved.append(hid)
                else:
                    raise HomeAssistantError(f"Zone '{zid}' not found in map.")
 
        if not resolved:
            raise HomeAssistantError("No valid zones provided.")
 
        coord._publish(encode_start_zones(resolved))
 
    async def _handle_dock_cancel_task(call: ServiceCall) -> None:
        """Dock AND cancel the current task (no recharge-resume)."""
        coord = _get_coordinator(call)
        coord._publish(encode_userctrl(USER_CTRL_RECHARGE_DOCK))
 
    async def _handle_cancel_task(call: ServiceCall) -> None:
        """Force-reinit: stop in place, reset to waiting ('Cancel task' in app)."""
        coord = _get_coordinator(call)
        coord._publish(encode_userctrl(USER_CTRL_FORCE_REINIT))

    async def _handle_play_sound(call: ServiceCall) -> None:
        """Play a voice prompt / sound on the mower (PbInput.audioId). Experiment
        with audio_id (0-33, see AudioId table) to find a good locate beep."""
        coord = _get_coordinator(call)
        coord._publish(encode_play_audio(int(call.data["audio_id"])))
 
    hass.services.async_register(
        DOMAIN, "start_zones", _handle_start_zones,
        schema=vol.Schema({
            vol.Optional("device_id"): vol.Any(str, [str]),
            vol.Required("zones"):     vol.Any(str, [str]),
        }),
    )
    hass.services.async_register(
        DOMAIN, "dock_cancel_task", _handle_dock_cancel_task,
        schema=vol.Schema({vol.Optional("device_id"): vol.Any(str, [str])}),
    )
    hass.services.async_register(
        DOMAIN, "cancel_task", _handle_cancel_task,
        schema=vol.Schema({vol.Optional("device_id"): vol.Any(str, [str])}),
    )
    hass.services.async_register(
        DOMAIN, "play_sound", _handle_play_sound,
        schema=vol.Schema({
            vol.Optional("device_id"): vol.Any(str, [str]),
            vol.Required("audio_id"): vol.All(vol.Coerce(int), vol.Range(min=0, max=33)),
        }),
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: LymowCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator:
        await coordinator.async_shutdown()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded