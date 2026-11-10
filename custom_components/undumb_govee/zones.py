"""Work out which separate lights a Govee device has.

Pure Python, no Home Assistant imports. A "zone" is one physical light inside
a lamp. Govee exposes a secondary light either as a range of segments
(segment_color_setting) or as its own toggle (devices.capabilities.toggle).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CAP_ON_OFF = "devices.capabilities.on_off"
CAP_TOGGLE = "devices.capabilities.toggle"
CAP_RANGE = "devices.capabilities.range"
CAP_COLOR = "devices.capabilities.color_setting"
CAP_SEGMENT = "devices.capabilities.segment_color_setting"
CAP_SCENE = "devices.capabilities.dynamic_scene"
CAP_ONLINE = "devices.capabilities.online"

INST_POWER = "powerSwitch"
INST_BRIGHTNESS = "brightness"
INST_RGB = "colorRgb"
INST_CT = "colorTemperatureK"
INST_SEG_RGB = "segmentedColorRgb"
INST_SEG_BRIGHTNESS = "segmentedBrightness"
INST_SCENE = "lightScene"
INST_DIY = "diyScene"

KIND_SEGMENT = "segment"
KIND_TOGGLE = "toggle"

# Toggle instances that are features rather than lights.
NON_LIGHT_TOGGLES = {"gradientToggle", "oscillationToggle", "dreamViewToggle", "sceneStageToggle"}

# Layouts:
#   split      - every light is a run of segments (e.g. floor lamps).
#   main_ring  - the lamp's own power/colour/colour-temp drive the main light,
#                and the segments form a separate ring (e.g. ceiling lights
#                with an RGBWW panel and an RGBIC edge).
LAYOUT_SPLIT = "split"
LAYOUT_MAIN_RING = "main_ring"
LAYOUTS = (LAYOUT_MAIN_RING, LAYOUT_SPLIT)


@dataclass(frozen=True)
class Preset:
    layout: str
    names: tuple[str, ...]
    label: str


# Known lamps. Segment ranges are worked out from what the lamp reports,
# because segment counts vary between hardware revisions. Users can change
# everything in the options flow.
SKU_PRESETS: dict[str, Preset] = {
    "H607C": Preset(LAYOUT_SPLIT, ("Main", "Ring"), "Floor Lamp 2"),
    "H60A4": Preset(LAYOUT_MAIN_RING, ("Ring",), "Square Ceiling Light"),
    "H60A0": Preset(LAYOUT_MAIN_RING, ("Ring",), "Ceiling Light Pro"),
    "H60A1": Preset(LAYOUT_MAIN_RING, ("Ring",), "Ceiling Light"),
}

DEFAULT_SEGMENT_NAMES = ("Main", "Second", "Third", "Fourth")


@dataclass
class Zone:
    """One controllable light inside a lamp."""

    key: str
    name: str
    kind: str
    segments: list[int] = field(default_factory=list)
    instance: str | None = None
    supports_color: bool = False
    supports_brightness: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "segments": format_segments(self.segments)}


@dataclass
class DeviceProfile:
    """What a device can do, parsed from its capability list."""

    sku: str
    device_id: str
    name: str
    has_power: bool = False
    brightness_range: tuple[int, int] | None = None
    has_rgb: bool = False
    color_temp_range: tuple[int, int] | None = None
    segment_count: int = 0
    segment_rgb: bool = False
    segment_brightness: bool = False
    segment_brightness_range: tuple[int, int] = (0, 100)
    light_toggles: list[str] = field(default_factory=list)
    has_scenes: bool = False
    has_diy_scenes: bool = False

    @property
    def has_segments(self) -> bool:
        return self.segment_count > 0 and (self.segment_rgb or self.segment_brightness)


def _range(params: dict[str, Any]) -> tuple[int, int] | None:
    rng = params.get("range")
    if isinstance(rng, dict) and "min" in rng and "max" in rng:
        return int(rng["min"]), int(rng["max"])
    return None


def _segment_info(params: dict[str, Any]) -> tuple[int, tuple[int, int] | None]:
    """Return (segment count, brightness range) from a segment capability."""
    count = 0
    bri: tuple[int, int] | None = None
    for fld in params.get("fields") or []:
        name = fld.get("fieldName")
        if name == "segment":
            elem = fld.get("elementRange") or {}
            if "max" in elem:
                count = max(count, int(elem["max"]) - int(elem.get("min", 0)) + 1)
            else:
                size = fld.get("size") or {}
                if "max" in size:
                    count = max(count, int(size["max"]))
        elif name == "brightness":
            bri = _range(fld)
    return count, bri


def parse_profile(device: dict[str, Any]) -> DeviceProfile:
    """Build a DeviceProfile from one entry of GET /user/devices."""
    prof = DeviceProfile(
        sku=device.get("sku", ""),
        device_id=device.get("device", ""),
        name=device.get("deviceName") or device.get("sku") or "Govee light",
    )
    for cap in device.get("capabilities") or []:
        ctype, inst = cap.get("type"), cap.get("instance")
        params = cap.get("parameters") or {}
        if ctype == CAP_ON_OFF and inst == INST_POWER:
            prof.has_power = True
        elif ctype == CAP_RANGE and inst == INST_BRIGHTNESS:
            prof.brightness_range = _range(params) or (1, 100)
        elif ctype == CAP_COLOR and inst == INST_RGB:
            prof.has_rgb = True
        elif ctype == CAP_COLOR and inst == INST_CT:
            prof.color_temp_range = _range(params) or (2000, 9000)
        elif ctype == CAP_SEGMENT:
            count, bri = _segment_info(params)
            prof.segment_count = max(prof.segment_count, count)
            if inst == INST_SEG_RGB:
                prof.segment_rgb = True
            elif inst == INST_SEG_BRIGHTNESS:
                prof.segment_brightness = True
                if bri:
                    prof.segment_brightness_range = bri
        elif ctype == CAP_TOGGLE and inst and inst not in NON_LIGHT_TOGGLES:
            if "light" in inst.lower():
                prof.light_toggles.append(inst)
        elif ctype == CAP_SCENE and inst == INST_SCENE:
            prof.has_scenes = True
        elif ctype == CAP_SCENE and inst == INST_DIY:
            prof.has_diy_scenes = True
    return prof


def is_light(device: dict[str, Any]) -> bool:
    return device.get("type") == "devices.types.light"


def split_evenly(count: int, parts: int) -> list[list[int]]:
    """Split segment indexes 0..count-1 into `parts` contiguous runs."""
    parts = max(1, min(parts, count))
    base, extra = divmod(count, parts)
    out, start = [], 0
    for i in range(parts):
        size = base + (1 if i < extra else 0)
        out.append(list(range(start, start + size)))
        start += size
    return out


def parse_segments(spec: str, count: int | None = None) -> list[int]:
    """Parse "0-9, 12" into [0..9, 12]. Raises ValueError on bad input."""
    result: set[int] = set()
    for part in spec.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            result.update(range(lo, hi + 1))
        else:
            result.add(int(part))
    if any(i < 0 for i in result):
        raise ValueError("negative segment")
    if count is not None and any(i >= count for i in result):
        raise ValueError(f"segment out of range (lamp has {count})")
    return sorted(result)


def format_segments(segments: list[int]) -> str:
    """Format [0,1,2,5] as "0-2, 5"."""
    segs = sorted(set(segments))
    runs: list[str] = []
    i = 0
    while i < len(segs):
        j = i
        while j + 1 < len(segs) and segs[j + 1] == segs[j] + 1:
            j += 1
        runs.append(str(segs[i]) if i == j else f"{segs[i]}-{segs[j]}")
        i = j + 1
    return ", ".join(runs)


def default_layout(prof: DeviceProfile) -> str:
    preset = SKU_PRESETS.get(prof.sku.upper())
    return preset.layout if preset else LAYOUT_SPLIT


def default_segment_zones(prof: DeviceProfile, layout: str | None = None) -> list[dict[str, Any]]:
    """Suggested segment mapping for a device, in options-flow format."""
    layout = layout or default_layout(prof)
    preset = SKU_PRESETS.get(prof.sku.upper())
    if not prof.has_segments:
        return []
    if layout == LAYOUT_MAIN_RING:
        name = preset.names[0] if preset and preset.layout == LAYOUT_MAIN_RING else "Ring"
        return [{"name": name, "segments": format_segments(list(range(prof.segment_count)))}]
    if prof.segment_count < 2:
        return []
    names = preset.names if preset and preset.layout == LAYOUT_SPLIT else DEFAULT_SEGMENT_NAMES[:2]
    runs = split_evenly(prof.segment_count, len(names))
    return [{"name": n, "segments": format_segments(r)} for n, r in zip(names, runs, strict=False)]


def _slug(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in text).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "zone"


def toggle_name(instance: str) -> str:
    """nightlightToggle -> Nightlight, backgroundLightToggle -> Background light."""
    base = instance[:-6] if instance.endswith("Toggle") else instance
    words, cur = [], ""
    for ch in base:
        if ch.isupper() and cur:
            words.append(cur)
            cur = ch.lower()
        else:
            cur += ch
    if cur:
        words.append(cur)
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def build_zones(
    prof: DeviceProfile, configured: list[dict[str, Any]] | None, layout: str | None = None
) -> list[Zone]:
    """Create Zone objects from the user's mapping (or the default one)."""
    zones: list[Zone] = []
    mapping = configured if configured else default_segment_zones(prof, layout)
    if prof.has_segments:
        used: set[str] = set()
        for entry in mapping:
            try:
                segs = parse_segments(str(entry.get("segments", "")), prof.segment_count)
            except ValueError:
                continue
            name = str(entry.get("name") or "").strip()
            if not segs or not name:
                continue
            key = _slug(name)
            while key in used:
                key += "_2"
            used.add(key)
            zones.append(
                Zone(
                    key=f"seg_{key}",
                    name=name,
                    kind=KIND_SEGMENT,
                    segments=segs,
                    supports_color=prof.segment_rgb,
                    supports_brightness=prof.segment_brightness,
                )
            )
    for inst in prof.light_toggles:
        zones.append(Zone(key=f"toggle_{inst}", name=toggle_name(inst), kind=KIND_TOGGLE, instance=inst))
    return zones


def rgb_to_int(rgb: tuple[int, int, int]) -> int:
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return (r << 16) | (g << 8) | b


def int_to_rgb(value: int) -> tuple[int, int, int]:
    value = int(value)
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def scale(value: float, src: tuple[float, float], dst: tuple[float, float]) -> int:
    """Map value from one range to another, rounding and clamping."""
    s0, s1 = src
    d0, d1 = dst
    if s1 == s0:
        return int(d1)
    out = d0 + (value - s0) * (d1 - d0) / (s1 - s0)
    return int(round(max(min(d0, d1), min(max(d0, d1), out))))


def state_map(capabilities: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a /device/state response into {instance: value}."""
    out: dict[str, Any] = {}
    for cap in capabilities:
        inst = cap.get("instance")
        state = cap.get("state") or {}
        if inst and "value" in state:
            out[inst] = state["value"]
    return out
