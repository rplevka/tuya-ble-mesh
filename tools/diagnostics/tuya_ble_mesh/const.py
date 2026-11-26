"""Constants for the Tuya BLE Mesh integration."""

from __future__ import annotations

DOMAIN = "tuya_ble_mesh"
NAME = "Tuya BLE Mesh"
VERSION = "0.1.0"

# The built-in Tuya integration stores the Smart Life / Tuya Smart cloud
# session in this config entry.  Its data is the only place in Home
# Assistant where an already authenticated mobile-cloud session exists.
TUYA_DOMAIN = "tuya"
TUYA_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"

CONF_TARGET_MAC = "target_mac"
CONF_SCAN_SECONDS = "scan_seconds"

DEFAULT_SCAN_SECONDS = 60

# Tuya's BLE service and manufacturer IDs
TUYA_BLE_SERVICE = "0000a201-0000-1000-8000-00805f9b34fb"
TUYA_COMPANY_ID = 0x07D0
SIG_MESH_PROVISIONING_SERVICE = "00001827-0000-1000-8000-00805f9b34fb"
SIG_MESH_PROXY_SERVICE = "00001828-0000-1000-8000-00805f9b34fb"
TELINK_MESH_SERVICE = "0000fe07-0000-1000-8000-00805f9b34fb"

VIEW_URL = "/api/tuya_ble_mesh/probe"
