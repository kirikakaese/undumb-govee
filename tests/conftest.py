"""Shared fixtures."""

from __future__ import annotations

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


SEG_FIELDS = [
    {"fieldName": "segment", "dataType": "Array", "elementRange": {"min": 0, "max": 14}, "size": {"min": 1, "max": 15}},
]

FLOOR_LAMP = {
    "sku": "H607C",
    "device": "AA:BB:CC:DD:EE:FF:00:11",
    "deviceName": "Floor Lamp",
    "type": "devices.types.light",
    "capabilities": [
        {"type": "devices.capabilities.online", "instance": "online"},
        {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "parameters": {"dataType": "ENUM"}},
        {"type": "devices.capabilities.range", "instance": "brightness", "parameters": {"range": {"min": 1, "max": 100, "precision": 1}}},
        {"type": "devices.capabilities.color_setting", "instance": "colorRgb", "parameters": {"range": {"min": 0, "max": 16777215}}},
        {"type": "devices.capabilities.color_setting", "instance": "colorTemperatureK", "parameters": {"range": {"min": 2700, "max": 6500}}},
        {"type": "devices.capabilities.segment_color_setting", "instance": "segmentedColorRgb",
         "parameters": {"dataType": "STRUCT", "fields": SEG_FIELDS + [{"fieldName": "rgb", "range": {"min": 0, "max": 16777215}}]}},
        {"type": "devices.capabilities.segment_color_setting", "instance": "segmentedBrightness",
         "parameters": {"dataType": "STRUCT", "fields": SEG_FIELDS + [{"fieldName": "brightness", "range": {"min": 0, "max": 100}}]}},
        {"type": "devices.capabilities.dynamic_scene", "instance": "lightScene", "parameters": {"dataType": "ENUM", "options": []}},
    ],
}

DESK_LAMP = {
    "sku": "H6022",
    "device": "11:22:33:44:55:66:77:88",
    "deviceName": "Desk Lamp",
    "type": "devices.types.light",
    "capabilities": [
        {"type": "devices.capabilities.on_off", "instance": "powerSwitch"},
        {"type": "devices.capabilities.range", "instance": "brightness", "parameters": {"range": {"min": 1, "max": 100}}},
        {"type": "devices.capabilities.color_setting", "instance": "colorRgb"},
        {"type": "devices.capabilities.toggle", "instance": "nightlightToggle"},
        {"type": "devices.capabilities.toggle", "instance": "gradientToggle"},
    ],
}

PLUG = {"sku": "H5080", "device": "99", "deviceName": "Plug", "type": "devices.types.socket", "capabilities": []}
