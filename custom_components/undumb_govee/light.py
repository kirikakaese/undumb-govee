"""Light entities: one for the whole lamp, one per light inside it."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import UndumbConfigEntry
from .const import ATTR_LAMP_ID, ATTR_ROLE, ATTR_SEGMENTS, ATTR_ZONE_NAME, DOMAIN, MANUFACTURER
from .coordinator import Lamp, UndumbCoordinator
from .zones import (
    CAP_COLOR,
    CAP_ON_OFF,
    CAP_RANGE,
    CAP_SCENE,
    CAP_SEGMENT,
    CAP_TOGGLE,
    INST_BRIGHTNESS,
    INST_CT,
    INST_POWER,
    INST_RGB,
    INST_SCENE,
    INST_SEG_BRIGHTNESS,
    INST_SEG_RGB,
    KIND_SEGMENT,
    Zone,
    format_segments,
    int_to_rgb,
    rgb_to_int,
    scale,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UndumbConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[LightEntity] = []
    for lamp in coordinator.lamps.values():
        entities.append(GoveeLampLight(coordinator, lamp))
        for zone in lamp.zones:
            if zone.kind == KIND_SEGMENT:
                entities.append(GoveeSegmentLight(coordinator, lamp, zone))
            else:
                entities.append(GoveeToggleLight(coordinator, lamp, zone))
    async_add_entities(entities)


class _Base(CoordinatorEntity[UndumbCoordinator], LightEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: UndumbCoordinator, lamp: Lamp) -> None:
        super().__init__(coordinator)
        self.lamp = lamp
        prof = lamp.profile
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, prof.device_id)},
            name=prof.name,
            manufacturer=MANUFACTURER,
            model=prof.sku,
        )

    @property
    def _state(self) -> dict[str, Any]:
        return self.coordinator.state(self.lamp.device_id)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.is_online(self.lamp.device_id)

    async def _power(self, on: bool) -> None:
        if self.lamp.profile.has_power:
            await self.coordinator.send(self.lamp, CAP_ON_OFF, INST_POWER, 1 if on else 0)


class GoveeLampLight(_Base):
    """The whole lamp: power, global brightness, colour and scenes."""

    _attr_name = None

    def __init__(self, coordinator: UndumbCoordinator, lamp: Lamp) -> None:
        super().__init__(coordinator, lamp)
        prof = lamp.profile
        self._attr_unique_id = prof.device_id
        modes: set[ColorMode] = set()
        if prof.has_rgb:
            modes.add(ColorMode.RGB)
        if prof.color_temp_range:
            modes.add(ColorMode.COLOR_TEMP)
            self._attr_min_color_temp_kelvin, self._attr_max_color_temp_kelvin = prof.color_temp_range
        if not modes:
            modes.add(ColorMode.BRIGHTNESS if prof.brightness_range else ColorMode.ONOFF)
        self._attr_supported_color_modes = modes
        self._scene_by_name = {s["name"]: s["value"] for s in lamp.scenes}
        if self._scene_by_name:
            self._attr_supported_features = LightEntityFeature.EFFECT
            self._attr_effect_list = list(self._scene_by_name)
        self._effect: str | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {ATTR_LAMP_ID: self.lamp.device_id, ATTR_ROLE: "lamp"}

    @property
    def is_on(self) -> bool | None:
        value = self._state.get(INST_POWER)
        return None if value is None else value == 1

    @property
    def brightness(self) -> int | None:
        rng = self.lamp.profile.brightness_range
        value = self._state.get(INST_BRIGHTNESS)
        if not rng or not isinstance(value, (int, float)):
            return None
        return scale(value, rng, (1, 255))

    @property
    def color_mode(self) -> ColorMode:
        modes = self._attr_supported_color_modes
        if len(modes) == 1:
            return next(iter(modes))
        ct = self._state.get(INST_CT)
        if isinstance(ct, (int, float)) and ct > 0:
            return ColorMode.COLOR_TEMP
        return ColorMode.RGB

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        value = self._state.get(INST_RGB)
        return int_to_rgb(value) if isinstance(value, int) else None

    @property
    def color_temp_kelvin(self) -> int | None:
        value = self._state.get(INST_CT)
        return int(value) if isinstance(value, (int, float)) and value > 0 else None

    @property
    def effect(self) -> str | None:
        return self._effect

    async def async_turn_on(self, **kwargs: Any) -> None:
        if not self.is_on:
            await self._power(True)
        prof = self.lamp.profile
        if ATTR_BRIGHTNESS in kwargs and prof.brightness_range:
            await self.coordinator.send(
                self.lamp, CAP_RANGE, INST_BRIGHTNESS,
                scale(kwargs[ATTR_BRIGHTNESS], (1, 255), prof.brightness_range),
            )
        if ATTR_RGB_COLOR in kwargs:
            self._effect = None
            await self.coordinator.send(self.lamp, CAP_COLOR, INST_RGB, rgb_to_int(kwargs[ATTR_RGB_COLOR]))
            if prof.color_temp_range and self.coordinator.data is not None:
                self.coordinator.data.setdefault(self.lamp.device_id, {})[INST_CT] = 0
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._effect = None
            await self.coordinator.send(self.lamp, CAP_COLOR, INST_CT, int(kwargs[ATTR_COLOR_TEMP_KELVIN]))
        if (name := kwargs.get(ATTR_EFFECT)) and name in self._scene_by_name:
            await self.coordinator.send(self.lamp, CAP_SCENE, INST_SCENE, self._scene_by_name[name])
            self._effect = name
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._power(False)


class GoveeSegmentLight(_Base, RestoreEntity):
    """One light made of a range of segments.

    Segments have no power switch, so "off" is brightness 0 and "on" restores
    the last brightness. State is tracked here because Govee doesn't report
    per-segment values back.
    """

    def __init__(self, coordinator: UndumbCoordinator, lamp: Lamp, zone: Zone) -> None:
        super().__init__(coordinator, lamp)
        self.zone = zone
        self._attr_name = zone.name
        self._attr_unique_id = f"{lamp.device_id}_{zone.key}"
        if zone.supports_color:
            self._attr_supported_color_modes = {ColorMode.RGB}
            self._attr_color_mode = ColorMode.RGB
        else:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        self._on = True
        self._bri = 255
        self._rgb: tuple[int, int, int] = (255, 255, 255)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.lamp.zone_entities[self.zone.key] = self
        if (last := await self.async_get_last_state()) is not None:
            self._on = last.state == STATE_ON
            if (bri := last.attributes.get(ATTR_BRIGHTNESS)) is not None:
                self._bri = int(bri)
            if (rgb := last.attributes.get(ATTR_RGB_COLOR)) is not None:
                self._rgb = tuple(rgb)  # type: ignore[assignment]

    async def async_will_remove_from_hass(self) -> None:
        self.lamp.zone_entities.pop(self.zone.key, None)
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_LAMP_ID: self.lamp.device_id,
            ATTR_ROLE: "zone",
            ATTR_ZONE_NAME: self.zone.name,
            ATTR_SEGMENTS: format_segments(self.zone.segments),
        }

    @property
    def is_on(self) -> bool:
        return self._on and self.coordinator.is_powered(self.lamp.device_id)

    @property
    def brightness(self) -> int:
        return self._bri

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._rgb if self.zone.supports_color else None

    async def _set_level(self, zone: Zone, bri: int, rgb: tuple[int, int, int] | None) -> None:
        """Set brightness (0-255) and/or colour on a zone's segments."""
        segs = zone.segments
        if zone.supports_brightness:
            rng = self.lamp.profile.segment_brightness_range
            value = 0 if bri <= 0 else max(1, scale(bri, (1, 255), (max(1, rng[0]), rng[1])))
            await self.coordinator.send(
                self.lamp, CAP_SEGMENT, INST_SEG_BRIGHTNESS, {"segment": segs, "brightness": value}
            )
            if rgb is not None and zone.supports_color:
                await self.coordinator.send(
                    self.lamp, CAP_SEGMENT, INST_SEG_RGB, {"segment": segs, "rgb": rgb_to_int(rgb)}
                )
        elif zone.supports_color:
            # Colour-only segments: dim by scaling the colour itself.
            base = rgb or (255, 255, 255)
            dimmed = tuple(int(c * max(0, bri) / 255) for c in base)
            await self.coordinator.send(
                self.lamp, CAP_SEGMENT, INST_SEG_RGB, {"segment": segs, "rgb": rgb_to_int(dimmed)}
            )

    def _siblings(self) -> list[GoveeSegmentLight]:
        return [e for k, e in self.lamp.zone_entities.items() if k != self.zone.key and isinstance(e, GoveeSegmentLight)]

    async def async_turn_on(self, **kwargs: Any) -> None:
        dev = self.lamp.device_id
        was_powered = self.coordinator.is_powered(dev)
        if ATTR_BRIGHTNESS in kwargs:
            self._bri = max(1, int(kwargs[ATTR_BRIGHTNESS]))
        if ATTR_RGB_COLOR in kwargs:
            self._rgb = tuple(kwargs[ATTR_RGB_COLOR])  # type: ignore[assignment]
        if self._bri <= 0:
            self._bri = 255
        if not was_powered:
            await self._power(True)
            # Lighting one zone of a dark lamp shouldn't wake the others.
            for sib in self._siblings():
                if not sib._on:
                    await sib._set_level(sib.zone, 0, None)
        self._on = True
        await self._set_level(self.zone, self._bri, self._rgb)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._on = False
        await self._set_level(self.zone, 0, None if self.zone.supports_brightness else self._rgb)
        # Every zone dark: switch the lamp off so HA and Apple Home agree.
        if not any(s._on for s in self._siblings()) and not any(
            self.coordinator.state(self.lamp.device_id).get(z.instance) == 1
            for z in self.lamp.zones
            if z.instance
        ):
            await self._power(False)
        self.async_write_ha_state()


class GoveeToggleLight(_Base):
    """A secondary light with its own on/off toggle (e.g. a nightlight)."""

    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, coordinator: UndumbCoordinator, lamp: Lamp, zone: Zone) -> None:
        super().__init__(coordinator, lamp)
        self.zone = zone
        self._attr_name = zone.name
        self._attr_unique_id = f"{lamp.device_id}_{zone.key}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {ATTR_LAMP_ID: self.lamp.device_id, ATTR_ROLE: "zone", ATTR_ZONE_NAME: self.zone.name}

    @property
    def is_on(self) -> bool | None:
        value = self._state.get(self.zone.instance or "")
        return None if value is None else value == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.lamp.profile.has_power and not self.coordinator.is_powered(self.lamp.device_id):
            await self._power(True)
        await self.coordinator.send(self.lamp, CAP_TOGGLE, self.zone.instance or "", 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.send(self.lamp, CAP_TOGGLE, self.zone.instance or "", 0)
