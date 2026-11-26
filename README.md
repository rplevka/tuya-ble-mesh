# Tuya P2 Mesh for Home Assistant

Local-only control of Tuya "P2" Bluetooth mesh bulbs from Home Assistant. The
integration sends and receives raw Bluetooth LE advertising frames through a
dedicated HCI adapter. There is no MQTT bridge and **no Tuya cloud access at
runtime**.

> **Status: 0.1.0.** The native transport and light platform are validated on
> the laptop lab described in [docs/test-lab.md](docs/test-lab.md). The NAS
> deployment is prepared but waits for a USB Bluetooth adapter; see
> [docs/nas-deployment.md](docs/nas-deployment.md). Until then the existing
> MQTT bridge remains the production fallback.

## Features

- On/off, brightness, colour temperature (2000–6500 K), and HSV colour.
- One config entry per Bluetooth adapter, with any number of bulbs.
- Config flow with discovery, manual-address fallback, beacon-key capture, and
  a visible ON/OFF test before the entry is created.
- Options flow to add bulbs, plus reconfigure and reauth flows.
- Sequence counter persisted across Home Assistant restarts.
- Diagnostics that redact all key material.
- Local raw-HCI transport; no cloud session, no MQTT, and no external bridge.

## Requirements

- Home Assistant **2026.11** or newer (validated on 2026.11.3).
- A Linux host with the Bluetooth kernel stack and a **dedicated Bluetooth
  adapter** usable through raw HCI. The lab used an Intel AX201 as `hci0`.
- Permission to open `AF_BLUETOOTH`/`BTPROTO_HCI` raw sockets
  (`CAP_NET_RAW`; the validated container recipe also granted `NET_ADMIN`).
- An adapter that supports LE scanning and LE advertising. Extended
  advertising is used when available; legacy advertising is supported.
- The adapter must be powered and not owned exclusively by another process.
  A dedicated dongle is strongly recommended.
- The bulb's Bluetooth address, 16-bit node ID, local key, and the home beacon
  key. These are obtained once from the Tuya app/cloud; the integration itself
  never contacts the cloud.

Home Assistant Bluetooth proxies (ESPHome, Shelly, etc.) are **not** usable as
the radio for this integration: they are scan-only. The integration needs a
local adapter it can advertise from.

### Container installs

HACS installs files into Home Assistant's configuration directory. It cannot
grant the Home Assistant container access to a USB radio. If Home Assistant
runs in Docker/Podman, the container needs the USB device, host networking (on
kernels that only expose the Bluetooth socket family there), `NET_ADMIN`/
`NET_RAW`, and relaxed seccomp or privileged access. The validated recipe and
the `hass-homeassistant-1` compose example are in
[docs/nas-deployment.md](docs/nas-deployment.md).

## Installation

### HACS

This repository is a custom HACS integration repository:

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/rplevka/tuya-ble-mesh` with category
   **Integration**.
3. Install **Tuya P2 Mesh**.
4. Restart Home Assistant.
5. **Settings → Devices & services → Add integration → Tuya P2 Mesh**.

### Manual

Copy `custom_components/tuya_p2_mesh/` into your Home Assistant
`/config/custom_components/` directory and restart Home Assistant. Do not copy
`tools/diagnostics/tuya_ble_mesh/`; it is reference material, not part of this
integration.

## Configuration flow

1. **Radio settings.** Select the adapter dedicated to this integration
   (`hci0`, `hci1`, …), the P2 source ID (default `0x7D00`), the sequence seed
   (default `0x4000`), and the advertising duration (default `0.5` s).
2. **Discover a bulb.** Select a bulb from the ten-second scan, or leave the
   selection empty and enter its Bluetooth address manually. P2 bulbs do not
   advertise while idle, so manual entry is the normal path for bulbs that are
   already installed.
3. **Bulb details.** Enter the Bluetooth address, node ID (hex), local key, and
   a friendly name. The beacon key is optional here.
4. **Beacon key.** If no beacon key was entered, the flow watches for the
   bulb's OFF and ON frames for up to 30 seconds. Toggle the bulb OFF and ON in
   the Tuya app during that window, or enter the beacon key manually.
5. **Test.** The integration sends a P2 ON/OFF test. Confirm that the bulb
   visibly responded before the entry is created.

The options flow (**Configure**) adds more bulbs while reusing the adapter and
beacon key. The reconfigure flow updates the adapter, source ID, beacon key, or
a bulb's local key. Reauth is available when a key changes.

### New bulbs and the Tuya cloud

A bulb must first be paired into the P2 mesh with the Tuya app. The app/cloud is
also the source of the bulb's local key and, on first setup, of the home beacon
key. After that one-time step, all control is local. Pairing a new bulb later
needs the app or cloud again; it does not need cloud access for existing bulbs.

Keep keys out of logs, issues, screenshots, and commits. The integration stores
them in the config entry, and its diagnostics output redacts every key. The
diagnostics still contain bulb addresses, node IDs, and the adapter name;
review it before sharing.

## Grouping

The integration exposes one light entity per bulb. Create a Home Assistant
[light group helper](https://www.home-assistant.io/integrations/light.group/)
(Settings → Devices & services → Helpers → Group → Light) to control several
bulbs together. Native P2 group addresses are not implemented; a group command
is sent as one advertisement per member and the controller serialises radio
access.

## Entity behaviour

P2 is a one-way advertising protocol. The integration does **not** poll the
bulbs, and the bulbs' sparse reports are not a full state source. Entities
therefore use optimistic state (`assumed_state`) and show the last state Home
Assistant sent. If a command is lost, the entity can be out of sync until the
next command.

Commands are sent as one or more frames depending on the number of data points
involved. The default 0.5 s advertising duration keeps a typical colour command
near one second; increasing the duration increases latency proportionally.

## NAS deployment

The production NAS has no Bluetooth adapter. The native deployment requires a
USB Bluetooth dongle with LE advertising support, container device/capability
changes, and the dongle within BLE range of the bulbs. See
[docs/nas-deployment.md](docs/nas-deployment.md) for the hardware checklist,
compose changes for `hass-homeassistant-1`, verification steps, fallback to the
MQTT bridge, and cloud-blocking notes.

The existing bridge remains the fallback. Do not run the bridge and this
integration as competing controllers of the same bulbs with the same source
ID. Start the native sequence counter above the last value used by the bridge,
and disable the bridge once the native path passes live tests.

## Repository layout

```text
custom_components/tuya_p2_mesh/   HACS integration (tuya_p2_mesh)
tools/diagnostics/tuya_ble_mesh/ old cloud/probe diagnostic, reference only
docs/test-lab.md                 validated laptop raw-HCI lab
docs/nas-deployment.md           NAS dongle and compose deployment checklist
tests/                           unit and config-flow tests
```

The diagnostic probe under `tools/diagnostics/` is not installed by HACS. It
reuses a Tuya cloud session to identify device families and can expose secret
keys in its API response; keep it admin-only and do not run it in production
without a reason.

## Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `The Bluetooth adapter could not be opened` | Missing raw-HCI permission, device, or adapter | Grant `CAP_NET_RAW`, pass the USB device into the container, and check `hciN` exists in `/sys/class/bluetooth`. |
| `Address family not supported by protocol` (`EAFNOSUPPORT`) | Container network namespace does not expose `AF_BLUETOOTH` | Use host networking, as in the validated lab recipe. |
| No adapter in the flow | Adapter is down or not visible to the container | Power it on the host, verify `/sys/class/bluetooth`, and restart Home Assistant. |
| No bulbs discovered | Bulbs are idle and do not advertise | Enter the Bluetooth address manually. |
| Test command sends but nothing happens | Wrong address, node ID, local key, beacon key, or out of range | Recheck the details, capture the beacon key again, and move the adapter closer. |
| Every command is slow | Advertising duration too high, or many data points | Lower the advertising duration to 0.5 s; group commands send one frame per bulb. |
| Entity state is wrong | P2 is one-way and optimistic | Send a state-changing command; there is no reliable poll. |
| Adapter disappears after reboot | Dongle re-enumerated or host Bluetooth powered it down | Keep the adapter powered on the host and prefer a stable USB port; re-check `hciN`. |

## Development

Run the unit and config-flow tests:

```bash
python3 -m pytest tests/ -v
```

The tests need `pytest-homeassistant-custom-component` (see
`requirements_test.txt`); the config-flow tests use the Home Assistant test
harness. The laptop lab record, including the validated container recipe, is
in [docs/test-lab.md](docs/test-lab.md).

## Legal

Not affiliated with Tuya. Use only on devices you own. Keys and cloud sessions
are personal credentials; keep them out of logs, issues, and commits.
