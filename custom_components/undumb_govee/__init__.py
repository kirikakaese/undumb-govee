"""Undumb Govee: control every light inside a multi-light Govee lamp."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GoveeApiError, GoveeAuthError, GoveeClient
from .const import (
    CARD_URL,
    CONF_DEVICES,
    CONF_SCAN_INTERVAL,
    CONF_ZONES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import Lamp, UndumbCoordinator
from .zones import build_zones, is_light, parse_profile

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT]

type UndumbConfigEntry = ConfigEntry[UndumbCoordinator]


async def _register_card(hass: HomeAssistant) -> None:
    """Serve the bundled Lovelace card and load it on every dashboard."""
    if hass.data.get(f"{DOMAIN}_card") or getattr(hass, "http", None) is None:
        return
    hass.data[f"{DOMAIN}_card"] = True
    path = Path(__file__).parent / "frontend" / "undumb-govee-card.js"
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(path), True)]
        )
        add_extra_js_url(hass, CARD_URL)
    except Exception:  # noqa: BLE001 - the card is optional
        _LOGGER.warning("Could not register the Undumb Govee card", exc_info=True)


async def async_setup_entry(hass: HomeAssistant, entry: UndumbConfigEntry) -> bool:
    """Set up Undumb Govee from a config entry."""
    client = GoveeClient(async_get_clientsession(hass), entry.data[CONF_API_KEY])
    try:
        devices = await client.get_devices()
    except GoveeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except GoveeApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    selected = set(entry.options.get(CONF_DEVICES) or [])
    zone_config = entry.options.get(CONF_ZONES) or {}
    lamps: dict[str, Lamp] = {}
    for dev in devices:
        if not is_light(dev):
            continue
        prof = parse_profile(dev)
        if selected and prof.device_id not in selected:
            continue
        lamp = Lamp(profile=prof, zones=build_zones(prof, zone_config.get(prof.device_id)))
        if prof.has_scenes:
            try:
                lamp.scenes = [
                    s for s in await client.get_scenes(prof.sku, prof.device_id)
                    if s.get("instance") in (None, "lightScene")
                ]
            except GoveeApiError as err:
                _LOGGER.debug("No scenes for %s: %s", prof.name, err)
        lamps[prof.device_id] = lamp
        _LOGGER.debug(
            "%s (%s): %d extra lights %s",
            prof.name,
            prof.sku,
            len(lamp.zones),
            [z.name for z in lamp.zones],
        )

    interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    coordinator = UndumbCoordinator(hass, entry, client, lamps, interval)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await _register_card(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: UndumbConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: UndumbConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

