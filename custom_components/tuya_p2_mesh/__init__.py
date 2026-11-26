"""Tuya P2 Mesh integration setup."""

from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from . import p2
from .const import (
    CONF_ADAPTER,
    CONF_ADVERTISING_SECONDS,
    CONF_BEACON_KEY,
    CONF_SEQUENCE_SEED,
    CONF_SOURCE_ID,
    DEFAULT_ADVERTISING_SECONDS,
    DEFAULT_SEQUENCE_SEED,
    DEFAULT_SOURCE_ID,
    DOMAIN,
    SEQUENCE_STORAGE_FILE,
)
from .controller import P2Controller, SequenceError, SequenceStore
from .radio import AsyncP2Radio, P2Radio, RadioError

PLATFORMS = [Platform.LIGHT]


def sequence_path(hass: HomeAssistant) -> Path:
    """Return the shared P2 sequence-counter path."""
    return Path(hass.config.path(SEQUENCE_STORAGE_FILE))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create the radio and controller, then set up the light platform."""
    try:
        adapter = str(entry.data[CONF_ADAPTER])
        source_id = int(entry.data.get(CONF_SOURCE_ID, DEFAULT_SOURCE_ID))
        sequence_seed = int(
            entry.data.get(CONF_SEQUENCE_SEED, DEFAULT_SEQUENCE_SEED)
        )
        advertising_seconds = float(
            entry.data.get(
                CONF_ADVERTISING_SECONDS, DEFAULT_ADVERTISING_SECONDS
            )
        )
        beacon_key = p2.key_bytes(entry.data[CONF_BEACON_KEY])
    except (KeyError, TypeError, ValueError) as err:
        raise ConfigEntryNotReady(
            "Tuya P2 Mesh configuration is invalid"
        ) from err

    try:
        radio = P2Radio(adapter)
    except (RadioError, ValueError) as err:
        raise ConfigEntryNotReady(
            f"Bluetooth adapter {adapter} is invalid"
        ) from err

    try:
        await hass.async_add_executor_job(radio.detect_mode)
    except (RadioError, OSError, ValueError) as err:
        await hass.async_add_executor_job(radio.close)
        raise ConfigEntryNotReady(
            f"Bluetooth adapter {adapter} is not available"
        ) from err

    try:
        sequence = await hass.async_add_executor_job(
            SequenceStore, sequence_path(hass), sequence_seed
        )
        controller = P2Controller(
            AsyncP2Radio(hass, radio),
            beacon_key,
            sequence,
            source_id=source_id,
            advertising_seconds=advertising_seconds,
        )
    except SequenceError as err:
        await hass.async_add_executor_job(radio.close)
        raise ConfigEntryNotReady(str(err)) from err
    except (KeyError, OSError, TypeError, ValueError) as err:
        await hass.async_add_executor_job(radio.close)
        raise ConfigEntryNotReady(
            "The P2 sequence counter could not be loaded; stop Home "
            "Assistant, back up and remove the sequence file, then restart "
            "and recreate the entry with the desired seed."
        ) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    try:
        await hass.config_entries.async_forward_entry_setups(
            entry, PLATFORMS
        )
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await controller.async_close()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the light platform and close the radio."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if not unload_ok:
        return False
    controller = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if controller is not None:
        await controller.async_close()
    return True
