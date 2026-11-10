"""Polling coordinator and per-lamp runtime state."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GoveeApiError, GoveeAuthError, GoveeClient, GoveeRateLimitError
from .const import CLOUD_EVERY_WITH_LAN, DOMAIN, LAN_RESCAN_EVERY
from .lan import LAN_INSTANCES, GoveeLan
from .zones import KIND_TOGGLE, LAYOUT_SPLIT, DeviceProfile, Zone, state_map

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# After a command, ignore polled values for that instance for this long.
# Govee's cloud often reports the old value for a few seconds.
HOLD_SECONDS = 15


@dataclass
class Lamp:
    """One Govee device with its detected lights."""

    profile: DeviceProfile
    zones: list[Zone]
    layout: str = LAYOUT_SPLIT
    scenes: list[dict[str, Any]] = field(default_factory=list)
    # Zone entities register here so they can coordinate with each other.
    zone_entities: dict[str, Any] = field(default_factory=dict)

    @property
    def device_id(self) -> str:
        return self.profile.device_id

    @property
    def sku(self) -> str:
        return self.profile.sku

    @property
    def needs_cloud_state(self) -> bool:
        return any(z.kind == KIND_TOGGLE for z in self.zones)


class UndumbCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetches state for all selected lamps of one Govee account."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: GoveeClient,
        lamps: dict[str, Lamp],
        interval: int,
        lan: GoveeLan | None = None,
        lan_hosts: list[str] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.lamps = lamps
        self.lan = lan
        self.lan_hosts = lan_hosts or []
        self._holds: dict[str, dict[str, tuple[Any, float]]] = {}
        self._cycle = 0
        self._via_lan: set[str] = set()

    # -- connection helpers -------------------------------------------------

    def lan_ip(self, dev_id: str) -> str | None:
        if self.lan is None:
            return None
        dev = self.lan.find(dev_id)
        return dev.ip if dev else None

    def connection(self, dev_id: str) -> str:
        return "lan" if dev_id in self._via_lan else "cloud"

    # -- polling ------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        data: dict[str, dict[str, Any]] = dict(self.data or {})
        cycle = self._cycle
        self._cycle += 1

        if self.lan is not None and cycle and cycle % LAN_RESCAN_EVERY == 0:
            await self.lan.scan(self.lan_hosts, wait=1.0)

        errors = 0
        for dev_id, lamp in self.lamps.items():
            merged = dict(data.get(dev_id, {}))
            lan_state = None
            if (ip := self.lan_ip(dev_id)) is not None:
                lan_state = await self.lan.status(ip)  # type: ignore[union-attr]
            if lan_state is not None:
                self._via_lan.add(dev_id)
                merged.update(lan_state)
            else:
                self._via_lan.discard(dev_id)

            use_cloud = lan_state is None or lamp.needs_cloud_state or cycle % CLOUD_EVERY_WITH_LAN == 0
            if use_cloud:
                try:
                    caps = await self.client.get_state(lamp.sku, dev_id)
                except GoveeAuthError as err:
                    raise ConfigEntryAuthFailed(str(err)) from err
                except GoveeRateLimitError as err:
                    if lan_state is None:
                        raise UpdateFailed("Govee rate limit reached, retrying later") from err
                except GoveeApiError as err:
                    _LOGGER.debug("State update for %s failed: %s", lamp.profile.name, err)
                    if lan_state is None:
                        errors += 1
                        continue
                else:
                    cloud = state_map(caps)
                    if lan_state is not None:
                        # The LAN answer is fresher for what it covers.
                        cloud = {k: v for k, v in cloud.items() if k not in lan_state}
                    merged.update(cloud)
            data[dev_id] = self._apply_holds(dev_id, merged)
        if errors and errors == len(self.lamps):
            raise UpdateFailed("Could not reach any Govee device")
        return data

    def _apply_holds(self, dev_id: str, fresh: dict[str, Any]) -> dict[str, Any]:
        holds = self._holds.get(dev_id, {})
        now = time.monotonic()
        for inst, (value, until) in list(holds.items()):
            if now < until:
                fresh[inst] = value
            else:
                del holds[inst]
        return fresh

    # -- state accessors ----------------------------------------------------

    def state(self, dev_id: str) -> dict[str, Any]:
        return (self.data or {}).get(dev_id, {})

    def is_online(self, dev_id: str) -> bool:
        online = self.state(dev_id).get("online")
        return online is not False and online != "false"

    def is_powered(self, dev_id: str) -> bool:
        # Devices without a power capability count as always powered.
        return self.state(dev_id).get("powerSwitch", 1) == 1

    # -- commands -----------------------------------------------------------

    async def send(self, lamp: Lamp, cap_type: str, instance: str, value: Any) -> None:
        """Send a command (LAN when possible) and update state optimistically."""
        ip = self.lan_ip(lamp.device_id) if instance in LAN_INSTANCES else None
        sent = ip is not None and self.lan is not None and self.lan.command(ip, instance, value)
        if not sent:
            try:
                await self.client.control(lamp.sku, lamp.device_id, cap_type, instance, value)
            except GoveeAuthError as err:
                self.config_entry.async_start_reauth(self.hass)
                raise HomeAssistantError("Govee rejected the API key") from err
            except GoveeRateLimitError as err:
                raise HomeAssistantError(
                    "Govee's rate limit was reached. Wait a minute and try again."
                ) from err
            except GoveeApiError as err:
                raise HomeAssistantError(f"Govee command failed: {err}") from err
        if not isinstance(value, dict):
            self._holds.setdefault(lamp.device_id, {})[instance] = (
                value,
                time.monotonic() + HOLD_SECONDS,
            )
            if self.data is not None:
                self.data.setdefault(lamp.device_id, {})[instance] = value
        self.async_update_listeners()
