"""Minimal async client for the Govee Platform API v2.

This module has no Home Assistant imports, so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import aiohttp

from .const import API_BASE

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=15)


class GoveeApiError(Exception):
    """Generic API failure."""


class GoveeAuthError(GoveeApiError):
    """The API key was rejected."""


class GoveeRateLimitError(GoveeApiError):
    """Govee returned HTTP 429."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("Govee API rate limit reached")
        self.retry_after = retry_after


class GoveeClient:
    """Talks to openapi.api.govee.com."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        self._session = session
        self._api_key = api_key
        # Govee throttles bursts per device; serialise our own requests.
        self._lock = asyncio.Lock()

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Govee-API-Key": self._api_key, "Content-Type": "application/json"}
        body = None
        if payload is not None:
            body = {"requestId": str(uuid.uuid4()), "payload": payload}
        async with self._lock:
            try:
                async with self._session.request(
                    method, f"{API_BASE}{path}", json=body, headers=headers, timeout=TIMEOUT
                ) as resp:
                    if resp.status in (401, 403):
                        raise GoveeAuthError("Govee rejected the API key")
                    if resp.status == 429:
                        retry = resp.headers.get("Retry-After")
                        raise GoveeRateLimitError(float(retry) if retry and retry.isdigit() else None)
                    try:
                        data = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError) as err:
                        raise GoveeApiError(f"Invalid response (HTTP {resp.status})") from err
            except (TimeoutError, aiohttp.ClientError) as err:
                raise GoveeApiError(f"Cannot reach Govee: {err}") from err

        if not isinstance(data, dict):
            raise GoveeApiError("Unexpected response from Govee")
        code = data.get("code")
        if code in (401, 403):
            raise GoveeAuthError(data.get("message") or data.get("msg") or "Unauthorized")
        if code == 429:
            raise GoveeRateLimitError()
        if code not in (None, 200):
            raise GoveeApiError(f"Govee error {code}: {data.get('message') or data.get('msg')}")
        return data

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return all devices with their capabilities."""
        data = await self._request("GET", "/user/devices")
        devices = data.get("data") or []
        return [d for d in devices if isinstance(d, dict)]

    async def get_state(self, sku: str, device: str) -> list[dict[str, Any]]:
        """Return the capability states of one device."""
        data = await self._request("POST", "/device/state", {"sku": sku, "device": device})
        return (data.get("payload") or {}).get("capabilities") or []

    async def get_scenes(self, sku: str, device: str) -> list[dict[str, Any]]:
        """Return the dynamic scenes (name/value options) a device supports."""
        data = await self._request("POST", "/device/scenes", {"sku": sku, "device": device})
        options: list[dict[str, Any]] = []
        for cap in (data.get("payload") or {}).get("capabilities") or []:
            params = cap.get("parameters") or {}
            for opt in params.get("options") or []:
                if isinstance(opt, dict) and "name" in opt and "value" in opt:
                    options.append(
                        {"name": opt["name"], "value": opt["value"], "instance": cap.get("instance")}
                    )
        return options

    async def control(
        self, sku: str, device: str, cap_type: str, instance: str, value: Any
    ) -> None:
        """Send one capability command."""
        _LOGGER.debug("control %s %s %s=%s", sku, device, instance, value)
        await self._request(
            "POST",
            "/device/control",
            {
                "sku": sku,
                "device": device,
                "capability": {"type": cap_type, "instance": instance, "value": value},
            },
        )
