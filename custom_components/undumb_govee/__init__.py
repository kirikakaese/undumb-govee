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
    CONF_LAN_HOSTS,
    CONF_LAYOUTS,
    CONF_SCAN_INTERVAL,
    CONF_USE_LAN,
    CONF_ZONES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import Lamp, UndumbCoordinator
from .lan import GoveeLan
from .zones import build_zones, default_layout, is_light, parse_profile

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


async def _interface_ips(hass: HomeAssistant) -> list[str]:
    """IPv4 addresses of every network interface, for the multicast scan."""
    try:
        from homeassistant.components import network

        adapters = await network.async_get_adapters(hass)
    except Exception:  # noqa: BLE001 - fall back to the default interface
        _LOGGER.debug("Couldn't list network adapters", exc_info=True)
        return []
    ips: list[str] = []
    for adapter in adapters:
        for info in adapter.get("ipv4") or []:
            addr = info.get("address")
            if addr and not addr.startswith("127.") and addr not in ips:
                ips.append(addr)
    return ips


def parse_hosts(text: str | None) -> list[str]:
    return [h.strip() for h in (text or "").replace(";", ",").split(",") if h.strip()]


async def _acquire_lan(hass: HomeAssistant) -> GoveeLan | None:
    """Start (or reuse) the shared LAN listener. None if the port is taken."""
    store = hass.data.setdefault(DOMAIN, {})
    if "lan" in store:
        store["lan_users"] += 1
        return store["lan"]
    lan = GoveeLan()
    try:
        await lan.start()
    except (OSError, RuntimeError) as err:
        _LOGGER.warning(
            "LAN control unavailable, using the cloud only. Port 4002 couldn't be opened (%s). "
            "Another Govee LAN integration may be using it",
            err,
        )
        return None
    store["lan"] = lan
    store["lan_users"] = 1
    return lan


def _release_lan(hass: HomeAssistant) -> None:
    store = hass.data.get(DOMAIN, {})
    if "lan" not in store:
        return
    store["lan_users"] -= 1
    if store["lan_users"] <= 0:
        store.pop("lan").stop()
        store.pop("lan_users", None)


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
    layouts = entry.options.get(CONF_LAYOUTS) or {}
    lamps: dict[str, Lamp] = {}
    for dev in devices:
        if not is_light(dev):
            continue
        prof = parse_profile(dev)
        if selected and prof.device_id not in selected:
            continue
        layout = layouts.get(prof.device_id) or default_layout(prof)
        lamp = Lamp(
            profile=prof,
            zones=build_zones(prof, zone_config.get(prof.device_id), layout),
            layout=layout,
            raw=dev,
        )
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

    lan: GoveeLan | None = None
    hosts = parse_hosts(entry.options.get(CONF_LAN_HOSTS))
    if entry.options.get(CONF_USE_LAN, True) and lamps:
        lan = await _acquire_lan(hass)
        if lan is not None:
            interfaces = await _interface_ips(hass)
            await lan.scan(hosts, interfaces=interfaces)
            found = [lamp.profile.name for dev_id, lamp in lamps.items() if lan.find(dev_id)]
            _LOGGER.debug("LAN control for: %s", found or "no lamps")

    interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    coordinator = UndumbCoordinator(hass, entry, client, lamps, interval, lan, hosts)
    if lan is not None:
        coordinator.interfaces = interfaces
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        if lan is not None:
            _release_lan(hass)
        raise
    entry.runtime_data = coordinator

    await _register_card(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: UndumbConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: UndumbConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and entry.runtime_data.lan is not None:
        _release_lan(hass)
    return unloaded

