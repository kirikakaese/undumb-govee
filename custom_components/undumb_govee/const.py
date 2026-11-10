"""Constants for Undumb Govee."""

from __future__ import annotations

DOMAIN = "undumb_govee"
MANUFACTURER = "Govee"

API_BASE = "https://openapi.api.govee.com/router/api/v1"

CONF_DEVICES = "devices"
CONF_ZONES = "zones"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_ZONES = 4

# Attributes the bundled Lovelace card uses to find a lamp's lights.
ATTR_LAMP_ID = "undumb_govee_lamp"
ATTR_ROLE = "undumb_govee_role"
ATTR_ZONE_NAME = "zone_name"
ATTR_SEGMENTS = "segments"

CARD_URL = "/undumb_govee/undumb-govee-card.js"
