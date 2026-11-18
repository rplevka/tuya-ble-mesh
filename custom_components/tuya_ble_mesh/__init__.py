"""Tuya BLE Mesh integration.

This module is deliberately small.  The heavy lifting lives in ``probe.py``
while the device protocol is being identified.  It is structured so the
Bluetooth advertisement collector, the Tuya cloud session reader, and the
GATT dump can be reused by the final light platform without changes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_SCAN_SECONDS,
    CONF_TARGET_MAC,
    DOMAIN,
    VIEW_URL,
)
from .probe import (
    async_dump_gatt,
    async_scan_bluetooth,
    cloud_dump,
    run_full_probe,
    tuya_entry_data,
)
from .views import TuyaMeshProbeView

_LOGGER = logging.getLogger(__name__)

SCAN_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_SCAN_SECONDS, default=60): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=300)
        )
    }
)

GATT_SCHEMA = vol.Schema({vol.Required("address"): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the diagnostic entry."""
    hass.data.setdefault(DOMAIN, {})
    bucket: dict[str, Any] = hass.data[DOMAIN].setdefault(
        entry.entry_id,
        {"entry": entry, "result": None, "lock": asyncio.Lock()},
    )

    if not hass.data[DOMAIN].get("view_registered"):
        hass.http.register_view(TuyaMeshProbeView(hass, DOMAIN, VIEW_URL))
        hass.data[DOMAIN]["view_registered"] = True

    async def _run_scan(call: ServiceCall) -> None:
        seconds = int(call.data.get(CONF_SCAN_SECONDS, 60))
        async with bucket["lock"]:
            result = bucket["result"] or {}
            result["ble"] = await async_scan_bluetooth(hass, seconds)
            result["ble_scanned_at"] = time.time()
            bucket["result"] = result
        _LOGGER.warning(
            "Tuya BLE Mesh scan finished: %d advertisements",
            len(bucket["result"].get("ble", [])),
        )

    async def _run_gatt(call: ServiceCall) -> None:
        address = str(call.data["address"]).upper()
        async with bucket["lock"]:
            result = bucket["result"] or {}
            result["gatt"] = await async_dump_gatt(hass, address)
            result[f"gatt_{address}"] = result["gatt"]
            bucket["result"] = result
        _LOGGER.warning("Tuya BLE Mesh GATT dump finished for %s", address)

    async def _run_cloud(call: ServiceCall | None = None) -> None:
        async with bucket["lock"]:
            result = bucket["result"] or {}
            entry_data = tuya_entry_data(hass)
            result["cloud"] = await hass.async_add_executor_job(
                cloud_dump, hass, entry_data
            )
            bucket["result"] = result
        _LOGGER.warning(
            "Tuya BLE Mesh cloud dump finished: %s",
            "ok" if bucket["result"]["cloud"].get("ok") else "error",
        )

    async def _run_all(call: ServiceCall | None = None) -> None:
        async with bucket["lock"]:
            bucket["result"] = await run_full_probe(
                hass,
                int(entry.data.get(CONF_SCAN_SECONDS, 60)),
                entry.data.get(CONF_TARGET_MAC) or None,
            )
        _LOGGER.warning("Tuya BLE Mesh full probe finished")

    if not hass.services.has_service(DOMAIN, "scan"):
        hass.services.async_register(DOMAIN, "scan", _run_scan, schema=SCAN_SCHEMA)
    if not hass.services.has_service(DOMAIN, "gatt"):
        hass.services.async_register(DOMAIN, "gatt", _run_gatt, schema=GATT_SCHEMA)
    if not hass.services.has_service(DOMAIN, "cloud_dump"):
        hass.services.async_register(DOMAIN, "cloud_dump", _run_cloud)
    if not hass.services.has_service(DOMAIN, "probe"):
        hass.services.async_register(DOMAIN, "probe", _run_all)

    hass.async_create_task(run_full_probe_into_bucket(hass, entry, bucket))

    return True


async def run_full_probe_into_bucket(
    hass: HomeAssistant, entry: ConfigEntry, bucket: dict[str, Any]
) -> None:
    """Run the initial probe without blocking setup."""
    try:
        async with bucket["lock"]:
            bucket["result"] = await run_full_probe(
                hass,
                int(entry.data.get(CONF_SCAN_SECONDS, 60)),
                entry.data.get(CONF_TARGET_MAC) or None,
            )
        _LOGGER.warning(
            "Tuya BLE Mesh initial probe finished; see %s", VIEW_URL
        )
    except Exception:  # pragma: no cover - diagnostic only
        _LOGGER.exception("Tuya BLE Mesh initial probe failed")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entry."""
    bucket = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if bucket:
        for task in bucket.get("tasks", []):
            task.cancel()
    return True
