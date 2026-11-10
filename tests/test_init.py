"""End-to-end tests with a fake Govee API."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.undumb_govee.const import CONF_DEVICES, CONF_SCAN_INTERVAL, CONF_ZONES, DOMAIN

from .conftest import DESK_LAMP, FLOOR_LAMP, PLUG


class FakeClient:
    """Records commands and serves canned state."""

    def __init__(self, *_: Any) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.states = {
            FLOOR_LAMP["device"]: [
                {"instance": "online", "state": {"value": True}},
                {"instance": "powerSwitch", "state": {"value": 1}},
                {"instance": "brightness", "state": {"value": 80}},
                {"instance": "colorRgb", "state": {"value": 0xFF8800}},
                {"instance": "colorTemperatureK", "state": {"value": 0}},
            ],
            DESK_LAMP["device"]: [
                {"instance": "powerSwitch", "state": {"value": 1}},
                {"instance": "nightlightToggle", "state": {"value": 0}},
            ],
        }
        self.get_devices = AsyncMock(return_value=[FLOOR_LAMP, DESK_LAMP, PLUG])
        self.get_scenes = AsyncMock(return_value=[{"name": "Sunrise", "value": {"id": 1, "paramId": 2}, "instance": "lightScene"}])

    async def get_state(self, sku: str, device: str) -> list[dict[str, Any]]:
        return self.states[device]

    async def control(self, sku: str, device: str, cap_type: str, instance: str, value: Any) -> None:
        self.calls.append((device, instance, value))
        for cap in self.states[device]:
            if cap["instance"] == instance and not isinstance(value, dict):
                cap["state"]["value"] = value


@pytest.fixture
def fake() -> FakeClient:
    client = FakeClient()
    with patch("custom_components.undumb_govee.GoveeClient", return_value=client):
        yield client


async def _setup(hass: HomeAssistant, options: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "test-key"},
        options=options or {CONF_DEVICES: [], CONF_SCAN_INTERVAL: 60, CONF_ZONES: {}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_created(hass: HomeAssistant, fake: FakeClient) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    ids = sorted(hass.states.async_entity_ids("light"))
    assert ids == [
        "light.desk_lamp",
        "light.desk_lamp_nightlight",
        "light.floor_lamp",
        "light.floor_lamp_main",
        "light.floor_lamp_ring",
    ]
    lamp = hass.states.get("light.floor_lamp")
    assert lamp.state == "on"
    assert lamp.attributes["brightness"] == 204
    assert lamp.attributes["rgb_color"] == (255, 136, 0)
    assert lamp.attributes["effect_list"] == ["Sunrise"]
    ring = hass.states.get("light.floor_lamp_ring")
    assert ring.attributes["segments"] == "8-14"
    assert ring.attributes["undumb_govee_lamp"] == FLOOR_LAMP["device"]


async def test_custom_zone_mapping(hass: HomeAssistant, fake: FakeClient) -> None:
    await _setup(
        hass,
        {
            CONF_DEVICES: [FLOOR_LAMP["device"]],
            CONF_SCAN_INTERVAL: 60,
            CONF_ZONES: {FLOOR_LAMP["device"]: [{"name": "Panel", "segments": "0-11"}, {"name": "Halo", "segments": "12-14"}]},
        },
    )
    assert hass.states.get("light.floor_lamp_panel").attributes["segments"] == "0-11"
    assert hass.states.get("light.floor_lamp_halo") is not None
    assert hass.states.get("light.desk_lamp") is None


async def test_zone_off_and_on(hass: HomeAssistant, fake: FakeClient) -> None:
    await _setup(hass)
    dev = FLOOR_LAMP["device"]

    await hass.services.async_call("light", "turn_off", {"entity_id": "light.floor_lamp_ring"}, blocking=True)
    assert fake.calls[-1] == (dev, "segmentedBrightness", {"segment": list(range(8, 15)), "brightness": 0})
    assert hass.states.get("light.floor_lamp_ring").state == "off"
    assert hass.states.get("light.floor_lamp_main").state == "on"

    fake.calls.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": "light.floor_lamp_ring", "brightness_pct": 50, "rgb_color": [0, 0, 255]},
        blocking=True,
    )
    assert fake.calls == [
        (dev, "segmentedBrightness", {"segment": list(range(8, 15)), "brightness": 50}),
        (dev, "segmentedColorRgb", {"segment": list(range(8, 15)), "rgb": 0x0000FF}),
    ]
    state = hass.states.get("light.floor_lamp_ring")
    assert state.state == "on"
    assert state.attributes["rgb_color"] == (0, 0, 255)


async def test_last_zone_off_powers_lamp_down_and_single_zone_wakes_alone(hass: HomeAssistant, fake: FakeClient) -> None:
    await _setup(hass)
    dev = FLOOR_LAMP["device"]
    for ent in ("light.floor_lamp_main", "light.floor_lamp_ring"):
        await hass.services.async_call("light", "turn_off", {"entity_id": ent}, blocking=True)
    assert fake.calls[-1] == (dev, "powerSwitch", 0)
    assert hass.states.get("light.floor_lamp").state == "off"

    fake.calls.clear()
    await hass.services.async_call("light", "turn_on", {"entity_id": "light.floor_lamp_ring"}, blocking=True)
    assert fake.calls[0] == (dev, "powerSwitch", 1)
    # Main stays dark.
    assert (dev, "segmentedBrightness", {"segment": list(range(0, 8)), "brightness": 0}) in fake.calls
    assert hass.states.get("light.floor_lamp_main").state == "off"
    assert hass.states.get("light.floor_lamp_ring").state == "on"


async def test_toggle_light(hass: HomeAssistant, fake: FakeClient) -> None:
    await _setup(hass)
    assert hass.states.get("light.desk_lamp_nightlight").state == "off"
    await hass.services.async_call("light", "turn_on", {"entity_id": "light.desk_lamp_nightlight"}, blocking=True)
    assert fake.calls[-1] == (DESK_LAMP["device"], "nightlightToggle", 1)
    assert hass.states.get("light.desk_lamp_nightlight").state == "on"


async def test_whole_lamp_commands(hass: HomeAssistant, fake: FakeClient) -> None:
    await _setup(hass)
    dev = FLOOR_LAMP["device"]
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.floor_lamp", "color_temp_kelvin": 3000, "brightness": 255}, blocking=True
    )
    assert (dev, "brightness", 100) in fake.calls
    assert (dev, "colorTemperatureK", 3000) in fake.calls
    assert hass.states.get("light.floor_lamp").attributes["color_mode"] == "color_temp"

    await hass.services.async_call("light", "turn_on", {"entity_id": "light.floor_lamp", "effect": "Sunrise"}, blocking=True)
    assert fake.calls[-1] == (dev, "lightScene", {"id": 1, "paramId": 2})
    assert hass.states.get("light.floor_lamp").attributes["effect"] == "Sunrise"

    await hass.services.async_call("light", "turn_off", {"entity_id": "light.floor_lamp"}, blocking=True)
    assert fake.calls[-1] == (dev, "powerSwitch", 0)


async def test_unload(hass: HomeAssistant, fake: FakeClient) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
