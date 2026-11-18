"""Diagnostic probe for Tuya BLE mesh devices.

The probe answers three questions that cannot be answered from the Home
Assistant UI alone:

* What does the bulb advertise?  Tuya sells at least four different things
  under the "Bluetooth mesh" label: Tuya BLE single-point devices, SIG Mesh
  nodes, "Tuya mesh" (Telink) nodes, and the advertisement-only Beacon /
  Beacon Mesh families.
* What did the Tuya mobile cloud know about the device?  The built-in Tuya
  integration stores an authenticated Smart Life / Tuya Smart session; the
  probe reuses it to read the raw home/device payloads, including keys and
  mesh metadata.
* What GATT services does the device expose?  A single read-only connection
  is enough to tell a SIG Mesh proxy (0x1828) from a Tuya BLE server (0xA201).

The result is exposed by ``views.py`` at ``/api/tuya_ble_mesh/probe`` and is
only available to administrators.  It can contain key material.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from .const import (
    SIG_MESH_PROVISIONING_SERVICE,
    SIG_MESH_PROXY_SERVICE,
    TUYA_BLE_SERVICE,
    TUYA_COMPANY_ID,
    TUYA_DOMAIN,
    TUYA_CLIENT_ID,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bluetooth advertisement collection
# ---------------------------------------------------------------------------


def _hex(value: bytes | bytearray | None) -> str:
    return bytes(value).hex() if value else ""


def _parse_ad_structures(raw: bytes | None) -> list[tuple[int, bytes]]:
    """Split a raw advertising payload into (AD type, data) pairs."""
    if not raw:
        return []
    out: list[tuple[int, bytes]] = []
    i = 0
    while i + 1 < len(raw):
        length = raw[i]
        if length == 0:
            break
        end = i + 1 + length
        if end > len(raw):
            break
        out.append((raw[i + 1], bytes(raw[i + 2 : end])))
        i = end
    return out


def _crc8(data: bytes) -> int:
    """CRC-8/SMBUS (poly 0x07, init 0), used by Tuya beacon frames."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _classify(
    service_uuids: list[str],
    manufacturer_data: dict[int, bytes],
    service_data: dict[str, bytes],
    raw: bytes | None,
) -> list[str]:
    """Return the protocol labels that fit this advertisement."""
    labels: list[str] = []
    uuids = {value.lower() for value in service_uuids}

    if TUYA_BLE_SERVICE in uuids or any(
        key.lower() == TUYA_BLE_SERVICE for key in service_data
    ):
        labels.append("tuya_ble_single")
    if SIG_MESH_PROVISIONING_SERVICE in uuids:
        labels.append("sig_mesh_provisioning")
    if SIG_MESH_PROXY_SERVICE in uuids:
        labels.append("sig_mesh_proxy")
    if any(uuid.startswith("0000fe07") for uuid in uuids):
        labels.append("telink_mesh")

    if TUYA_COMPANY_ID in manufacturer_data:
        labels.append("tuya_manufacturer_0x07d0")

    ad = _parse_ad_structures(raw)

    # SIG Mesh messages use AD type 0x2A, beacons 0x2B.  Device UUID and
    # other mesh beacons are useful even without the proxy service.
    if any(kind == 0x2A for kind, _ in ad):
        labels.append("sig_mesh_message")
    if any(kind == 0x2B for kind, _ in ad):
        labels.append("sig_mesh_beacon")

    # Tuya Beacon v2 / Thing Beacon Mesh carry a 26-byte frame in the
    # Complete List of 16-bit Service UUIDs (AD type 0x03).
    uuid_bytes = b"".join(data for kind, data in ad if kind == 0x03)
    if uuid_bytes:
        labels.append("16bit_uuid_list")
        if len(uuid_bytes) == 26:
            header = uuid_bytes[0]
            frame_type = (header >> 3) & 0x0F
            crc_ok = _crc8(uuid_bytes[:25]) == uuid_bytes[25]
            if crc_ok:
                labels.append(f"tuya_beacon_frame_type_{frame_type}")
            if frame_type == 8:
                labels.append("tuya_thing_beacon_mesh_unprovisioned")
            elif frame_type == 1 and header & 0x80:
                labels.append("tuya_p2_unprovisioned")
            elif frame_type == 12 and uuid_bytes[3:5] in (b"\x80\x00", b"\x80\x01"):
                labels.append("tuya_p2_report")
            elif frame_type in (9, 10, 11, 12, 13, 14):
                labels.append("tuya_beacon_activation_or_proxy")

    # A 0x07D0 manufacturer payload with the Tuya BLE flags byte is a
    # single-point device, but the batteries show the same pattern.  The
    # a201 service is the definitive discriminator.
    return labels


def _advertisement_to_dict(info: Any) -> dict[str, Any]:
    manufacturer = getattr(info, "manufacturer_data", None) or {}
    service_data = getattr(info, "service_data", None) or {}
    service_uuids = list(getattr(info, "service_uuids", None) or [])
    raw = getattr(info, "raw", None)
    result = {
        "address": info.address,
        "name": getattr(info, "name", None),
        "rssi": getattr(info, "rssi", None),
        "source": getattr(info, "source", None),
        "connectable": getattr(info, "connectable", None),
        "time": getattr(info, "time", None),
        "service_uuids": service_uuids,
        "manufacturer_data": {
            f"0x{key:04x}": _hex(value) for key, value in manufacturer.items()
        },
        "service_data": {
            key: _hex(value) for key, value in service_data.items()
        },
        "raw": _hex(raw),
        "tx_power": getattr(getattr(info, "advertisement", None), "tx_power", None),
        "labels": _classify(service_uuids, manufacturer, service_data, raw),
    }
    return result


async def async_scan_bluetooth(hass: HomeAssistant, seconds: int = 60) -> list[dict[str, Any]]:
    """Collect advertisements for ``seconds`` and return the newest per address."""
    seen: dict[str, dict[str, Any]] = {}

    @callback
    def _callback(info: Any, _change: Any) -> None:
        entry = _advertisement_to_dict(info)
        previous = seen.get(entry["address"])
        if previous is None or (entry.get("time") or 0) >= (previous.get("time") or 0):
            seen[entry["address"]] = entry

    unregister = bluetooth.async_register_callback(
        hass,
        _callback,
        {},
        bluetooth.BluetoothScanningMode.ACTIVE,
    )
    try:
        await asyncio.sleep(max(1, int(seconds)))
    finally:
        unregister()

    return sorted(
        seen.values(),
        key=lambda item: (
            "tuya" not in " ".join(item["labels"]).lower(),
            -(item.get("rssi") or -999),
        ),
    )


# ---------------------------------------------------------------------------
# Tuya mobile cloud dump
# ---------------------------------------------------------------------------


def _raw(obj: Any) -> Any:
    """Make a JSON-safe copy without hiding any fields."""
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return json.loads(json.dumps(obj, default=str))


def _normalise_device(device: dict[str, Any]) -> dict[str, Any]:
    """Keep the fields that matter for local protocol work."""
    keys = (
        "id",
        "name",
        "devId",
        "productId",
        "product_id",
        "category",
        "localKey",
        "local_key",
        "uuid",
        "nodeId",
        "node_id",
        "gatewayId",
        "gateway_id",
        "meshId",
        "mesh_id",
        "meta",
        "devKey",
        "dev_key",
        "mac",
        "pv",
        "sub",
        "online",
        "capability",
        "protocolAttribute",
        "supportLocal",
        "isSigMesh",
        "isSigMeshWifi",
        "bluetoothCapability",
        "meshCategory",
        "meshCategoryExt",
        "productKey",
        "deviceType",
    )
    return {key: device.get(key) for key in keys if key in device}


def tuya_entry_data(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return the Tuya config-entry data (must be called in the event loop)."""
    entries = hass.config_entries.async_entries(TUYA_DOMAIN)
    if not entries:
        return None
    return dict(entries[0].data)


def cloud_dump(
    hass: HomeAssistant, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Read raw homes/devices through the authenticated Tuya app session.

    This is intentionally executed in the executor.  The ``tuya_sharing``
    package is what the built-in Tuya integration uses for the same account;
    reusing it means no app password is needed and no new cloud session is
    created.
    """
    result: dict[str, Any] = {
        "ok": False,
        "generated": time.time(),
        "homes": None,
        "devices": None,
        "mesh_paths": [],
        "error": None,
    }

    if data is None:
        data = tuya_entry_data(hass)
    if not data:
        result["error"] = "no Tuya config entry"
        return result

    token_info = data.get("token_info")
    if not token_info:
        result["error"] = "the Tuya config entry has no token_info"
        return result

    try:
        from tuya_sharing import Manager  # type: ignore[import-not-found]
    except ImportError as err:  # pragma: no cover - only on minimal installs
        result["error"] = f"tuya_sharing is not available: {err}"
        return result

    try:
        manager = Manager(
            TUYA_CLIENT_ID,
            data.get("user_code", ""),
            data.get("terminal_id", ""),
            data.get("endpoint", ""),
            token_info,
        )
    except Exception as err:  # pragma: no cover - defensive
        result["error"] = f"could not create Tuya manager: {err}"
        return result

    api = manager.customer_api

    def call(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = api.get(path, params)
            return {"path": path, "ok": True, "response": _raw(response)}
        except Exception as err:  # noqa: BLE001 - diagnostics
            return {
                "path": path,
                "ok": False,
                "error": f"{type(err).__name__}: {err}",
            }

    homes = call("/v1.0/m/life/users/homes")
    result["homes"] = homes
    home_list = []
    if homes.get("ok"):
        response = homes.get("response") or {}
        if response.get("success"):
            home_list = response.get("result") or []

    devices: list[dict[str, Any]] = []
    mesh_paths: list[dict[str, Any]] = []
    for home in home_list:
        home_id = home.get("ownerId") or home.get("homeId") or home.get("id")
        if not home_id:
            continue
        home_devices = call(
            "/v1.0/m/life/ha/home/devices", {"homeId": str(home_id)}
        )
        mesh_paths.append(home_devices)
        if home_devices.get("ok"):
            payload = home_devices.get("response") or {}
            if payload.get("success"):
                for device in payload.get("result") or []:
                    if isinstance(device, dict):
                        devices.append(_normalise_device(device))

        # These are the plausible REST paths for the mesh/beacon key
        # material.  A 404/405 is expected on accounts that only have
        # single-point BLE devices; a 200 is the jackpot.
        for suffix in (
            f"/v1.0/m/life/ha/home/{home_id}/mesh",
            f"/v1.0/m/life/ha/home/{home_id}/sigmesh",
            f"/v1.0/m/life/ha/home/{home_id}/beaconmesh",
            f"/v1.0/m/life/ha/home/{home_id}/mesh/list",
        ):
            mesh_paths.append(call(suffix))

    for suffix in (
        "/v1.0/m/life/ha/mesh/list",
        "/v1.0/m/life/ha/sigmesh/list",
        "/v1.0/m/life/ha/beaconmesh/list",
    ):
        mesh_paths.append(call(suffix))

    result["devices"] = devices
    result["mesh_paths"] = mesh_paths
    result["ok"] = bool(devices or home_list)
    return result


# ---------------------------------------------------------------------------
# Read-only GATT dump
# ---------------------------------------------------------------------------


async def async_dump_gatt(hass: HomeAssistant, address: str) -> dict[str, Any]:
    """Connect once, enumerate services, read harmless characteristics."""
    result: dict[str, Any] = {
        "ok": False,
        "address": address,
        "services": [],
        "error": None,
    }

    ble_device = bluetooth.async_ble_device_from_address(hass, address, True)
    if ble_device is None:
        ble_device = bluetooth.async_ble_device_from_address(hass, address, False)
    if ble_device is None:
        result["error"] = "device not known to the Home Assistant Bluetooth fabric"
        return result

    client = None
    try:
        try:
            from bleak_retry_connector import (  # type: ignore[import-not-found]
                BleakClientWithServiceCache,
                establish_connection,
            )

            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.address,
                max_attempts=3,
            )
        except ImportError:  # pragma: no cover - HA always has it
            from bleak import BleakClient

            client = BleakClient(ble_device, timeout=20)
            await client.connect()

        services = []
        safe_uuids = {
            "00002a00-0000-1000-8000-00805f9b34fb",  # Device Name
            "00002a01-0000-1000-8000-00805f9b34fb",  # Appearance
            "00002a24-0000-1000-8000-00805f9b34fb",  # Model Number
            "00002a25-0000-1000-8000-00805f9b34fb",  # Serial Number
            "00002a26-0000-1000-8000-00805f9b34fb",  # Firmware Revision
            "00002a27-0000-1000-8000-00805f9b34fb",  # Hardware Revision
            "00002a28-0000-1000-8000-00805f9b34fb",  # Software Revision
            "00002a29-0000-1000-8000-00805f9b34fb",  # Manufacturer Name
            "00002a50-0000-1000-8000-00805f9b34fb",  # PnP ID
        }
        for service in client.services:
            chars = []
            for char in service.characteristics:
                value = None
                if "read" in char.properties or char.uuid.lower() in safe_uuids:
                    try:
                        raw = await client.read_gatt_char(char)
                        value = _hex(raw)
                    except Exception as err:  # noqa: BLE001 - diagnostic
                        value = f"<read failed: {type(err).__name__}: {err}>"
                chars.append(
                    {
                        "uuid": char.uuid,
                        "handle": char.handle,
                        "properties": list(char.properties),
                        "description": getattr(char, "description", None),
                        "value": value,
                    }
                )
            services.append(
                {
                    "uuid": service.uuid,
                    "handle": service.handle,
                    "description": getattr(service, "description", None),
                    "characteristics": chars,
                }
            )
        result["services"] = services
        result["ok"] = True
    except Exception as err:  # noqa: BLE001 - diagnostic
        result["error"] = f"{type(err).__name__}: {err}"
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - diagnostic
                pass
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_full_probe(
    hass: HomeAssistant, seconds: int = 60, target_mac: str | None = None
) -> dict[str, Any]:
    """Run all probes and return one JSON document."""
    result: dict[str, Any] = {
        "generated": time.time(),
        "cloud": None,
        "ble": None,
        "gatt": None,
        "errors": [],
    }

    try:
        entry_data = tuya_entry_data(hass)
        result["cloud"] = await hass.async_add_executor_job(
            cloud_dump, hass, entry_data
        )
    except Exception as err:  # noqa: BLE001 - diagnostic
        result["errors"].append(f"cloud: {type(err).__name__}: {err}")

    try:
        result["ble"] = await async_scan_bluetooth(hass, seconds)
    except Exception as err:  # noqa: BLE001 - diagnostic
        result["errors"].append(f"ble: {type(err).__name__}: {err}")

    if target_mac:
        try:
            result["gatt"] = await async_dump_gatt(hass, target_mac)
        except Exception as err:  # noqa: BLE001 - diagnostic
            result["errors"].append(f"gatt: {type(err).__name__}: {err}")

    return result
