"""Config and options flow tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.undumb_govee.api import GoveeAuthError
from custom_components.undumb_govee.const import CONF_DEVICES, CONF_SCAN_INTERVAL, CONF_ZONES, DOMAIN

from .conftest import CEILING_LIGHT, DESK_LAMP, FLOOR_LAMP, PLUG

PATCH = "custom_components.undumb_govee.config_flow.GoveeClient"


def _client(devices=None, error=None):
    client = AsyncMock()
    if error:
        client.get_devices.side_effect = error
    else:
        client.get_devices.return_value = devices if devices is not None else [FLOOR_LAMP, DESK_LAMP, PLUG]
    return client


async def test_full_flow(hass: HomeAssistant) -> None:
    with patch(PATCH, return_value=_client()), patch(
        "custom_components.undumb_govee.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: " abcdefgh12345678 ", CONF_SCAN_INTERVAL: 90}
        )
        assert result["step_id"] == "devices"
        assert result["description_placeholders"] == {"count": "2"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICES: [FLOOR_LAMP["device"]]}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_API_KEY: "abcdefgh12345678"}
    assert result["options"] == {
        CONF_DEVICES: [FLOOR_LAMP["device"]],
        CONF_SCAN_INTERVAL: 90,
        "use_lan": True,
        "lan_hosts": "",
        CONF_ZONES: {},
        "layouts": {},
    }


async def test_invalid_key(hass: HomeAssistant) -> None:
    with patch(PATCH, return_value=_client(error=GoveeAuthError("no"))):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_zone_mapping(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "k"},
        options={CONF_DEVICES: [FLOOR_LAMP["device"]], CONF_SCAN_INTERVAL: 60, CONF_ZONES: {}},
    )
    entry.add_to_hass(hass)
    with patch(PATCH, return_value=_client()), patch(
        "custom_components.undumb_govee.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "pick_lamp"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"lamp": FLOOR_LAMP["device"]})
        assert result["step_id"] == "zones"

        # Overlapping segments are rejected.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"layout": "split", "name_1": "Main", "segments_1": "0-9", "name_2": "Ring", "segments_2": "9-14"},
        )
        assert result["errors"] == {"segments_2": "overlap"}

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"layout": "split", "name_1": "Main", "segments_1": "0-9", "name_2": "Ring", "segments_2": "10-14"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["layouts"] == {FLOOR_LAMP["device"]: "split"}
    assert entry.options[CONF_ZONES] == {
        FLOOR_LAMP["device"]: [{"name": "Main", "segments": "0-9"}, {"name": "Ring", "segments": "10-14"}]
    }


async def test_options_ceiling_light_defaults_to_main_ring(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "k"},
        options={CONF_DEVICES: [CEILING_LIGHT["device"]], CONF_SCAN_INTERVAL: 60, CONF_ZONES: {}},
    )
    entry.add_to_hass(hass)
    with patch(PATCH, return_value=_client([CEILING_LIGHT, FLOOR_LAMP])), patch(
        "custom_components.undumb_govee.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "pick_lamp"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"lamp": CEILING_LIGHT["device"]})
        schema = {str(k): k for k in result["data_schema"].schema}
        assert schema["layout"].default() == "main_ring"
        assert schema["segments_1"].description == {"suggested_value": "0-13"}
        # Switch to the split layout and reset its rows.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"layout": "split", "reset": True}
        )
    assert entry.options["layouts"] == {CEILING_LIGHT["device"]: "split"}
    assert entry.options[CONF_ZONES] == {}
