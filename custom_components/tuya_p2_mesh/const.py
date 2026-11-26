"""Constants for the Tuya P2 Mesh integration.

This module must stay pure Python so the protocol and controller code can be
tested without importing Home Assistant.
"""

from __future__ import annotations

DOMAIN = "tuya_p2_mesh"

CONF_ADAPTER = "adapter"
CONF_SOURCE_ID = "source_id"
CONF_SEQUENCE_SEED = "sequence_seed"
CONF_BEACON_KEY = "beacon_key"
CONF_BULBS = "bulbs"
CONF_MAC = "mac"
CONF_NODE_ID = "node_id"
CONF_LOCAL_KEY = "local_key"
CONF_NAME = "name"
CONF_ADVERTISING_SECONDS = "advertising_seconds"

# Tuya P2 data points.
DP_SWITCH = 1
DP_MODE = 2
DP_BRIGHT = 3
DP_TEMP = 4
DP_COLOR = 11

DEFAULT_ADAPTER = "hci0"
DEFAULT_SOURCE_ID = 0x7D00
DEFAULT_SEQUENCE_SEED = 0x4000
DEFAULT_ADVERTISING_SECONDS = 3.0

# DP 3 and DP 4 use the bulb's native 0..1000 scale.
BRIGHTNESS_MIN = 10
BRIGHTNESS_MAX = 1000
TEMP_MIN = 0
TEMP_MAX = 1000

# Home Assistant colour-temperature limits, in Kelvin.
KELVIN_MIN = 2000
KELVIN_MAX = 6500
