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
from .const import DOMAIN
from .zones import DeviceProfile, Zone, state_map

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
    scenes: list[dict[str, Any]] = field(default_factory=list)
    # Zone entities register here so they can coordinate with each other.
    zone_entities: dict[str, Any] = field(default_factory=dict)

    @property
    def device_id(self) -> str:
        return self.profile.device_id

    @property
    def sku(self) -> str:
        return self.profile.sku


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
        self._holds: dict[str, dict[str, tuple[Any, float]]] = {}

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        data: dict[str, dict[str, Any]] = dict(self.data or {})
        errors = 0
        for dev_id, lamp in self.lamps.items():
            try:
                caps = await self.client.get_state(lamp.sku, dev_id)
            except GoveeAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except GoveeRateLimitError as err:
                raise UpdateFailed("Govee rate limit reached, retrying later") from err
            except GoveeApiError as err:
                _LOGGER.debug("State update for %s failed: %s", lamp.profile.name, err)
                errors += 1
                continue
            data[dev_id] = self._apply_holds(dev_id, state_map(caps))
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

    def state(self, dev_id: str) -> dict[str, Any]:
        return (self.data or {}).get(dev_id, {})

    def is_online(self, dev_id: str) -> bool:
        online = self.state(dev_id).get("online")
        return online is not False and online != "false"

    def is_powered(self, dev_id: str) -> bool:
        # Devices without a power capability count as always powered.
        return self.state(dev_id).get("powerSwitch", 1) == 1

    async def send(self, lamp: Lamp, cap_type: str, instance: str, value: Any) -> None:
        """Send a command and update state optimistically."""
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
