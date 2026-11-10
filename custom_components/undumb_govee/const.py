"""Constants for Undumb Govee."""

from __future__ import annotations

DOMAIN = "undumb_govee"
MANUFACTURER = "Govee"

API_BASE = "https://openapi.api.govee.com/router/api/v1"

CONF_DEVICES = "devices"
CONF_ZONES = "zones"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_LAYOUTS = "layouts"
CONF_USE_LAN = "use_lan"
CONF_LAN_HOSTS = "lan_hosts"

DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_ZONES = 4

# Rescan the LAN for moved lamps every N refreshes.
LAN_RESCAN_EVERY = 10
# With LAN state available, still ask the cloud every N refreshes (for
# toggles and online status the LAN API doesn't report).
CLOUD_EVERY_WITH_LAN = 10

# Attributes the bundled Lovelace card uses to find a lamp's lights.
ATTR_LAMP_ID = "undumb_govee_lamp"
ATTR_ROLE = "undumb_govee_role"
ATTR_ZONE_NAME = "zone_name"
ATTR_SEGMENTS = "segments"
ATTR_LAYOUT = "undumb_govee_layout"
ATTR_CONNECTION = "connection"

CARD_URL = "/undumb_govee/undumb-govee-card.js"
