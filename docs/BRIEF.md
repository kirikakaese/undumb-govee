# Project brief: undumb-govee

## Context

Govee sells several lamps with **two independently controllable light sources in
one housing**. Examples are the Floor Lamp 2 (H607C), which has a main light plus an
LED ring in the base, uplighter floor lamps with an uplight plus a side light, and
table lamps with a main shade plus a rear ambient light.

In the Govee app you can control each light separately. In Home Assistant, and
in Apple Home through Matter or HomeKit Bridge, these lamps show up as **one
light entity**. You can switch the whole lamp, but the second light can't be
addressed on its own.

## Goal

Build a Home Assistant custom integration, installable through HACS, that
exposes **every light inside a multi-light Govee lamp as its own `light`
entity**, plus a "whole lamp" entity. Every entity must also work through
HomeKit Bridge, so Apple Home finally shows both lights.

## Technical approach

1. **Use the official Govee Platform API v2** (`openapi.api.govee.com`). Each
   device describes its capabilities there. Multi-light lamps expose their
   secondary light in one of two ways:
   - **Segments** (`devices.capabilities.segment_color_setting`, with
     `segmentedColorRgb` / `segmentedBrightness`). Each physical light is a
     range of segment indexes.
   - **Toggles** (`devices.capabilities.toggle`, for example `nightlightToggle`
     or `backgroundLightToggle`). The secondary light has its own on/off switch.
2. **Discover lights from capabilities instead of hard-coding models.** Ship
   presets for known SKUs and fall back to a sensible guess. Let the user edit
   the mapping in an options flow: name each light and assign it segments.
3. **Entities per lamp:**
   - `light.<lamp>`: whole lamp. Power, brightness, RGB, colour temperature,
     and Govee scenes as effects.
   - `light.<lamp>_<zone>` for each light: on/off, brightness, RGB.
     Segment lights emulate "off" as brightness 0 and restore the last level
     when turned back on.
4. **Respect the API limits** (about 10,000 requests per day per key, plus
   per-minute throttles). Use one coordinator per account, a conservative
   poll interval, optimistic state after commands, and backoff on HTTP 429.
5. **Make the UI-first experience good:** a config flow (API key, then pick
   devices), an options flow to map lights, diagnostics with the key
   redacted, and a bundled Lovelace card that shows both lights of a lamp side
   by side.

## Non-goals (for v1)

- LAN/BLE control. The cloud API is the only documented way to address
  segments and secondary toggles across models.
- Non-light Govee devices.

## Done when

- Adding the integration with an API key lists your lamps and creates one
  entity per light.
- Both lights can be switched, dimmed and coloured independently from HA.
- HomeKit Bridge exposes them, so Apple Home shows two tiles.
- The pure logic (capability parsing, zone mapping, payloads) is unit tested.
- The README explains setup, light mapping, the card and Apple Home.
