"""Config and options flow for Undumb Govee."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import GoveeApiError, GoveeAuthError, GoveeClient
from .const import (
    CONF_DEVICES,
    CONF_SCAN_INTERVAL,
    CONF_ZONES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_ZONES,
    MIN_SCAN_INTERVAL,
)
from .zones import DeviceProfile, build_zones, default_segment_zones, is_light, parse_profile, parse_segments


def _describe(prof: DeviceProfile) -> str:
    extra = len(build_zones(prof, None))
    lights = f"{extra} lights" if extra >= 2 else ("main + 1 extra light" if extra == 1 else "1 light")
    return f"{prof.name} ({prof.sku}) · {lights}"


async def _fetch(hass, api_key: str) -> list[DeviceProfile]:
    client = GoveeClient(async_get_clientsession(hass), api_key)
    return [parse_profile(d) for d in await client.get_devices() if is_light(d)]


def _interval_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=MIN_SCAN_INTERVAL, max=3600, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
    )


def _device_selector(profiles: list[DeviceProfile]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=p.device_id, label=_describe(p)) for p in profiles],
            multiple=True,
        )
    )


class UndumbGoveeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._api_key: str | None = None
        self._interval = DEFAULT_SCAN_INTERVAL
        self._profiles: list[DeviceProfile] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            try:
                self._profiles = await _fetch(self.hass, key)
            except GoveeAuthError:
                errors["base"] = "invalid_auth"
            except GoveeApiError:
                errors["base"] = "cannot_connect"
            else:
                if not self._profiles:
                    errors["base"] = "no_devices"
                else:
                    await self.async_set_unique_id(key[-8:])
                    self._abort_if_unique_id_configured()
                    self._api_key = key
                    self._interval = int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
                    return await self.async_step_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): _interval_selector(),
                }
            ),
            errors=errors,
        )

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="Govee",
                data={CONF_API_KEY: self._api_key},
                options={
                    CONF_DEVICES: user_input.get(CONF_DEVICES, []),
                    CONF_SCAN_INTERVAL: self._interval,
                    CONF_ZONES: {},
                },
            )
        multi = [p.device_id for p in self._profiles if build_zones(p, None)]
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {vol.Optional(CONF_DEVICES, default=multi or [p.device_id for p in self._profiles]): _device_selector(self._profiles)}
            ),
            description_placeholders={"count": str(len(self._profiles))},
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            try:
                await _fetch(self.hass, key)
            except GoveeAuthError:
                errors["base"] = "invalid_auth"
            except GoveeApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates={CONF_API_KEY: key}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return UndumbGoveeOptionsFlow()


class UndumbGoveeOptionsFlow(OptionsFlow):
    """Change devices, refresh interval and light mapping."""

    def __init__(self) -> None:
        self._profiles: list[DeviceProfile] = []
        self._device: DeviceProfile | None = None

    async def _load(self) -> None:
        if not self._profiles:
            self._profiles = await _fetch(self.hass, self.config_entry.data[CONF_API_KEY])

    def _save(self, **changes: Any) -> ConfigFlowResult:
        options = {**self.config_entry.options, **changes}
        return self.async_create_entry(data=options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "pick_lamp"])

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        try:
            await self._load()
        except GoveeApiError:
            return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            return self._save(
                **{
                    CONF_DEVICES: user_input.get(CONF_DEVICES, []),
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                }
            )
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DEVICES, default=opts.get(CONF_DEVICES, [])): _device_selector(self._profiles),
                    vol.Required(CONF_SCAN_INTERVAL, default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): _interval_selector(),
                }
            ),
        )

    async def async_step_pick_lamp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        try:
            await self._load()
        except GoveeApiError:
            return self.async_abort(reason="cannot_connect")
        segmented = [p for p in self._profiles if p.has_segments and p.segment_count >= 2]
        selected = set(self.config_entry.options.get(CONF_DEVICES) or [])
        if selected:
            segmented = [p for p in segmented if p.device_id in selected]
        if not segmented:
            return self.async_abort(reason="no_segment_lamps")
        if user_input is not None:
            self._device = next(p for p in segmented if p.device_id == user_input["lamp"])
            return await self.async_step_zones()
        return self.async_show_form(
            step_id="pick_lamp",
            data_schema=vol.Schema(
                {
                    vol.Required("lamp"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=p.device_id, label=f"{p.name} ({p.sku}, {p.segment_count} segments)")
                                for p in segmented
                            ]
                        )
                    )
                }
            ),
        )

    async def async_step_zones(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._device is not None
        prof = self._device
        all_zones: dict[str, Any] = dict(self.config_entry.options.get(CONF_ZONES) or {})
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("reset"):
                all_zones.pop(prof.device_id, None)
                return self._save(**{CONF_ZONES: all_zones})
            zones, seen = [], set()
            for i in range(1, MAX_ZONES + 1):
                name = (user_input.get(f"name_{i}") or "").strip()
                spec = (user_input.get(f"segments_{i}") or "").strip()
                if not name and not spec:
                    continue
                if not name or not spec:
                    errors[f"segments_{i}" if name else f"name_{i}"] = "incomplete"
                    continue
                try:
                    segs = parse_segments(spec, prof.segment_count)
                except ValueError:
                    errors[f"segments_{i}"] = "bad_segments"
                    continue
                if seen & set(segs):
                    errors[f"segments_{i}"] = "overlap"
                    continue
                seen |= set(segs)
                zones.append({"name": name, "segments": spec})
            if not zones and not errors:
                errors["base"] = "no_zones"
            if not errors:
                all_zones[prof.device_id] = zones
                return self._save(**{CONF_ZONES: all_zones})

        current = all_zones.get(prof.device_id) or default_segment_zones(prof)
        schema: dict[Any, Any] = {}
        for i in range(1, MAX_ZONES + 1):
            cur = current[i - 1] if i <= len(current) else {}
            schema[vol.Optional(f"name_{i}", description={"suggested_value": cur.get("name", "")})] = str
            schema[vol.Optional(f"segments_{i}", description={"suggested_value": cur.get("segments", "")})] = str
        schema[vol.Optional("reset", default=False)] = bool
        return self.async_show_form(
            step_id="zones",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "name": prof.name,
                "count": str(prof.segment_count),
                "last": str(prof.segment_count - 1),
            },
        )
