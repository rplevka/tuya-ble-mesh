# Tuya P2 Mesh — Native Local Bluetooth Integration

Status: Proposed for approval
Date: 2026-11-26
Repository: `rplevka/tuya-ble-mesh`

## 1. Intent

Build a native Home Assistant integration for the two Tuya RGBCCT 7002
bulbs that talks to them directly over local Bluetooth Low Energy
advertisements. The integration must provide a configuration wizard for
adding bulbs and keys, expose full light controls, support grouping from
Home Assistant, and require no Tuya cloud at runtime.

The first implementation and validation happen in a minimal Home
Assistant container on the development laptop using its internal Intel
AX201 adapter. Production will run the same integration inside the NAS
Home Assistant container after a USB Bluetooth adapter is added.

## 2. Why

The bulbs are Tuya Beacon Mesh P2 devices: advertisement-only, one-way,
with no GATT service and no cloud connection of their own.

The existing MQTT bridge on the laptop proved the protocol and provides
working local control today, but it is an external process and lacks the
requested native configuration flow. The Tuya Bluetooth Gateway was
tested and is not a P2 mesh gateway; the cloud association it created did
not change the physical mesh and cannot control the bulbs. The NAS Home
Assistant container has no Bluetooth adapter and only scan-only
Shelly/ESPHome proxies, so a native NAS solution requires a USB Bluetooth
adapter in the Home Assistant container. That is the chosen production
path.

## 3. Goals

- A native Home Assistant custom integration, installable via HACS or a
  manual copy into `custom_components`.
- Raw HCI Bluetooth transport: advertising and scanning from the HA
  process without BlueZ D-Bus or MQTT.
- Configuration flow covering adapter selection, bulb discovery, local
  key entry, beacon key capture, test transmission, and entry creation.
- An add-device flow that reuses the home beacon key and adds another
  bulb.
- Light entities supporting on/off, brightness, white colour temperature,
  and RGB/HS colour.
- Native Home Assistant grouping via a light group helper.
- Unit tests for protocol, key recovery, DP encoding, and flow logic, plus
  a live hardware test in the container lab.
- Runtime without the Tuya cloud.

## 4. Non-goals

- Fully offline pairing of a brand-new bulb. Tuya’s pairing flow requires
  the mobile app/cloud once per bulb; the integration starts after that.
- A Tuya Cloud QR-login implementation in v1. The local key is entered or
  imported by the user; cloud API calls are not part of the integration.
- Tuya gateway transport.
- P2 group addresses. Grouping is done by Home Assistant.
- Multiple Home Assistant instances or a remote bridge API.
- Controlling devices other than the supported P2 light family.

## 5. Anti-goals

- Hard-coding keys, MAC addresses, or home identifiers in the source.
- Breaking or replacing the working bridge before the native integration
  passes its live tests.
- Resetting or re-pairing the bulbs, which would change their keys.
- Any runtime dependency on Tuya cloud, MQTT, or an external bridge.
- Using the scan-only Home Assistant Bluetooth proxies for transmission.
- Logging secrets or raw key material.

## 6. Constraints and findings

- P2 frames are 26 bytes carried as an AD 0x03 complete list of 16-bit
  service UUIDs. The advertisement must begin with the Flags element
  `02 01 06`; flags-last advertisements are ignored by the bulbs.
- Frame layout: header, source, destination, sequence, subcommand,
  plaintext CRC, 16-byte XXTEA ciphertext XOR the 16-byte beacon key, and
  an outer CRC. DP writes use frame type 1 and subcommand 5.
- The beacon key is a home-wide XOR mask. It can be recovered from one
  known OFF or ON frame using the bulb’s local key and the known DP 1
  payload. Two independent captures must agree.
- DP mapping: 1 switch (bool), 2 mode (enum), 3 brightness (4-byte value,
  10..1000), 4 colour temperature (4-byte value, 0..1000), 11 colour
  (4-byte raw HSV: H 2 bytes big-endian, S 1 byte, V 1 byte, both 0..100).
- The bulbs accept controller source ID `0x7D00`. Other tested sources
  (`0x7D01`, `0x7FFE`) were ignored by the bulbs. The source ID and
  sequence counter are therefore configurable, defaulting to `0x7D00`.
- Sequence numbers are per source and must keep increasing. The counter
  must be persisted before transmission.
- P2 is one-way. The bulbs send only sparse reports/heartbeats, not full
  DP state. Entity state is optimistic unless a report is decoded.
- The gateway association in the Tuya cloud is metadata only. The bulbs
  still use their original physical P2 mesh keys. They must not be reset
  or re-paired during this work.
- Development laptop: Intel AX201, `hci0`, Podman 5.8.4, passwordless
  sudo. The existing `tuya-p2-bridge.service` owns the adapter during
  normal operation and must be stopped while the test container owns it.
- NAS: Home Assistant container `hass-homeassistant-1`, image
  `homeassistant/home-assistant:stable`, no Bluetooth adapter, only
  scan-only Shelly/ESPHome proxies. The NAS kernel has the Bluetooth core
  and `btusb` available. A USB adapter is required for Path A.
- Home Assistant version: 2026.11.x, Python 3.14 in the container.
- Raw HCI inside a container requires the adapter device to be passed in,
  plus `NET_ADMIN`/`NET_RAW`; the seccomp profile may need to be relaxed.
  The exact working recipe will be established in the laptop lab.

## 7. Approaches considered

### A. Native integration inside Home Assistant with a USB Bluetooth adapter

The integration owns a local adapter, sends P2 advertisements through raw
HCI, and scans for reports. On the NAS, a USB Bluetooth dongle is passed
into the Home Assistant container. No external bridge or MQTT.

Trade-offs: best user experience and no extra always-on device; requires
a USB adapter, container device/capability changes, and the NAS must be
within BLE range of the bulbs.

### B. Native integration plus an always-on bridge process

Home Assistant hosts the UI and entities; a small bridge on a laptop or
Raspberry Pi owns the BLE radio and communicates over MQTT or HTTP.

Trade-offs: no NAS hardware changes, reuses the working bridge; still
requires an always-on external device and adds a second process to
maintain. Not chosen because the user selected Path A.

### C. Tuya Bluetooth Gateway over LAN

Reuse the gateway LAN protocol to control gateway sub-devices.

Rejected: the gateway does not control these P2 beacon bulbs. The cloud
association succeeded, but app and LAN control both fail.

### D. Home Assistant Bluetooth proxies

Use the existing Shelly/ESPHome proxies for discovery and transmission.

Rejected for transmission: the proxies are scan-only. They remain useful
only for diagnostics or future discovery assistance.

**Chosen: A.** The laptop lab validates the native transport now; the NAS
deployment follows when a USB adapter is available.

## 8. Architecture

### 8.1 Package layout

```text
custom_components/tuya_p2_mesh/
  __init__.py
  manifest.json
  const.py
  config_flow.py
  controller.py
  light.py
  p2.py
  radio.py
  diagnostics.py
  strings.json
  translations/en.json
```

No external Python requirements. `radio.py` is adapted from the MIT
raw-HCI reference implementation with attribution.

### 8.2 Transport

- A `P2Radio` class owns one adapter (`hci0` by default) and a lock.
- It enumerates adapters from `/sys/class/bluetooth` and validates that
  the process can open an HCI control socket.
- Advertising sends the full 31-byte payload
  `02 01 06 1b 03 <26-byte frame>` using HCI Management/LE commands.
- Scanning reads legacy and extended advertising reports and extracts the
  26-byte AD 0x03 payload.
- Advertising and scanning are serialized; the class stops its own
  advertising before scanning and restores it afterwards.
- Blocking HCI operations run in an executor; the HA event loop is never
  blocked.
- The adapter is expected to be dedicated to this integration. If Home
  Assistant’s Bluetooth integration also uses it, the conflict is
  documented and the integration reports an error rather than silently
  fighting for the adapter.

### 8.3 Protocol module

`p2.py` provides:

- CRC-8/SMBUS
- Tuya’s big-endian XXTEA
- frame build and parse
- DP TLV encode/decode
- beacon key recovery from known OFF/ON frames
- key parsing: 16 ASCII characters or 32 hex digits

The module is ported from the verified `tools/p2.py` implementation and
covered by unit tests with known vectors and real captured frames.

### 8.4 Configuration model

One config entry represents one adapter/home:

```text
adapter: hci0
source_id: 0x7D00
beacon_key: <16 bytes>
bulbs:
  - mac: <BLE address>
    local_key: <16 bytes>
    node_id: <16-bit>
    name: <friendly name>
```

The beacon key is shared by all bulbs in the home. Each bulb has its own
local key and node ID. Keys are stored in the config entry (Home
Assistant’s protected `.storage`) and are never logged or committed.

### 8.5 Configuration flow

The flow does not call the Tuya cloud.

1. **Adapter and source**
   - Select an HCI adapter.
   - Confirm the source ID (default `0x7D00`) and advertising duration.
   - Validate raw HCI access.
2. **Discover bulb**
   - Scan for P2 advertisements for 10–30 seconds.
   - List discovered devices with MAC, node ID when visible, and RSSI.
   - Provide a manual MAC entry fallback.
3. **Keys**
   - Enter the bulb’s local key (from the Tuya app or developer portal).
   - If this is the first bulb, enter the home beacon key or capture it:
     - Instruct the user to toggle the bulb OFF and ON in the Tuya app.
     - Capture both frames, derive the beacon key from each using the
       known DP 1 payloads, and accept only if both derivations match.
   - For later bulbs, reuse the stored beacon key and validate it against
     a captured frame if available.
4. **Test**
   - Send a DP 1 ON/OFF through the selected adapter.
   - Ask the user to confirm the bulb responded.
   - Create the config entry.
5. **Add device** (options flow)
   - Reuse the adapter and beacon key.
   - Scan, enter the new bulb’s local key, capture/validate, test, add.
6. **Reconfigure / reauth**
   - Update the adapter, source, beacon key, or a bulb’s local key.

The local key must be obtained once from Tuya’s app or developer portal.
The integration itself never talks to the cloud.

### 8.6 Light entities

One `light` entity per bulb:

- `supported_color_modes`: `{HS, COLOR_TEMP}`
- `min_color_temp_kelvin = 2000`, `max = 6500`
- `assumed_state = True`
- `unique_id` based on the bulb MAC
- Device info with the bulb name and model

Commands:

- `turn_on` with colour: DP 2 = 1, DP 11 = HSV, then DP 1 = true.
- `turn_on` with colour temperature: DP 2 = 0, DP 4 = value, then DP 1.
- `turn_on` with brightness only: DP 3 = value, then DP 1.
- `turn_off`: DP 1 = false.
- Brightness maps 0..255 to DP 3 10..1000.
- Colour temperature maps mireds 153..500 to DP 4 1000..0.
- HS colour uses H 0..360, S 0..100, V from brightness (0..100).

Each command group is sent as a separate frame with its own sequence
number. The entity updates optimistically after transmission. If a report
is decoded, it updates the entity state.

### 8.7 Grouping

Use Home Assistant’s native light group helper:

Settings → Devices & services → Helpers → Group → Light.

The integration documents this and may include a one-click “create group”
service later. P2 group addresses are not used in v1.

### 8.8 Availability and errors

The entity is unavailable when the adapter cannot be opened or the
controller has not completed setup. Adapter errors are logged with the
adapter name and no key material. A failed advertisement attempt is
surfaced as an entity update error rather than silently ignored.

### 8.9 Diagnostics

Diagnostics include adapter, source ID, bulb MACs, node IDs, names, last
seen time, and configured durations. Keys, raw decrypted payloads, and
secret material are redacted.

### 8.10 Tests

Unit tests:

- XXTEA known vectors
- CRC-8 vectors
- frame build/parse round trips
- beacon key recovery from captured OFF and ON frames
- DP encode/decode for DP 1/2/3/4/11
- config flow schema and validation with fake data
- radio command construction with a fake HCI socket

Live tests:

- Minimal HA container on the laptop with the AX201.
- Manual key entry for the existing bulbs.
- On/off, brightness, colour temperature, and RGB colour.
- Group helper behaviour.
- Container capability/seccomp recipe documented for the NAS.

## 9. Laptop test lab

- Stop `tuya-p2-bridge.service` so the test container owns `hci0`.
- Run a minimal Home Assistant container with Podman, port 8124, a
  separate config directory, the integration mounted into
  `/config/custom_components`, the internal USB Bluetooth device passed
  through, `NET_ADMIN`/`NET_RAW`, and relaxed seccomp/privileged access as
  needed.
- Onboard the test instance, add the integration, and validate raw HCI
  access before adding bulbs.
- Validate the manual key path first; the capture path is covered by unit
  tests and can be exercised with a future bulb.
- After testing, stop the container and restart the production bridge.
- The production NAS Home Assistant is not modified during the lab test;
  its bridge-backed entities are temporarily unavailable while the host
  bridge is stopped.

## 10. Production NAS deployment

- Add a Linux-friendly USB Bluetooth adapter with LE advertising support
  and, if needed, an extension cable/antenna for range.
- Update the `hass-homeassistant-1` compose configuration:
  - pass the Bluetooth USB device into the container
  - add `NET_ADMIN` and `NET_RAW`
  - relax seccomp or use privileged access if required
- Deploy `custom_components/tuya_p2_mesh` to `/config/custom_components`
  and restart Home Assistant.
- Add the integration with the adapter selected (`hci0` or the dongle’s
  adapter).
- Keep the existing MQTT bridge as a fallback until the native integration
  passes live tests; then disable the bridge service.
- The NAS currently has no Bluetooth adapter, so this phase waits for the
  hardware.

## 11. Work split

- **W1 — Protocol core and tests**: port `p2.py`, add unit tests for
  crypto, frames, DP encoding, and key recovery.
- **W2 — Radio transport and tests**: adapt the raw-HCI advertiser/scanner,
  add locking, adapter enumeration, fake-socket tests.
- **W3 — Integration scaffold and config flow**: manifest, constants,
  config entry, options flow, storage, reauth.
- **W4 — Light platform and controller**: entities, command sequencing,
  optimistic state, availability, diagnostics, translations.
- **W5 — Test lab**: Podman container, onboarding, raw-HCI validation,
  live bulb tests, capability recipe.
- **W6 — Documentation and packaging**: HACS metadata, README, migration
  notes, deployment instructions.

W1 and W2 can proceed in parallel. W3 depends on their interfaces. W4
depends on W3. W5 can start once the component skeleton loads. W6 follows
successful live tests.

## 12. Risks and open items

- Raw HCI inside the Home Assistant container may require more privileges
  than expected; the laptop lab will establish the minimum working recipe.
- BlueZ and the integration may contend for the same adapter. A dedicated
  adapter is preferred; the lab must test coexistence.
- The bulbs’ source-ID filtering is not fully documented. `0x7D00` is the
  known working source; the integration keeps it configurable.
- Sequence counter conflicts with the existing bridge/app must be handled
  during migration. The integration will start above the last known
  bridge sequence and persist its own counter.
- P2 is one-way; state is optimistic. Reports may not provide full DP
  state.
- NAS BLE range with a USB dongle is unknown until hardware is installed.
- New bulbs still require one-time Tuya app/cloud pairing and local-key
  retrieval. The integration does not remove that dependency.
- The gateway cloud records are misleading but harmless; the bulbs must
  not be reset or re-paired during this work.

## 13. Builder-check resolutions

These points were raised by a fresh reviewer and are settled here.

### 13.1 Source ID and sequence ownership

The integration is the **only transmitter from source `0x7D00`** after
cutover. The existing `tuya-p2-bridge` service must be stopped and
disabled while the integration runs. The Tuya app may continue to use
`0x7D01`; the bulbs ignore that source, so it does not conflict.

Migration rule:

- The config flow offers a sequence seed:
  - import the existing bridge counter from its
    `tuya_p2_seq.json`, or
  - enter a starting value manually.
- The default seed is `0x4000`, above the observed bridge range.
- The integration persists its counter before every transmission.
- Running the bridge and the integration against the same bulbs at the
  same time is unsupported; the design and docs state this explicitly.
- The lab test order is: stop the bridge, seed/import the counter, test
  the integration, then leave the bridge stopped while the integration
  owns the adapter.

### 13.2 Beacon-key capture frames

Capture the **app’s outbound DP write frames**, not the bulbs’ reports.

- Filter: destination equals the bulb’s node ID; source is not the bulb;
  frame type 1; subcommand 5; plaintext CRC matches DP 1 OFF or ON.
- In the verified captures, the app’s OFF/ON commands are DP1-only
  (`01 11 00` / `01 11 01`), so the full 16-byte plaintext is known.
- The phone must be near the HA adapter while the user toggles the bulb.
- Both OFF and ON derivations must agree before the beacon key is
  accepted.
- If the app sends a multi-DP/full-state frame, capture is not possible
  with this method; the flow falls back to manual beacon-key entry.
- A manually entered beacon key can be validated against any captured
  app command.

### 13.3 Discovery and node ID

Passive discovery is **best-effort**. The bulbs are quiet and may not
advertise during a 10–30 second scan.

- The flow requires a MAC address and a node ID, either manually or from
  a capture.
- Node IDs can be obtained from the Tuya app/cloud for bound devices, or
  from the destination field of an app command captured while the user
  toggles the bulb.
- The manual-MAC path therefore also asks for the node ID.
- After both keys are known, the test step sends a DP 1 command to the
  entered node ID and asks the user to confirm; this validates the
  addressing.
- No ping is sent before the keys are known.

### 13.4 Repository and HACS layout

The new integration becomes the repository’s **single HACS integration**.

- `custom_components/tuya_p2_mesh` is the HACS-installable component.
- The existing diagnostic probe `custom_components/tuya_ble_mesh` is
  preserved as reference material under `tools/diagnostics/` and is no
  longer the repository’s HACS integration.
- Existing diagnostic config entries are not loaded and are unrelated to
  the new integration; they can be removed separately.
- `hacs.json` and the README are updated accordingly.
- If the user prefers a separate repository, the component can be moved
  without changing its code.

## 14. Approval

Approval of this design authorizes the implementation plan and the work
split above. The next step after approval is the implementation plan,
followed by TDD implementation in an isolated feature branch and the
laptop test lab.
