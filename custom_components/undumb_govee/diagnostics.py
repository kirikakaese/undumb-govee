"""Diagnostics for Undumb Govee (API key redacted)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from . import UndumbConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: UndumbConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), {CONF_API_KEY}),
        "options": dict(entry.options),
        "lamps": {
            dev_id: {
                "profile": asdict(lamp.profile),
                "zones": [asdict(z) for z in lamp.zones],
                "scene_count": len(lamp.scenes),
                "state": coordinator.state(dev_id),
            }
            for dev_id, lamp in coordinator.lamps.items()
        },
    }
