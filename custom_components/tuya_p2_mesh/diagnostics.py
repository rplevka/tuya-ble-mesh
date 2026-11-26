"""Diagnostics support for the Tuya P2 Mesh integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:  # Imported as part of the integration package.
    from .const import (
        CONF_ADAPTER,
        CONF_BEACON_KEY,
        CONF_BULBS,
        CONF_LOCAL_KEY,
        CONF_MAC,
        CONF_NAME,
        CONF_NODE_ID,
        CONF_SOURCE_ID,
    )
except ImportError:  # Imported directly by the unit tests.
    from const import (
        CONF_ADAPTER,
        CONF_BEACON_KEY,
        CONF_BULBS,
        CONF_LOCAL_KEY,
        CONF_MAC,
        CONF_NAME,
        CONF_NODE_ID,
        CONF_SOURCE_ID,
    )

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_REDACTED = "**REDACTED**"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return config entry diagnostics with all key material redacted."""
    data = entry.data
    bulbs = [
        {
            CONF_NAME: bulb.get(CONF_NAME),
            CONF_MAC: bulb.get(CONF_MAC),
            CONF_NODE_ID: bulb.get(CONF_NODE_ID),
            CONF_LOCAL_KEY: _REDACTED,
        }
        for bulb in data.get(CONF_BULBS, [])
    ]
    return {
        CONF_ADAPTER: data.get(CONF_ADAPTER),
        CONF_SOURCE_ID: data.get(CONF_SOURCE_ID),
        CONF_BEACON_KEY: _REDACTED,
        CONF_BULBS: bulbs,
    }
