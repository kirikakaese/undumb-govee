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
from custom_components.undumb_govee.lan import LanDevice, normalise_id, status_to_state, to_lan_command

from .conftest import CEILING_LIGHT, DESK_LAMP, FLOOR_LAMP, PLUG


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
            CEILING_LIGHT["device"]: [
                {"instance": "online", "state": {"value": True}},
                {"instance": "powerSwitch", "state": {"value": 1}},
                {"instance": "brightness", "state": {"value": 50}},
                {"instance": "colorTemperatureK", "state": {"value": 4000}},
            ],
            DESK_LAMP["device"]: [
                {"instance": "powerSwitch", "state": {"value": 1}},
                {"instance": "nightlightToggle", "state": {"value": 0}},
            ],
        }
        self.get_devices = AsyncMock(return_value=[FLOOR_LAMP, DESK_LAMP, CEILING_LIGHT, PLUG])
        self.state_calls = 0
        self.get_scenes = AsyncMock(return_value=[{"name": "Sunrise", "value": {"id": 1, "paramId": 2}, "instance": "lightScene"}])

    async def get_state(self, sku: str, device: str) -> list[dict[str, Any]]:
        self.state_calls += 1
        return self.states[device]

    async def control(self, sku: str, device: str, cap_type: str, instance: str, value: Any) -> None:
        self.calls.append((device, instance, value))
        for cap in self.states[device]:
            if cap["instance"] == instance and not isinstance(value, dict):
                cap["state"]["value"] = value


class FakeLan:
    """Pretends lamps listed in `lamps` answer on the LAN."""

    instance: FakeLan | None = None

    def __init__(self) -> None:
        self.lamps: dict[str, dict[str, Any]] = {}  # device id -> devStatus data
        self.commands: list[tuple[str, str, dict[str, Any]]] = []
        self.frames: list[bytes] = []
        self.started = self.stopped = False
        FakeLan.instance = self

    async def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    async def scan(self, hosts=None, wait: float = 2.0):
        return {}

    def find(self, device_id: str) -> LanDevice | None:
        for dev in self.lamps:
            if normalise_id(dev) == normalise_id(device_id):
                return LanDevice(dev, "192.0.2.10", "")
        return None

    async def status(self, ip: str, timeout: float = 2.0):
        return status_to_state(next(iter(self.lamps.values()))) if self.lamps else None

    def ptreal(self, ip: str, frames: list[bytes]) -> bool:
        self.frames.extend(frames)
        return True

    def command(self, ip: str, instance: str, value: Any) -> bool:
        cmd = to_lan_command(instance, value)
        if cmd is None:
            return False
        self.commands.append((ip, *cmd))
        return True


@pytest.fixture
def fake() -> FakeClient:
    client = FakeClient()
    with patch("custom_components.undumb_govee.GoveeClient", return_value=client), patch(
        "custom_components.undumb_govee.GoveeLan", FakeLan
    ):
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
        "light.ceiling_light",
        "light.ceiling_light_ring",
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


async def test_ceiling_light_main_ring(hass: HomeAssistant, fake: FakeClient) -> None:
    await _setup(hass)
    dev = CEILING_LIGHT["device"]
    main = hass.states.get("light.ceiling_light")
    assert main.attributes["undumb_govee_layout"] == "main_ring"
    assert main.attributes["color_temp_kelvin"] == 4000
    ring = hass.states.get("light.ceiling_light_ring")
    assert ring.attributes["segments"] == "0-13"

    # Switching the ring off must leave the main panel on.
    await hass.services.async_call("light", "turn_off", {"entity_id": "light.ceiling_light_ring"}, blocking=True)
    assert fake.calls[-1] == (dev, "segmentedBrightness", {"segment": list(range(14)), "brightness": 0})
    assert (dev, "powerSwitch", 0) not in fake.calls
    assert hass.states.get("light.ceiling_light").state == "on"
    assert hass.states.get("light.ceiling_light_ring").state == "off"


async def test_set_segments_service(hass: HomeAssistant, fake: FakeClient) -> None:
    await _setup(hass)
    dev = CEILING_LIGHT["device"]
    await hass.services.async_call(
        DOMAIN, "set_segments", {"entity_id": "light.ceiling_light_ring", "segments": "0-2"}, blocking=True
    )
    assert fake.calls[-1] == (dev, "segmentedColorRgb", {"segment": [0, 1, 2], "rgb": 0xFF0000})

    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "set_segments", {"entity_id": "light.ceiling_light", "segments": "40"}, blocking=True
        )


async def test_lan_used_for_main_controls(hass: HomeAssistant, fake: FakeClient) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "test-key"},
        options={CONF_DEVICES: [CEILING_LIGHT["device"]], CONF_SCAN_INTERVAL: 60, CONF_ZONES: {}},
    )
    entry.add_to_hass(hass)

    orig_init = FakeLan.__init__

    def init_with_lamp(self) -> None:
        orig_init(self)
        self.lamps[CEILING_LIGHT["device"]] = {
            "onOff": 1, "brightness": 30, "color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 3000,
        }

    with patch.object(FakeLan, "__init__", init_with_lamp):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    lan = FakeLan.instance
    assert lan.started

    main = hass.states.get("light.ceiling_light")
    assert main.attributes["connection"] == "lan"
    assert main.attributes["color_temp_kelvin"] == 3000  # from LAN, not the cloud's 4000

    fake.calls.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.ceiling_light", "brightness_pct": 80, "rgb_color": [255, 0, 0]},
        blocking=True,
    )
    assert lan.commands == [
        ("192.0.2.10", "brightness", {"value": 80}),
        ("192.0.2.10", "colorwc", {"color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0}),
    ]
    assert fake.calls == []  # nothing went to the cloud

    # Ring segments still go through the cloud.
    await hass.services.async_call("light", "turn_off", {"entity_id": "light.ceiling_light_ring"}, blocking=True)
    assert fake.calls[-1][1] == "segmentedBrightness"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert lan.stopped


async def _setup_ceiling_on_lan(hass: HomeAssistant, options: dict[str, Any]) -> FakeLan:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_API_KEY: "test-key"}, options=options)
    entry.add_to_hass(hass)
    orig_init = FakeLan.__init__

    def init_with_lamp(self) -> None:
        orig_init(self)
        self.lamps[CEILING_LIGHT["device"]] = {"onOff": 1, "brightness": 50, "colorTemInKelvin": 4000}

    with patch.object(FakeLan, "__init__", init_with_lamp):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return FakeLan.instance


async def test_lan_sections_layout(hass: HomeAssistant, fake: FakeClient) -> None:
    from custom_components.undumb_govee.lan import section_brightness, section_color, section_power

    lan = await _setup_ceiling_on_lan(
        hass,
        {
            CONF_DEVICES: [CEILING_LIGHT["device"]],
            CONF_SCAN_INTERVAL: 60,
            CONF_ZONES: {},
            "layouts": {CEILING_LIGHT["device"]: "lan_sections"},
        },
    )
    assert hass.states.get("light.ceiling_light_main").attributes["section"] == 1
    assert hass.states.get("light.ceiling_light_ring").attributes["section"] == 2

    await hass.services.async_call("light", "turn_off", {"entity_id": "light.ceiling_light_main"}, blocking=True)
    assert lan.frames == [section_power(1, False)]
    assert hass.states.get("light.ceiling_light_main").state == "off"
    assert hass.states.get("light.ceiling_light_ring").state == "on"
    assert lan.commands == []  # lamp stays powered: the ring is still on

    lan.frames.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": "light.ceiling_light_ring", "brightness_pct": 40, "rgb_color": [255, 0, 128]},
        blocking=True,
    )
    assert lan.frames == [section_power(2, True), section_brightness(2, 40), section_color(2, (255, 0, 128))]

    # Last section off powers the lamp down.
    await hass.services.async_call("light", "turn_off", {"entity_id": "light.ceiling_light_ring"}, blocking=True)
    assert lan.commands[-1] == ("192.0.2.10", "turn", {"value": 0})


async def test_send_lan_frames_service(hass: HomeAssistant, fake: FakeClient) -> None:
    from custom_components.undumb_govee.lan import section_power

    lan = await _setup_ceiling_on_lan(
        hass, {CONF_DEVICES: [CEILING_LIGHT["device"]], CONF_SCAN_INTERVAL: 60, CONF_ZONES: {}}
    )
    await hass.services.async_call(
        DOMAIN, "send_lan_frames", {"entity_id": "light.ceiling_light", "frames": ["33 30 02 00"]}, blocking=True
    )
    assert lan.frames == [section_power(2, False)]

    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "send_lan_frames", {"entity_id": "light.ceiling_light", "frames": ["xyz"]}, blocking=True
        )


async def test_section_light_needs_lan(hass: HomeAssistant, fake: FakeClient) -> None:
    await _setup(
        hass,
        {
            CONF_DEVICES: [CEILING_LIGHT["device"]],
            CONF_SCAN_INTERVAL: 60,
            CONF_ZONES: {},
            "layouts": {CEILING_LIGHT["device"]: "lan_sections"},
        },
    )
    assert hass.states.get("light.ceiling_light_main").state == "unavailable"


async def test_snapshots_are_effects(hass: HomeAssistant, fake: FakeClient) -> None:
    import copy

    lamp = copy.deepcopy(CEILING_LIGHT)
    lamp["capabilities"].append(
        {
            "type": "devices.capabilities.dynamic_scene",
            "instance": "snapshot",
            "parameters": {"dataType": "ENUM", "options": [{"name": "Ring only", "value": 4242}]},
        }
    )
    fake.get_devices.return_value = [lamp]
    await _setup(hass)
    state = hass.states.get("light.ceiling_light")
    assert state.attributes["effect_list"] == ["Snapshot: Ring only"]
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.ceiling_light", "effect": "Snapshot: Ring only"}, blocking=True
    )
    assert fake.calls[-1] == (CEILING_LIGHT["device"], "snapshot", 4242)
