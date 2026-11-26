# Laptop Home Assistant container lab

Status: complete. The container/HCI setup, config-flow onboarding, live bulb
commands, latency re-check, and production restore were all validated on
2026-11-26. The user confirmed the visible bulb tests and the post-`c3e8aaf`
latency. The test config directory is kept for inspection.

## Purpose

Run the native `tuya_p2_mesh` component against the real office and kids room
bulbs in a disposable Home Assistant container on the laptop. The container owns
the laptop's Intel AX201 Bluetooth adapter (`hci0`); the production
`tuya-p2-bridge.service` must be stopped for the whole lab.

No Tuya cloud API is used. Bulb keys are read at runtime from
`/home/roman/.config/tuya-p2-bridge/config.json` (mode 0600) and are never
printed, logged, or committed. This document intentionally contains no key,
MAC, or device-ID values.

## Host

- Podman 5.8.4 (rootful, `sudo podman`)
- Home Assistant container image `ghcr.io/home-assistant/home-assistant:stable`
  (HA Core 2026.11.3 at the time of this test)
- Intel AX201 Bluetooth, exposed as `hci0`
- Production bridge `tuya-p2-bridge.service`

## 1. Stop the production bridge

```bash
sudo systemctl stop tuya-p2-bridge.service
systemctl is-active tuya-p2-bridge.service   # must print: inactive
```

The production NAS light entities are unavailable while this lab runs.

## 2. Prepare the config directory

```bash
mkdir -p /home/roman/ha-p2-test/config
```

`/home/roman/ha-p2-test/config/configuration.yaml`:

```yaml
# frontend pulls in http, auth, api, onboarding, and the config-entry API.
# default_config is deliberately not used: it loads the built-in Bluetooth
# integration, which is useless without a system D-Bus in the container and
# only logs errors about hci0.
frontend:

http:
  server_port: 8124
```

## 3. Working container recipe: host networking is required

The recipe in the task brief (podman bridge network, `-p 8124:8123`,
`--privileged`, `/dev/bus/usb`, `NET_ADMIN`/`NET_RAW`, seccomp unconfined) was
tried. `/sys/class/bluetooth` was visible inside the container, but creating a
raw HCI socket failed:

```text
OSError: [Errno 97] Address family not supported by protocol
```

That is `EAFNOSUPPORT` from `socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI)`.
The same socket works on the host, and works in the container as soon as
`--net=host` is used. This kernel only exposes the Bluetooth socket family in
the host network namespace; `--privileged` and USB access alone are not enough.
This is an environment restriction, not a bug in the integration.

Working command (run from the repository root):

```bash
sudo podman run -d --name ha-p2-test \
  --net=host \
  -v /home/roman/ha-p2-test/config:/config \
  -v "$PWD/custom_components/tuya_p2_mesh:/config/custom_components/tuya_p2_mesh:rw" \
  --privileged \
  -v /dev/bus/usb:/dev/bus/usb \
  --cap-add=NET_ADMIN --cap-add=NET_RAW \
  --security-opt seccomp=unconfined \
  ghcr.io/home-assistant/home-assistant:stable
```

Because `--net=host` ignores `-p`, Home Assistant is configured to listen on
port 8124 directly, so the lab URL is still `http://localhost:8124`.

### HA 2026.11 HTTP port trial

HA 2026.11 stages a YAML `http:` change as an unconfirmed "pending" config and
automatically reverts it after five minutes unless the user promotes it. On a
fresh config directory the first start therefore binds 8124 as pending, then
reverts to 8123 if it is not confirmed.

For an unattended lab, stop the container after the first start, promote the
port in the HTTP store, and start it again:

```bash
sudo podman stop ha-p2-test
sudo python3 - <<'PY'
import json
import os

path = "/home/roman/ha-p2-test/config/.storage/http"
with open(path) as handle:
    data = json.load(handle)
data["data"]["stable"]["server_port"] = 8124
data["data"]["pending"] = None
data["data"]["yaml_migration_done"] = True
tmp = path + ".tmp"
with open(tmp, "w") as handle:
    json.dump(data, handle)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(tmp, path)
os.chmod(path, 0o600)
PY
sudo podman start ha-p2-test
```

Alternatively, open the UI on port 8124 during the five-minute trial and use
the pending-HTTP-config confirmation shown there (the WebSocket command is
`http/config/promote`).

## 4. Verify raw HCI inside the container

```bash
sudo podman exec ha-p2-test sh -c 'ls -l /sys/class/bluetooth'
sudo podman exec ha-p2-test python3 -c \
  "import socket; s=socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI); print('HCI OK'); s.close()"
```

Observed:

```text
hci0 -> ../../devices/.../bluetooth/hci0
HCI OK
```

Integration-level radio check (this actually binds `hci0` and sends HCI
commands):

```bash
sudo podman run --rm --privileged --net=host \
  -v "$PWD/custom_components/tuya_p2_mesh:/c:ro" \
  ghcr.io/home-assistant/home-assistant:stable \
  sh -c 'cd /c && python3 -c "from radio import P2Radio; r=P2Radio(\"hci0\"); r.detect_mode(); print(\"HCI bind OK, extended mode:\", r.extended); r.stop_advertising(); print(\"advertising stop OK\"); r.close()"'
```

Observed:

```text
HCI bind OK, extended mode: True
advertising stop OK
```

## 5. Onboarding and adding the integration

Onboarding was completed through the unauthenticated API, then an access token
was saved outside the repository at `/tmp/opencode/ha-p2-test-token` (mode
0600). The test login is a disposable user; credentials are kept in the task
report, not here.

API sequence used:

1. `POST /api/onboarding/users` with `client_id`,
   `name`, `username`, `password`, and `language`; the returned `auth_code` is
   exchanged at `POST /auth/token` (form-encoded authorization-code grant).
2. `POST /api/onboarding/core_config` with `{}`.
3. `POST /api/onboarding/analytics` with `{"analytics": false}`.
4. `POST /api/onboarding/integration` with the same `client_id` and
   `redirect_uri` (`http://localhost:8124/`).

The integration was added through the config-entry API with the manual
key/beacon path, because P2 bulbs do not emit P2 advertisements while idle and
the discovery scan correctly reported `no_devices`:

- `POST /api/config/config_entries/flow` with `{"handler": "tuya_p2_mesh"}`
- user step: adapter `hci0`, source ID `7D00`, sequence seed `4000`,
  advertising duration `3.0` seconds (later migrated to `0.5` for the
  `c3e8aaf` latency fix; see section 9)
- scan step: enter the bulb MAC manually
- device step: MAC, node ID (hex), local key, name, beacon key
- test step: confirm the DP 1 ON/OFF test send

The office bulb created the config entry. The kids room bulb was appended with
the options/add-device flow:

- `POST /api/config/config_entries/options/flow` with
  `{"handler": "<config-entry-id>"}` (the options API uses the entry ID in the
  `handler` field)
- `{"next_step_id": "add_device"}`
- manual MAC, then bulb details; the flow sends its own DP 1 ON/OFF test.

The entry uses one beacon key and both bulbs' local keys, all read from the
production bridge config at runtime. No key value was copied into this file or
into shell history.

## 6. Entities

| Entity | Name |
| --- | --- |
| `light.office_bulb` | Office bulb |
| `light.kids_room_bulb` | Kids room bulb |
| `light.p2_lab_lights` | P2 Lab Lights (native HA light group helper) |

## 7. Live validation (user-confirmed)

These service calls were issued through the test HA API. All returned HTTP 200
and the optimistic entity state updated as expected. The radio commands are
fire-and-forget advertisements, so the API result proves transmission; the
user separately confirmed the visible changes.

| # | Call | Data |
| --- | --- | --- |
| 1 | `light.turn_off` office | - |
| 2 | `light.turn_on` office | - |
| 3 | `light.turn_off` office | - |
| 4 | `light.turn_on` office | `brightness_pct: 30` |
| 5 | `light.turn_on` office | `brightness_pct: 50` |
| 6 | `light.turn_on` office | `brightness_pct: 100` |
| 7 | `light.turn_on` office | `hs_color: [0, 100]`, `brightness_pct: 50` (red) |
| 8 | `light.turn_on` office | `hs_color: [240, 100]`, `brightness_pct: 50` (blue) |
| 9 | `light.turn_on` office | `color_temp_kelvin: 2000`, `brightness_pct: 50` |
| 10 | `light.turn_on` office | `color_temp_kelvin: 6500`, `brightness_pct: 50` |
| 11 | `light.turn_on` kids room | - |
| 12 | `light.turn_off` kids room | - |
| 13 | `light.turn_on` group | `brightness_pct: 50` |
| 14 | `light.turn_off` group | - |
| 15 | `light.turn_on` office | `color_temp_kelvin: 4000`, `brightness_pct: 50` (final state) |

Observed timings before the latency fix, with `advertising_seconds: 3.0`: a
one-frame command took about 3.3 s, brightness (two frames) about 6.6 s, an HS
colour (three frames) about 9.8 s, and colour temperature plus brightness
(four frames) about 13 s. This was the configured advertising duration
multiplied by the number of DP groups, not an error. See section 9 for the
post-`c3e8aaf` re-check.

The lab was left at: office bulb on, white 4000 K, 50% brightness; kids room
bulb off. The group entity reports on because one member is on.

### User confirmation

The user confirmed the visible behaviour of every item above: office on/off,
30/50/100% brightness, red and blue, warm 2000 K and cool 6500 K, kids room
on/off, the native light group, and the final office white 4000 K at 50% with
the kids room off. The user also confirmed the improved latency of the
`c3e8aaf` build (HS colour ~0.65 s, white temperature plus brightness ~1.12 s).

### Sequence persistence across a Home Assistant restart

- Before the HA restart, the persisted counter was `0x4023`.
- The container was restarted (`sudo podman restart ha-p2-test`) and the
  counter was still `0x4023` afterwards, i.e. it was not reset to the seed
  `0x4000`.
- Subsequent group and final-state commands advanced it to `0x402F`.

No integration errors appeared in `home-assistant.log`; the only warning is the
standard "custom integration has not been tested" loader warning.

## 8. Known environment notes

- Raw HCI inside a Podman bridge network fails with `EAFNOSUPPORT`; use
  `--net=host`.
- HA 2026.11 stages YAML `http:` port changes as pending and reverts after five
  minutes unless promoted.
- The options-flow REST endpoint takes the config-entry ID in the `handler`
  field, not the integration domain.
- `default_config:` is not used in this lab, so the built-in Bluetooth
  integration does not log D-Bus/hci0 errors. The raw-HCI component does not
  need it.

## 9. Latency fix re-check (`c3e8aaf`)

The approved latency fix packs compatible DPs into the same P2 frame and
changes the default advertising duration to 0.5 s. Existing config entries keep
their stored value, so the lab entry was migrated from 3.0 s to 0.5 s by
stopping the container and changing only
`data["entries"][...]["data"]["advertising_seconds"]` in
`.storage/core.config_entries`. Both bulbs and all keys were preserved, and the
sequence counter was not touched.

Re-measured through the test HA API after restart:

| Command | Frames | Wall clock |
| --- | --- | --- |
| Office HS red, 50% brightness | 1 | 0.65 s |
| Office white 4000 K, 50% brightness | 2 | 1.12 s |
| Kids room on / off | 1 each | 0.68 s / 0.55 s |

The sequence counter persisted across the restart (16492 -> 16495) and
advanced to 16500 after these calls; it was not reset to the seed.

## 10. Teardown and production restore

Executed after the user confirmed the live tests:

- `ha-p2-test` was stopped; `sudo podman ps` no longer lists it. The config
  directory `/home/roman/ha-p2-test/config` was kept for inspection.
- `tuya-p2-bridge.service` was started and `systemctl is-active` reports
  `active`.

The commands used:

```bash
sudo podman stop ha-p2-test
sudo systemctl start tuya-p2-bridge.service
systemctl is-active tuya-p2-bridge.service   # prints: active
```

Optional cleanup after the results are recorded:

```bash
sudo podman rm ha-p2-test
# The config directory can be kept for inspection or removed:
# sudo rm -rf /home/roman/ha-p2-test
```
