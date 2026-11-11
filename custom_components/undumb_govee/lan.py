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
import base64
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


def build_frame(data: bytes | list[int]) -> bytes:
    """Pad a Govee command to 19 bytes and append the XOR checksum (20 bytes)."""
    raw = bytes(data)
    if len(raw) > 19:
        raise ValueError("Govee frames hold at most 19 data bytes")
    raw = raw.ljust(19, b"\x00")
    checksum = 0
    for byte in raw:
        checksum ^= byte
    return raw + bytes([checksum])


def parse_hex_frame(text: str) -> bytes:
    """Turn "33 30 01 00" (or "333001") into a checksummed frame."""
    cleaned = text.replace(" ", "").replace(":", "").replace(",", "")
    if not cleaned or len(cleaned) % 2:
        raise ValueError("Use pairs of hex digits, like 33 30 01 00")
    return build_frame(bytes.fromhex(cleaned))


def ptreal_message(frames: list[bytes]) -> bytes:
    return command_message(
        "ptReal", {"command": [base64.b64encode(f).decode() for f in frames]}
    )


# Section commands, as used by Govee lamps with separately switchable lights
# (reverse-engineered from the Govee app; see README). <sec> is 1-based.
def section_power(section: int, on: bool) -> bytes:
    return build_frame([0x33, 0x30, section, 0x01 if on else 0x00])


def section_brightness(section: int, pct: int) -> bytes:
    return build_frame([0x33, 0x05, 0x2C, section, 0x02, max(1, min(100, int(pct)))])


def section_color(section: int, rgb: tuple[int, int, int] | None, kelvin: int = 0) -> bytes:
    r, g, b = rgb if rgb is not None else (0xFF, 0xFF, 0xFF)
    k = max(0, min(0xFFFF, int(kelvin)))
    return build_frame([0x33, 0x05, 0x2C, section, 0x01, r, g, b, k >> 8, k & 0xFF])


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

    def _set_multicast_if(self, interface_ip: str) -> None:
        sock = self._transport.get_extra_info("socket") if self._transport else None
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))

    async def scan(
        self,
        extra_hosts: list[str] | None = None,
        wait: float = 2.0,
        interfaces: list[str] | None = None,
    ) -> dict[str, LanDevice]:
        """Ask every lamp on the network (and any given IPs) to identify itself.

        The multicast scan goes out once per network interface. On machines
        with several interfaces (Home Assistant OS has Docker bridges), the
        default one is often not the home network.
        """
        msg = scan_message()
        for iface in interfaces or [None]:
            try:
                if iface:
                    self._set_multicast_if(iface)
                self._send(msg, MULTICAST_GROUP, SCAN_PORT)
            except OSError as err:
                _LOGGER.debug("LAN scan via %s failed: %s", iface or "default interface", err)
        for host in extra_hosts or []:
            try:
                self._send(msg, host, SCAN_PORT)
            except OSError as err:
                _LOGGER.debug("LAN scan to %s failed: %s", host, err)
        await asyncio.sleep(wait)
        return self.devices

    def find(self, device_id: str) -> LanDevice | None:
        """Find a lamp by its cloud device ID.

        The LAN API and the cloud don't always use the same ID length (some
        lamps report only the 6-byte MAC on one side), so fall back to
        matching the MAC at the end.
        """
        key = normalise_id(device_id)
        if key in self.devices:
            return self.devices[key]
        if len(key) < 12:
            return None
        mac = key[-12:]
        for dev_key, dev in self.devices.items():
            if dev_key[-12:] == mac:
                return dev
        return None

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

    def ptreal(self, ip: str, frames: list[bytes]) -> bool:
        """Send raw Govee frames (at most 12 per message)."""
        try:
            for start in range(0, len(frames), 12):
                self._send(ptreal_message(frames[start : start + 12]), ip, COMMAND_PORT)
        except OSError as err:
            _LOGGER.debug("ptReal to %s failed: %s", ip, err)
            return False
        return True

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
