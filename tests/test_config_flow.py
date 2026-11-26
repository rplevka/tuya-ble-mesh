"""Tests for the Tuya P2 Mesh config, options, and reauth flows."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol

import custom_components.tuya_p2_mesh as component
from custom_components.tuya_p2_mesh import config_flow
from custom_components.tuya_p2_mesh.const import (
    CONF_ADAPTER,
    CONF_ADVERTISING_SECONDS,
    CONF_BEACON_KEY,
    CONF_BULB,
    CONF_BULBS,
    CONF_CONFIRMED,
    CONF_DEVICE,
    CONF_LOCAL_KEY,
    CONF_MAC,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_SEQUENCE_SEED,
    CONF_SOURCE_ID,
    DOMAIN,
)
from custom_components.tuya_p2_mesh.controller import (
    CaptureError,
    P2Advertisement,
    P2Controller,
)
from custom_components.tuya_p2_mesh.radio import RadioError
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

ADAPTER = "hci0"
MAC = "AA:BB:CC:DD:EE:FF"
MAC2 = "11:22:33:44:55:66"
NODE_ID = 0x0010
LOCAL_KEY = "0123456789ABCDEF"
LOCAL_KEY2 = "FEDCBA9876543210"
BEACON_KEY = "00112233445566778899AABBCCDDEEFF"
NEW_BEACON_KEY = "FFEEDDCCBBAA99887766554433221100"
SEQUENCE_SEED = 0x4000

USER_INPUT = {
    CONF_ADAPTER: ADAPTER,
    CONF_SOURCE_ID: "7D00",
    CONF_SEQUENCE_SEED: "4000",
    CONF_ADVERTISING_SECONDS: 1.0,
}


class FakeRadio:
    """In-process stand-in for the raw HCI radio."""

    instances: list[FakeRadio] = []
    fail = False

    def __init__(self, adapter: str = ADAPTER) -> None:
        self.adapter = adapter
        self.validated = False
        self.closed = False
        self.frames: list[tuple[bytes, float]] = []
        FakeRadio.instances.append(self)

    def detect_mode(self) -> None:
        if FakeRadio.fail:
            raise RadioError("adapter unavailable")
        self.validated = True

    def advertise(self, frame: bytes, seconds: float) -> None:
        if FakeRadio.fail:
            raise RadioError("adapter unavailable")
        self.frames.append((frame, seconds))

    def scan(self, seconds: float) -> list[Any]:
        if FakeRadio.fail:
            raise RadioError("adapter unavailable")
        return []

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_hardware(monkeypatch: pytest.MonkeyPatch) -> type[FakeRadio]:
    """Replace the HCI radio with an in-process fake in every flow test."""
    FakeRadio.instances = []
    FakeRadio.fail = False
    monkeypatch.setattr(config_flow, "P2Radio", FakeRadio)
    monkeypatch.setattr(config_flow, "find_adapters", lambda: [ADAPTER, "hci1"])
    monkeypatch.setattr(component, "P2Radio", FakeRadio)
    return FakeRadio


def _bulb(
    mac: str = MAC,
    node_id: int = NODE_ID,
    local_key: str = LOCAL_KEY,
    name: str = "Office",
) -> dict[str, Any]:
    """Build one synthetic bulb config entry."""
    return {
        CONF_MAC: mac,
        CONF_NODE_ID: node_id,
        CONF_LOCAL_KEY: local_key,
        CONF_NAME: name,
    }


def _entry_data() -> dict[str, Any]:
    return {
        CONF_ADAPTER: ADAPTER,
        CONF_SOURCE_ID: 0x7D00,
        CONF_SEQUENCE_SEED: SEQUENCE_SEED,
        CONF_ADVERTISING_SECONDS: 1.0,
        CONF_BEACON_KEY: BEACON_KEY.lower(),
        CONF_BULBS: [_bulb()],
    }


def _make_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_entry_data(),
        title=f"Tuya P2 Mesh ({ADAPTER})",
        unique_id=ADAPTER,
    )
    entry.add_to_hass(hass)
    return entry


def _field(schema: vol.Schema, name: str) -> Any:
    """Return the validator for a field in a voluptuous schema."""
    for marker, value in schema.schema.items():
        if getattr(marker, "schema", marker) == name:
            return value
    raise AssertionError(f"field {name!r} not found in schema")


def _device_input(**overrides: Any) -> dict[str, Any]:
    data = {
        CONF_MAC: MAC,
        CONF_NODE_ID: "0010",
        CONF_LOCAL_KEY: LOCAL_KEY,
        CONF_NAME: "Office",
        CONF_BEACON_KEY: "",
    }
    data.update(overrides)
    return data


async def _start_user_flow(hass) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def _advance_to_scan(hass) -> dict[str, Any]:
    result = await _start_user_flow(hass)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(USER_INPUT)
    )


async def _advance_to_device(hass) -> dict[str, Any]:
    result = await _advance_to_scan(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "scan"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MAC: MAC}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "device"
    return result


async def test_user_step_shows_adapter_and_source_defaults(hass) -> None:
    result = await _start_user_flow(hass)
    defaults = result["data_schema"]({})

    assert defaults[CONF_ADAPTER] == ADAPTER
    assert defaults[CONF_SOURCE_ID] == "7D00"
    assert defaults[CONF_SEQUENCE_SEED] == "4000"
    assert defaults[CONF_ADVERTISING_SECONDS] == 0.5


async def test_manual_device_entry_creates_entry(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(P2Controller, "test", AsyncMock())

    result = await _advance_to_device(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _device_input(beacon_key=BEACON_KEY)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONFIRMED: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Tuya P2 Mesh ({ADAPTER})"
    data = result["data"]
    assert data[CONF_ADAPTER] == ADAPTER
    assert data[CONF_SOURCE_ID] == 0x7D00
    assert data[CONF_SEQUENCE_SEED] == SEQUENCE_SEED
    assert data[CONF_ADVERTISING_SECONDS] == 1.0
    assert data[CONF_BEACON_KEY] == BEACON_KEY.lower()
    assert data[CONF_BULBS] == [_bulb()]

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].data == data


async def test_test_step_retries_failed_send(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    send = AsyncMock(side_effect=[RadioError("adapter busy"), None])
    monkeypatch.setattr(
        config_flow.TuyaP2MeshConfigFlow, "_async_send_test", send
    )

    result = await _advance_to_device(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _device_input(beacon_key=BEACON_KEY)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test"
    assert result["errors"] == {"base": "test_failed"}
    assert result["data_schema"]({})[CONF_CONFIRMED] is False
    assert send.await_count == 1

    # The obvious normal submission retries the failed send and re-renders.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONFIRMED: True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test"
    assert result["errors"] == {}
    assert result["data_schema"]({})[CONF_CONFIRMED] is True
    assert send.await_count == 2

    # Confirming the successful retry creates the entry.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONFIRMED: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BULBS] == [_bulb()]


async def test_scan_step_lists_discovered_devices(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertisement = P2Advertisement(
        mac=MAC,
        rssi=-42,
        frame=b"\0" * 26,
        src=0x8000,
        dst=NODE_ID,
        seq=7,
        subcmd=5,
    )
    monkeypatch.setattr(
        P2Controller, "scan", AsyncMock(return_value=[advertisement])
    )

    result = await _advance_to_scan(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "scan"
    selector = _field(result["data_schema"], CONF_DEVICE)
    options = [
        option["value"] if isinstance(option, dict) else option
        for option in selector.config["options"]
    ]
    assert MAC in options

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: MAC}
    )
    assert result["step_id"] == "device"
    assert result["data_schema"]({})[CONF_MAC] == MAC


async def test_scan_manual_entry_works_with_discovered_devices(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertisement = P2Advertisement(
        mac=MAC,
        rssi=-42,
        frame=b"\0" * 26,
        src=0x8000,
        dst=NODE_ID,
        seq=7,
        subcmd=5,
    )
    monkeypatch.setattr(
        P2Controller, "scan", AsyncMock(return_value=[advertisement])
    )

    result = await _advance_to_scan(hass)
    assert result["step_id"] == "scan"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MAC: MAC2}
    )

    assert result["step_id"] == "device"
    assert result["data_schema"]({})[CONF_MAC] == MAC2


async def test_capture_step_derives_beacon_key(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = AsyncMock(return_value=bytes.fromhex(BEACON_KEY))
    monkeypatch.setattr(P2Controller, "capture_beacon_key", capture)
    monkeypatch.setattr(P2Controller, "test", AsyncMock())

    result = await _advance_to_device(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _device_input(beacon_key="")
    )
    assert result["step_id"] == "capture"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "test"
    capture.assert_awaited_once()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONFIRMED: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BEACON_KEY] == BEACON_KEY.lower()


async def test_capture_mismatch_shows_error(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        P2Controller,
        "capture_beacon_key",
        AsyncMock(side_effect=CaptureError("no matching pair")),
    )
    monkeypatch.setattr(P2Controller, "test", AsyncMock())

    result = await _advance_to_device(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _device_input(beacon_key="")
    )
    assert result["step_id"] == "capture"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "capture"
    assert result["errors"] == {"base": "capture_failed"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BEACON_KEY: BEACON_KEY}
    )
    assert result["step_id"] == "test"


async def test_options_add_device(hass, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P2Controller, "test", AsyncMock())
    advertisement = P2Advertisement(
        mac=MAC2,
        rssi=-50,
        frame=b"\0" * 26,
        src=0x8000,
        dst=0x11,
        seq=8,
        subcmd=5,
    )
    monkeypatch.setattr(
        P2Controller, "scan", AsyncMock(return_value=[advertisement])
    )
    entry = _make_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["add_device"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_device"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_device"
    selector = _field(result["data_schema"], CONF_DEVICE)
    options = [
        option["value"] if isinstance(option, dict) else option
        for option in selector.config["options"]
    ]
    assert MAC2 in options

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEVICE: MAC2}
    )
    assert result["step_id"] == "add_device_details"
    assert result["data_schema"]({})[CONF_MAC] == MAC2

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MAC: MAC2,
            CONF_NODE_ID: "0011",
            CONF_LOCAL_KEY: LOCAL_KEY2,
            CONF_NAME: "Kitchen",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_BULBS] == [
        _bulb(),
        _bulb(mac=MAC2, node_id=0x11, local_key=LOCAL_KEY2, name="Kitchen"),
    ]
    assert entry.data[CONF_ADAPTER] == ADAPTER
    assert entry.data[CONF_BEACON_KEY] == BEACON_KEY.lower()


async def test_options_add_device_manual_entry(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(P2Controller, "test", AsyncMock())
    entry = _make_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_device"}
    )
    assert result["step_id"] == "add_device"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["step_id"] == "add_device_details"
    assert result["data_schema"]({})[CONF_MAC] == ""

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MAC: MAC2,
            CONF_NODE_ID: "0011",
            CONF_LOCAL_KEY: LOCAL_KEY2,
            CONF_NAME: "Kitchen",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_BULBS] == [
        _bulb(),
        _bulb(mac=MAC2, node_id=0x11, local_key=LOCAL_KEY2, name="Kitchen"),
    ]
    assert entry.data[CONF_BEACON_KEY] == BEACON_KEY.lower()


async def test_options_add_device_rejects_duplicate_mac(hass) -> None:
    entry = _make_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAC: MAC}
    )
    assert result["step_id"] == "add_device_details"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MAC: MAC,
            CONF_NODE_ID: "0010",
            CONF_LOCAL_KEY: LOCAL_KEY,
            CONF_NAME: "Office",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_device_details"
    assert result["errors"] == {"base": "duplicate_mac"}
    assert entry.data[CONF_BULBS] == [_bulb()]


async def test_reauth_updates_keys(hass) -> None:
    entry = _make_entry(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BEACON_KEY: NEW_BEACON_KEY,
            CONF_BULB: MAC,
            CONF_LOCAL_KEY: LOCAL_KEY2,
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_BEACON_KEY] == NEW_BEACON_KEY.lower()
    assert entry.data[CONF_BULBS][0][CONF_LOCAL_KEY] == LOCAL_KEY2
    await hass.async_block_till_done()


async def test_reconfigure_updates_source_and_beacon(hass) -> None:
    entry = _make_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ADAPTER: ADAPTER,
            CONF_SOURCE_ID: "7D02",
            CONF_BEACON_KEY: NEW_BEACON_KEY,
            CONF_BULB: MAC,
            CONF_LOCAL_KEY: "",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SOURCE_ID] == 0x7D02
    assert entry.data[CONF_BEACON_KEY] == NEW_BEACON_KEY.lower()
    await hass.async_block_till_done()


async def test_user_step_rejects_adapter_when_hci_fails(
    hass, fake_hardware: type[FakeRadio]
) -> None:
    fake_hardware.fail = True
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(USER_INPUT)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_setup_entry_creates_controller_and_forwards_light(
    hass, fake_hardware: type[FakeRadio]
) -> None:
    entry = _make_entry(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = hass.data[DOMAIN][entry.entry_id]
    assert isinstance(controller, P2Controller)
    assert fake_hardware.instances[-1].validated
    assert hass.states.get("light.office") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert fake_hardware.instances[-1].closed


async def test_setup_entry_not_ready_when_adapter_fails(
    hass, fake_hardware: type[FakeRadio]
) -> None:
    entry = _make_entry(hass)
    fake_hardware.fail = True

    with pytest.raises(ConfigEntryNotReady):
        await component.async_setup_entry(hass, entry)

    assert fake_hardware.instances[-1].closed
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_test_step_persists_shared_sequence(hass) -> None:
    result = await _advance_to_device(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _device_input(beacon_key=BEACON_KEY)
    )
    assert result["step_id"] == "test"

    sequence_path = Path(
        hass.config.path(".storage/tuya_p2_mesh_sequence.json")
    )
    assert json.loads(sequence_path.read_text(encoding="utf-8"))["seq"] == (
        SEQUENCE_SEED + 2
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONFIRMED: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data[CONF_SEQUENCE_SEED] == SEQUENCE_SEED

    await hass.async_block_till_done()
    controller = hass.data[DOMAIN][entry.entry_id]
    assert controller.sequence.value == SEQUENCE_SEED + 2


async def test_no_keys_in_logs(hass, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    result = await _advance_to_device(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _device_input(beacon_key=BEACON_KEY)
    )
    assert result["step_id"] == "test"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONFIRMED: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    assert LOCAL_KEY not in caplog.text
    assert BEACON_KEY.lower() not in caplog.text
    assert BEACON_KEY not in caplog.text
    assert MAC not in caplog.text
