"""Tests for capability parsing and light mapping."""

from __future__ import annotations

import pytest

from custom_components.undumb_govee.zones import (
    KIND_SEGMENT,
    KIND_TOGGLE,
    build_zones,
    default_segment_zones,
    format_segments,
    int_to_rgb,
    parse_profile,
    parse_segments,
    rgb_to_int,
    scale,
    split_evenly,
    state_map,
    toggle_name,
)

from .conftest import DESK_LAMP, FLOOR_LAMP


def test_parse_floor_lamp() -> None:
    prof = parse_profile(FLOOR_LAMP)
    assert prof.sku == "H607C"
    assert prof.has_power and prof.has_rgb and prof.has_scenes
    assert prof.brightness_range == (1, 100)
    assert prof.color_temp_range == (2700, 6500)
    assert prof.segment_count == 15
    assert prof.segment_rgb and prof.segment_brightness
    assert prof.light_toggles == []


def test_parse_toggle_lamp() -> None:
    prof = parse_profile(DESK_LAMP)
    assert prof.light_toggles == ["nightlightToggle"]  # gradientToggle is not a light
    assert not prof.has_segments


def test_default_zones_use_preset_names() -> None:
    zones = default_segment_zones(parse_profile(FLOOR_LAMP))
    assert zones == [{"name": "Main", "segments": "0-7"}, {"name": "Ring", "segments": "8-14"}]


def test_build_zones_custom_mapping() -> None:
    prof = parse_profile(FLOOR_LAMP)
    zones = build_zones(prof, [{"name": "Panel", "segments": "0-9"}, {"name": "Ring", "segments": "10-14"}])
    assert [(z.key, z.kind, z.segments[0], z.segments[-1]) for z in zones] == [
        ("seg_panel", KIND_SEGMENT, 0, 9),
        ("seg_ring", KIND_SEGMENT, 10, 14),
    ]
    assert all(z.supports_color and z.supports_brightness for z in zones)


def test_build_zones_skips_invalid_entries() -> None:
    prof = parse_profile(FLOOR_LAMP)
    zones = build_zones(prof, [{"name": "Bad", "segments": "20-30"}, {"name": "", "segments": "0"}, {"name": "Ok", "segments": "1"}])
    assert [z.name for z in zones] == ["Ok"]


def test_build_zones_toggle() -> None:
    zones = build_zones(parse_profile(DESK_LAMP), None)
    assert len(zones) == 1
    assert zones[0].kind == KIND_TOGGLE
    assert zones[0].name == "Nightlight"
    assert zones[0].instance == "nightlightToggle"


def test_duplicate_zone_names_get_unique_keys() -> None:
    zones = build_zones(parse_profile(FLOOR_LAMP), [{"name": "A", "segments": "0"}, {"name": "A", "segments": "1"}])
    assert zones[0].key != zones[1].key


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("0-3", [0, 1, 2, 3]), ("5, 1-2", [1, 2, 5]), ("3-1", [1, 2, 3]), ("", []), ("4;6", [4, 6])],
)
def test_parse_segments(spec: str, expected: list[int]) -> None:
    assert parse_segments(spec) == expected


@pytest.mark.parametrize("spec", ["a", "1-x", "-1", "15"])
def test_parse_segments_invalid(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_segments(spec, 15)


def test_format_segments() -> None:
    assert format_segments([0, 1, 2, 5, 7, 8]) == "0-2, 5, 7-8"
    assert format_segments([]) == ""


def test_split_evenly() -> None:
    assert split_evenly(5, 2) == [[0, 1, 2], [3, 4]]
    assert split_evenly(2, 4) == [[0], [1]]


def test_color_helpers() -> None:
    assert rgb_to_int((255, 0, 128)) == 0xFF0080
    assert int_to_rgb(0xFF0080) == (255, 0, 128)
    assert rgb_to_int((300, -5, 0)) == 0xFF0000


def test_scale() -> None:
    assert scale(255, (1, 255), (1, 100)) == 100
    assert scale(1, (1, 255), (1, 100)) == 1
    assert scale(128, (1, 255), (0, 100)) == 50
    assert scale(999, (1, 255), (1, 100)) == 100


def test_toggle_name() -> None:
    assert toggle_name("backgroundLightToggle") == "Background light"


def test_state_map() -> None:
    caps = [
        {"instance": "powerSwitch", "state": {"value": 1}},
        {"instance": "brightness", "state": {"value": 40}},
        {"instance": "segmentedColorRgb", "state": {"value": ""}},
        {"instance": "weird"},
    ]
    assert state_map(caps) == {"powerSwitch": 1, "brightness": 40, "segmentedColorRgb": ""}
