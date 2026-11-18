# Tuya BLE Mesh for Home Assistant

A local-first Home Assistant integration for Tuya Bluetooth smart bulbs and
related devices.

The project is currently in its **device-identification phase**.  Tuya uses at
least four unrelated protocols that all appear in the app as "Bluetooth mesh":

| Family | Air protocol | What is needed for local control |
| --- | --- | --- |
| **Tuya BLE single** | GATT, service `0xA201` | device UUID + local key |
| **SIG Mesh** (Tuya "Bluetooth Mesh (SIG)") | standard SIG Mesh, GATT proxy `0x1828` or advertising bearer | NetKey, AppKey, IV index, node unicast address |
| **Tuya / Telink mesh** | Telink advertising mesh, often service `0xFE07` | mesh name/password or mesh key, node address |
| **Tuya Beacon v1/v2** | advertising only, 16-bit UUID list or manufacturer data | beacon key, per-device local key, node id, sequence |

Guessing the wrong family cannot work.  This build therefore ships a
diagnostic probe that answers the question from the actual device and from
the authenticated Tuya mobile-cloud session already present in Home
Assistant.

## What the probe does

* Collects Bluetooth advertisements through Home Assistant's Bluetooth fabric
  (the onboard adapter, ESPHome Bluetooth proxies, Shelly, etc.).
* Classifies each advertisement: Tuya BLE single (`0xA201`), SIG Mesh
  provisioning/proxy (`0x1827`/`0x1828`), SIG Mesh message/beacon, Tuya
  Beacon v1 (`0x07D0` manufacturer data), and the 26-byte 16-bit-UUID frames
  used by Tuya Beacon v2 / "Thing Beacon Mesh".
* Reuses the token in the built-in **Tuya** integration to read the raw
  `/v1.0/m/life/users/homes` and `/v1.0/m/life/ha/home/devices` responses.
  This contains the device list, `localKey`, `nodeId`, `meshId`, `meta`, and
  the product identity without asking for the app password.
* Optionally connects once to a device and dumps its GATT services and
  readable characteristics.
* Exposes the result to administrators at
  `GET /api/tuya_ble_mesh/probe`.

**The probe response can contain secret keys.**  It is admin-only and is not
cached.  Do not share the JSON, and remove or disable the integration once
the device family is known.

## Install

This repository is installable as a custom marketplace repository:

1. Settings → Marketplace → Add from link
2. Enter `rplevka/tuya-ble-mesh`
3. Install **Tuya BLE Mesh**
4. Restart Home Assistant
5. Settings → Devices & services → Add integration → **Tuya BLE Mesh**

The initial probe runs on setup.  It takes about a minute (the cloud dump is
fast; the BLE scan is 60 seconds by default).

## Read the result

```bash
curl -s \
  -H "Authorization: Bearer $HASS_TOKEN" \
  http://homeassistant.local:8123/api/tuya_ble_mesh/probe \
  | python3 -m json.tool
```

Useful filters:

```bash
# devices the Tuya cloud knows, with keys and mesh metadata
jq '.cloud.devices' probe.json

# raw mesh-key endpoint attempts
jq '.cloud.mesh_paths' probe.json

# local advertisements, most interesting first
jq '.ble[] | select(.labels | length > 0)' probe.json

# GATT services if a target MAC was configured
jq '.gatt' probe.json
```

## Services

| Service | Purpose |
| --- | --- |
| `tuya_ble_mesh.scan` | Rescan Bluetooth(`scan_seconds`, default 60) |
| `tuya_ble_mesh.cloud_dump` | Re-read the Tuya cloud device list |
| `tuya_ble_mesh.gatt` | Read-only GATT dump of `address` |
| `tuya_ble_mesh.probe` | All of the above |

## Next step

Once the probe identifies the family and provides the keys, the light
platform is added to the same config entry:

* SIG Mesh: local GATT-proxy controller over the Home Assistant Bluetooth
  fabric (works with ESPHome proxies), with NetKey/AppKey and the standard
  Generic OnOff, Light Lightness, Light CTL, and Light HSL models.
* Beacon v2: advertisement sender using the beacon/local keys, sequence
  counter, and group addresses.
* Tuya BLE single: local-key GATT transport.

The long-term goal is no cloud dependency at runtime: the cloud (or a
one-time app data import) is used only to obtain the mesh keys.

## Development

The integration lives in `custom_components/tuya_ble_mesh/`.  `probe.py` is
the reusable protocol/transport layer; the final light platform will build on
its `async_scan_bluetooth`, `async_dump_gatt`, and cloud-session helpers.

## Legal

Not affiliated with Tuya.  Use only on devices you own.  Keys and cloud
sessions are personal credentials; keep them out of logs, issues, and
commits.
