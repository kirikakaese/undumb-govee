"""Govee LAN API client (UDP), used when "LAN Control" is on in the Govee app.

Protocol (documented by Govee):
  * discovery: send {"msg":{"cmd":"scan",...}} to 239.255.255.250:4001 (or a
    lamp's IP on 4001); lamps answer to port 4002 with ip/device/sku.
  * commands go to <ip>:4003: turn, brightness, colorwc, devStatus.
  * devStatus answers arrive on port 4002.

No Home Assistant imports, so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

MULTICAST_GROUP = "239.255.255.250"
SCAN_PORT = 4001
LISTEN_PORT = 4002
COMMAND_PORT = 4003

# Cloud capability instances that the LAN API can handle too.
LAN_INSTANCES = {"powerSwitch", "brightness", "colorRgb", "colorTemperatureK"}


@dataclass
class LanDevice:
    device_id: str
    ip: str
    sku: str


def scan_message() -> bytes:
    return json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()


def command_message(cmd: str, data: dict[str, Any]) -> bytes:
    return json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()


def to_lan_command(instance: str, value: Any) -> tuple[str, dict[str, Any]] | None:
    """Translate a cloud capability command into a LAN command."""
    if instance == "powerSwitch":
        return "turn", {"value": 1 if value else 0}
    if instance == "brightness":
        return "brightness", {"value": int(value)}
    if instance == "colorRgb":
        v = int(value)
        return "colorwc", {
            "color": {"r": (v >> 16) & 0xFF, "g": (v >> 8) & 0xFF, "b": v & 0xFF},
            "colorTemInKelvin": 0,
        }
    if instance == "colorTemperatureK":
        return "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": int(value)}
    return None


def status_to_state(data: dict[str, Any]) -> dict[str, Any]:
    """Translate a devStatus answer into cloud-style {instance: value}."""
    out: dict[str, Any] = {"online": True}
    if "onOff" in data:
        out["powerSwitch"] = 1 if data["onOff"] else 0
    if "brightness" in data:
        out["brightness"] = data["brightness"]
    color = data.get("color")
    if isinstance(color, dict):
        out["colorRgb"] = (
            (int(color.get("r", 0)) << 16) | (int(color.get("g", 0)) << 8) | int(color.get("b", 0))
        )
    if "colorTemInKelvin" in data:
        out["colorTemperatureK"] = data["colorTemInKelvin"]
    return out


def parse_message(raw: bytes) -> tuple[str, dict[str, Any]] | None:
    try:
        msg = json.loads(raw.decode()).get("msg") or {}
    except (UnicodeDecodeError, ValueError, AttributeError):
        return None
    cmd, data = msg.get("cmd"), msg.get("data")
    if not isinstance(cmd, str) or not isinstance(data, dict):
        return None
    return cmd, data


def normalise_id(device_id: str) -> str:
    return device_id.replace(":", "").upper()


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, owner: GoveeLan) -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.owner._on_message(data, addr[0])


class GoveeLan:
    """Shared UDP listener plus helpers for scanning and commands."""

    def __init__(self) -> None:
        self.devices: dict[str, LanDevice] = {}  # normalised id -> device
        self._transport: asyncio.DatagramTransport | None = None
        self._waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        try:
            sock.bind(("", LISTEN_PORT))
        except OSError:
            sock.close()
            raise
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)

    def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
        for futs in self._waiters.values():
            for fut in futs:
                if not fut.done():
                    fut.cancel()
        self._waiters.clear()

    def _send(self, payload: bytes, ip: str, port: int) -> None:
        if self._transport is None:
            raise OSError("LAN listener not running")
        self._transport.sendto(payload, (ip, port))

    def _on_message(self, raw: bytes, sender: str) -> None:
        parsed = parse_message(raw)
        if not parsed:
            return
        cmd, data = parsed
        if cmd == "scan" and data.get("device"):
            dev = LanDevice(str(data["device"]), str(data.get("ip") or sender), str(data.get("sku", "")))
            self.devices[normalise_id(dev.device_id)] = dev
            _LOGGER.debug("LAN found %s (%s) at %s", dev.sku, dev.device_id, dev.ip)
        elif cmd == "devStatus":
            for fut in self._waiters.pop(sender, []):
                if not fut.done():
                    fut.set_result(data)

    async def scan(self, extra_hosts: list[str] | None = None, wait: float = 2.0) -> dict[str, LanDevice]:
        """Ask every lamp on the network (and any given IPs) to identify itself."""
        msg = scan_message()
        targets = [MULTICAST_GROUP, *(extra_hosts or [])]
        for target in targets:
            try:
                self._send(msg, target, SCAN_PORT)
            except OSError as err:
                _LOGGER.debug("LAN scan to %s failed: %s", target, err)
        await asyncio.sleep(wait)
        return self.devices

    def find(self, device_id: str) -> LanDevice | None:
        return self.devices.get(normalise_id(device_id))

    async def status(self, ip: str, timeout: float = 2.0) -> dict[str, Any] | None:
        """Return cloud-style state from a lamp, or None if it didn't answer."""
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(ip, []).append(fut)
        try:
            self._send(command_message("devStatus", {}), ip, COMMAND_PORT)
            data = await asyncio.wait_for(fut, timeout)
        except (OSError, TimeoutError, asyncio.CancelledError):
            waiters = self._waiters.get(ip, [])
            if fut in waiters:
                waiters.remove(fut)
            return None
        return status_to_state(data)

    def command(self, ip: str, instance: str, value: Any) -> bool:
        """Send a cloud-style command over LAN. Returns False if not possible."""
        translated = to_lan_command(instance, value)
        if translated is None:
            return False
        try:
            self._send(command_message(*translated), ip, COMMAND_PORT)
        except OSError as err:
            _LOGGER.debug("LAN command to %s failed: %s", ip, err)
            return False
        return True
