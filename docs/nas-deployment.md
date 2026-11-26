# NAS deployment: native Tuya P2 Mesh

Status: **prepared, not yet deployed.** The NAS currently has no Bluetooth
adapter. This checklist covers the hardware, the `hass-homeassistant-1`
container changes, verification, cutover, and rollback. The native transport
has been validated only in the laptop lab
([test-lab.md](test-lab.md)); keep the existing MQTT bridge as the production
fallback until the NAS path passes live tests.

No keys, Bluetooth addresses, node IDs, or cloud credentials belong in this
file, the compose file, shell history, or logs. Values below are placeholders
such as `hciN`, `/path/to/config`, and `0x7D00`.

## 1. Hardware prerequisites

### Bluetooth adapter

- **Linux-friendly USB adapter** supported by the in-kernel `btusb` driver.
  Check `dmesg` after plugging it in.
- **Bluetooth LE 4.0 or newer**, with both LE scanning and LE advertising
  (peripheral role). Extended advertising is preferred; the integration falls
  back to legacy advertising when the controller does not support it.
- Avoid BLE HID-only dongles, adapters that only support central/peripheral
  GATT, and adapters whose vendor driver has no mainline Linux support.
- A **USB extension cable or an adapter with an external antenna** is strongly
  recommended. The NAS enclosure, metal shelves, and USB 3.0 ports all reduce
  range. Keep the dongle away from USB 3.0 ports and from the NAS chassis.
- Use the dongle **dedicated to this integration**. Do not also configure it
  in Home Assistant's built-in Bluetooth integration, another HCI tool, or a
  second bridge. Raw HCI commands from competing users can interfere.
- Commonly available CSR8510 and RTL8761B-class dongles are examples of
  Linux-friendly BLE adapters, but verify the exact model against the NAS
  kernel and BlueZ version before buying.

### Placement and range

- The NAS must be within BLE range of every bulb. Indoors, expect roughly
  10–30 m through walls and floors, with large variation. The real range is
  unknown until the dongle is installed.
- Test from the intended location with the actual bulbs. If a bulb is at the
  edge of range, use the USB extension cable, move the dongle, or keep that
  bulb on the MQTT bridge fallback.
- Each adapter gets its own config entry. A second adapter for a distant area
  is not automatically meshed; it would need its own entry and radio, and is
  outside this checklist.

## 2. Host checks before changing compose

Run these on the NAS host, not inside the container:

```bash
lsusb
sudo dmesg | tail -n 50
bluetoothctl list
bluetoothctl show
```

Install the BlueZ tools if they are missing. Identify the adapter index
(`hci0`, `hci1`, …) and keep it powered. Host BlueZ usually powers the adapter
at boot; if it does not, bring it up explicitly:

```bash
sudo hciconfig hciN up
# or
sudo btmgmt -i N power on
```

The adapter must stay powered across reboots. If the NAS does not run BlueZ,
add a boot-time `hciconfig hciN up` unit or equivalent. The integration opens
raw HCI sockets but does not reset or power the controller itself.

Check that the adapter is not exclusively owned by another process and that
the Bluetooth kernel modules are loaded:

```bash
lsmod | grep -E 'bluetooth|btusb'
ls -l /sys/class/bluetooth
```

## 3. Compose changes for `hass-homeassistant-1`

Keep the existing service definition, image, config volume, timezone, and
restart policy. Add the Bluetooth-related settings below.

### 3.1 Validated lab recipe, translated to compose

The laptop lab used Podman with host networking, privileged mode, the USB bus,
`NET_ADMIN`/`NET_RAW`, and an unconfined seccomp profile. The equivalent
compose fragment is:

```yaml
services:
  homeassistant:
    container_name: hass-homeassistant-1
    image: homeassistant/home-assistant:stable
    restart: unless-stopped

    # Required by the validated recipe: the kernel exposed AF_BLUETOOTH only
    # in the host network namespace. Remove any `ports:` mapping when using
    # host networking; Home Assistant listens on 8123 on the host directly.
    network_mode: host

    # Validated first bring-up. This is broad: it grants all capabilities,
    # device access, and an unconfined seccomp profile. See 3.2 to reduce it.
    privileged: true
    security_opt:
      - seccomp:unconfined

    volumes:
      - /path/to/homeassistant/config:/config
      - /etc/localtime:/etc/localtime:ro
      - /dev/bus/usb:/dev/bus/usb

    environment:
      - TZ=${TZ:-UTC}
```

Notes:

- `network_mode: host` and `ports:` are incompatible. If the NAS HA instance
  currently publishes `8123:8123`, remove that mapping and update the reverse
  proxy or bookmarks to `http://<nas-address>:8123`.
- Mounting the whole `/dev/bus/usb` bus is convenient because USB bus/device
  numbers change after a reboot. It exposes all USB devices to the container;
  if that is not acceptable, pass only the dongle and add a matching device
  cgroup rule, accepting that the path may change on re-enumeration.
- If the NAS already runs Home Assistant with host networking or privileged
  mode, add only the settings it is missing.
- Do not add the docker socket or other unrelated privileges.

### 3.2 Least-privilege alternative

Once host networking is confirmed and the dongle is stable, replace
`privileged: true` with the narrower capability/device grants. This variant
has not been validated on the NAS, so test it before relying on it:

```yaml
    privileged: false
    cap_add:
      - NET_ADMIN
      - NET_RAW
    security_opt:
      - seccomp:unconfined
    device_cgroup_rules:
      - "c 189:* rmw"        # USB character devices
    volumes:
      - /path/to/homeassistant/config:/config
      - /etc/localtime:/etc/localtime:ro
      - /dev/bus/usb:/dev/bus/usb
    # Alternative to the USB volume: pass one dongle path explicitly. The
    # bus/device numbers are not stable across reboots, so update this after
    # re-enumeration.
    # devices:
    #   - /dev/bus/usb/BUS/DEVICE:/dev/bus/usb/BUS/DEVICE
```

`CAP_NET_RAW` is the hard requirement for raw HCI. `NET_ADMIN` is kept because
the validated recipe included it. The USB device and cgroup rule let the
container see the dongle; in many kernels the HCI interface itself is already
provided by the host, so the USB mount may turn out to be unnecessary once
tested. Do not remove it until raw HCI has been verified after a reboot.

Apply and validate:

```bash
docker compose config
docker compose up -d homeassistant
docker compose ps
docker inspect hass-homeassistant-1 --format '{{.HostConfig.NetworkMode}} {{.HostConfig.Privileged}}'
```

The service name is whatever your compose file uses; the example calls it
`homeassistant`. Confirm the Home Assistant UI is reachable on port 8123 after
switching to host networking.

## 4. Verify raw HCI inside the container

The whole point of the compose changes is to make this work **inside** the
Home Assistant container. Check the adapter list first:

```bash
docker exec hass-homeassistant-1 sh -c 'ls -l /sys/class/bluetooth'
```

Expected: one or more `hciN` entries. Then open a raw HCI socket:

```bash
docker exec hass-homeassistant-1 python3 -c \
  "import socket; s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI); print('HCI OK'); s.close()"
```

If this fails:

| Error | Meaning | Fix |
| --- | --- | --- |
| `Address family not supported by protocol` (`EAFNOSUPPORT`) | The container's network namespace does not expose the Bluetooth socket family | Use `network_mode: host`, as in the validated lab recipe. Privileged mode alone is not enough on such kernels. |
| `Permission denied` (`EACCES`/`EPERM`) | Missing `CAP_NET_RAW`; if a device node is opened directly, the device cgroup may also block it | Add the capability and device rule, or use the validated privileged recipe while debugging. |
| No `hciN` entries | Dongle not enumerated on the host, driver/firmware missing, or sysfs not visible in the container | Check `lsusb` and `dmesg` on the host, then `ls -l /sys/class/bluetooth` inside the container. |
| Adapter appears but commands time out | Adapter is down, held by another process, or the dongle lacks advertising support | Power it on the host, stop competing Bluetooth users, and test with a known-good dongle. |

Once the integration is deployed (section 5), repeat the check through its
radio module to confirm HCI bind and advertising setup, not just socket
creation.

## 5. Deploy the integration

The integration is installed into the Home Assistant configuration directory,
which is the host path mounted at `/config`.

### HACS

1. In Home Assistant, open HACS → **Custom repositories**.
2. Add `https://github.com/rplevka/tuya-ble-mesh` as an **Integration**.
3. Install **Tuya P2 Mesh**.
4. Restart Home Assistant.

### Manual copy

```bash
# On the NAS host, with the HA config path mounted at /path/to/config:
rsync -a --delete custom_components/tuya_p2_mesh/ \
  /path/to/homeassistant/config/custom_components/tuya_p2_mesh/
```

Then restart Home Assistant:

```bash
docker restart hass-homeassistant-1
```

Confirm the deployed `custom_components/tuya_p2_mesh` directory is present and
that the old `tuya_ble_mesh` probe is not being installed. Other unrelated
custom integrations are fine; the key point is that HACS sees only
`tuya_p2_mesh` as this repository's integration. Do not copy
`tools/diagnostics/tuya_ble_mesh/` (the old cloud probe), and do not copy any
obsolete working trees.

After restart, check **Settings → System → Logs** for a loader warning only.
There should be no `tuya_p2_mesh` setup error.

Then run the integration's direct radio check inside the container, replacing
`hciN` with the dongle's adapter name:

```bash
docker exec -w /config/custom_components/tuya_p2_mesh hass-homeassistant-1 \
  python3 -c "from radio import P2Radio; r = P2Radio('hciN'); r.detect_mode(); print('HCI bind OK, extended mode:', r.extended); r.stop_advertising(); print('advertising stop OK'); r.close()"
```

Expected output is similar to:

```text
HCI bind OK, extended mode: True
advertising stop OK
```

## 6. Add the integration and bulbs

1. **Settings → Devices & services → Add integration → Tuya P2 Mesh**.
2. Select the dongle adapter (`hciN`), source ID (default `0x7D00`), sequence
   seed, and advertising duration (default `0.5` s).
   - Choose a sequence seed **above the last value used by the MQTT bridge**.
     The counter must keep increasing. The seed is set at entry creation;
     changing it later requires recreating the entry, not editing storage
     while Home Assistant is running.
   - Keep the default source ID unless you have a documented reason to change
     it.
3. The scan waits ten seconds. P2 bulbs do not advertise while idle, so
   discovery often returns nothing; leave the selection empty and enter the
   bulb's Bluetooth address manually.
4. Enter the node ID (hex), local key, friendly name, and beacon key if you
   have it. If not, use the beacon-key capture step: toggle the bulb OFF and ON
   in the Tuya app within the 30-second window, or paste the key manually.
5. Confirm the ON/OFF test visibly changed the bulb. Only then finish the
   flow.
6. Add further bulbs with the integration's **Configure** options flow. The
   adapter and beacon key are reused; each bulb needs its own address, node ID,
   and local key.
7. For grouping, create a Home Assistant light group helper over the new light
   entities (Settings → Devices & services → Helpers → Group → Light). Native
   P2 group addresses are not implemented; the helper sends one advertisement
   per member.

Keys are stored in the Home Assistant config entry. Do not put them in YAML,
compose environment variables, or this document. The old bridge configuration
and the Tuya app/cloud are the one-time source of those values.

## 7. Acceptance test before cutover

Run these from Home Assistant, not from a shell, and have the user visually
confirm each bulb:

- [ ] Office bulb on, off, 30/50/100% brightness.
- [ ] Red and blue colour at 50% brightness.
- [ ] Warm 2000 K and cool 6500 K at 50% brightness.
- [ ] Kids room bulb on and off.
- [ ] Light-group on at 50% and off.
- [ ] Restart Home Assistant; the persisted sequence counter is not reset and
      commands still work.
- [ ] No `tuya_p2_mesh` errors in the log.
- [ ] A command still works after a NAS reboot and a dongle re-enumeration.

The laptop lab record has the validated commands, observed timings, and the
sequence-persistence check: [test-lab.md](test-lab.md). Do not declare the NAS
deployment successful from API responses alone; P2 transmission is
fire-and-forget and the visible bulb response is the acceptance test.

## 8. Cutover and fallback to the MQTT bridge

The existing MQTT bridge (`tuya-p2-bridge.service` on its own host) remains
the fallback throughout.

1. Before cutover, back up the compose file and Home Assistant configuration
   (including `.storage/`), and record the bridge's last sequence value.
2. Keep the bridge installed and its config intact. Do not uninstall the
   bridge service during the trial.
3. After the native integration passes the acceptance test, disable the
   bridge-backed MQTT entities or stop the bridge service so two controllers
   are not sending commands to the same bulbs. If both must run temporarily,
   expect duplicate or racing commands and do not rely on state.
4. Keep the native integration and the bridge available in parallel for a
   rollback window. Remove the bridge only after the user is satisfied.

Rollback:

```bash
# On the bridge host:
sudo systemctl start tuya-p2-bridge.service
systemctl is-active tuya-p2-bridge.service
```

Then disable the Tuya P2 Mesh config entry in Home Assistant (or remove the
integration) and re-enable the MQTT entities. Do not delete the bridge config,
the integration's `.storage/tuya_p2_mesh_sequence.json`, or the Home Assistant
backup until the rollback window closes.

## 9. Blocking the Tuya cloud

The native integration does not contact the Tuya cloud at runtime. Once the
bulbs are commissioned, you can block Tuya cloud endpoints at the gateway or
router if you want to enforce that boundary.

- Runtime control, discovery, and key use are entirely local. Blocking the
  cloud does not break existing bulbs.
- **New bulb pairing needs the app/cloud once** (pairing into the mesh,
  obtaining the local key, and, on first setup, the home beacon key).
  Temporarily allow the cloud, or pair from a phone on mobile data, then
  restore the block.
- The Tuya app's cloud control of these bulbs will stop working while the
  cloud is blocked; local Home Assistant control continues to work.
- If Home Assistant also runs the built-in Tuya cloud integration, blocking
  the cloud will break that integration. Remove or disable it first if it is
  no longer used.
- Do not block GitHub/HACS, your MQTT broker, or Home Assistant's own update
  and remote-access traffic while blocking Tuya.
- Blocking is a network policy, not an integration feature. Record the exact
  domains/rules in your router configuration, not in this repository.

## 10. Security notes

- The validated recipe runs Home Assistant with broad container privileges.
  Prefer the least-privilege variant from section 3.2 once it is tested.
- Mounting `/dev/bus/usb` exposes every USB device on the NAS to the
  container. Use a dedicated host or narrow the mount if that matters.
- Install the integration from the trusted repository only, and review the
  code before granting `CAP_NET_RAW`.
- Integration diagnostics redact all keys, but still include the adapter name,
  bulb addresses, and node IDs. Review before attaching to an issue.
- The old diagnostic probe under `tools/diagnostics/tuya_ble_mesh/` is not
  installed by HACS and is not part of this deployment. Keep it admin-only if
  it is ever used; its API response can expose secret keys.

## 11. Rollback of the compose changes

If the native deployment is abandoned, restore the previous compose file and
recreate the container:

```bash
docker compose up -d homeassistant
```

Remove host networking, privileged/capability grants, the USB mount, and the
seccomp override. Restore the previous `ports:` mapping and reverse-proxy
configuration. Then restart the MQTT bridge as described in section 8.
