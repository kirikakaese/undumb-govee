"""Tests for the LAN protocol helpers and listener."""

from __future__ import annotations

import asyncio
import json

from custom_components.undumb_govee.lan import (
    GoveeLan,
    command_message,
    normalise_id,
    parse_message,
    scan_message,
    status_to_state,
    to_lan_command,
)


def test_messages() -> None:
    assert json.loads(scan_message()) == {"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}
    assert json.loads(command_message("turn", {"value": 1})) == {"msg": {"cmd": "turn", "data": {"value": 1}}}


def test_to_lan_command() -> None:
    assert to_lan_command("powerSwitch", 1) == ("turn", {"value": 1})
    assert to_lan_command("brightness", 42) == ("brightness", {"value": 42})
    assert to_lan_command("colorRgb", 0x102030) == (
        "colorwc", {"color": {"r": 16, "g": 32, "b": 48}, "colorTemInKelvin": 0}
    )
    assert to_lan_command("colorTemperatureK", 2700) == (
        "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 2700}
    )
    assert to_lan_command("segmentedColorRgb", {}) is None


def test_status_to_state() -> None:
    assert status_to_state(
        {"onOff": 0, "brightness": 12, "color": {"r": 255, "g": 0, "b": 1}, "colorTemInKelvin": 0}
    ) == {"online": True, "powerSwitch": 0, "brightness": 12, "colorRgb": 0xFF0001, "colorTemperatureK": 0}


def test_parse_message_rejects_garbage() -> None:
    assert parse_message(b"\xff") is None
    assert parse_message(b'{"msg": {"cmd": 1}}') is None
    assert parse_message(b"[]") is None


def test_normalise_id() -> None:
    assert normalise_id("aa:bb:cc") == "AABBCC"


class _Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))

    def close(self) -> None:
        pass


async def test_listener_scan_and_status() -> None:
    lan = GoveeLan()
    transport = _Transport()
    lan._transport = transport  # type: ignore[assignment]

    lan._on_message(
        command_message("scan", {"ip": "192.0.2.5", "device": "0A:0B:0C:0D:0E:0F:10:11", "sku": "H60A4"}),
        "192.0.2.5",
    )
    found = lan.find("0a0b0c0d0e0f1011")
    assert found is not None and found.ip == "192.0.2.5" and found.sku == "H60A4"

    task = asyncio.ensure_future(lan.status("192.0.2.5", timeout=1))
    await asyncio.sleep(0)
    assert transport.sent[-1][1] == ("192.0.2.5", 4003)
    lan._on_message(command_message("devStatus", {"onOff": 1, "brightness": 70}), "192.0.2.5")
    assert await task == {"online": True, "powerSwitch": 1, "brightness": 70}

    # A lamp that doesn't answer times out cleanly.
    assert await lan.status("192.0.2.99", timeout=0.05) is None
    assert lan._waiters.get("192.0.2.99") == []

    assert lan.command("192.0.2.5", "powerSwitch", 0)
    assert json.loads(transport.sent[-1][0])["msg"]["cmd"] == "turn"
    assert not lan.command("192.0.2.5", "segmentedBrightness", {})
