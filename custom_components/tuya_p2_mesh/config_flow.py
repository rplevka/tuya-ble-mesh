"""Config and options flows for the Tuya P2 Mesh integration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import sequence_path
from .const import (
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
    DEFAULT_ADAPTER,
    DEFAULT_ADVERTISING_SECONDS,
    DEFAULT_SEQUENCE_SEED,
    DEFAULT_SOURCE_ID,
    DOMAIN,
)
from .controller import (
    BulbConfig,
    CaptureError,
    P2Advertisement,
    P2Controller,
    SequenceStore,
)
from .p2 import key_bytes
from .radio import AsyncP2Radio, P2Radio, RadioError, find_adapters

MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
SCAN_SECONDS = 10.0
CAPTURE_SECONDS = 30.0


def _parse_mac(value: Any) -> str:
    """Normalize and validate a Bluetooth MAC address."""
    mac = str(value).strip().upper().replace("-", ":")
    if not MAC_RE.fullmatch(mac):
        raise ValueError("invalid MAC address")
    return mac


def _parse_u16(value: Any) -> int:
    """Parse a hexadecimal 16-bit integer from a form value."""
    if isinstance(value, bool):
        raise ValueError("invalid 16-bit integer")
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip()
        if text[:2].lower() == "0x":
            text = text[2:]
        if not text:
            raise ValueError("invalid 16-bit integer")
        number = int(text, 16)
    if not 0 <= number <= 0xFFFF:
        raise ValueError("16-bit integer out of range")
    return number


def _parse_key_text(value: Any) -> str:
    """Validate a Tuya key and return it in its original form."""
    text = str(value).strip()
    key_bytes(text)
    return text


def _parse_beacon_key(value: Any) -> str:
    """Validate a Tuya key and return it as lowercase hex."""
    return key_bytes(str(value).strip()).hex()


def _bulb_from_input(
    user_input: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build a bulb config from form input, or return an error."""
    errors: dict[str, str] = {}
    try:
        mac = _parse_mac(user_input.get(CONF_MAC))
    except ValueError:
        errors["base"] = "invalid_mac"
        mac = ""
    try:
        node_id = _parse_u16(user_input.get(CONF_NODE_ID))
    except ValueError:
        errors.setdefault("base", "invalid_node_id")
        node_id = 0
    try:
        local_key = _parse_key_text(user_input.get(CONF_LOCAL_KEY))
    except ValueError:
        errors.setdefault("base", "invalid_key")
        local_key = ""
    name = str(user_input.get(CONF_NAME) or "").strip()
    if not name:
        errors.setdefault("base", "invalid_name")
    if errors:
        return {}, errors
    return (
        {
            CONF_MAC: mac,
            CONF_NODE_ID: node_id,
            CONF_LOCAL_KEY: local_key,
            CONF_NAME: name,
        },
        {},
    )


def _bulb_config(bulb: Mapping[str, Any]) -> BulbConfig:
    """Build a controller bulb config from config-entry data."""
    return BulbConfig(
        mac=bulb[CONF_MAC],
        local_key=key_bytes(bulb[CONF_LOCAL_KEY]),
        node_id=int(bulb[CONF_NODE_ID]),
        name=bulb[CONF_NAME],
    )


async def _async_check_adapter(hass: HomeAssistant, adapter: str) -> str | None:
    """Return a form error key when the adapter cannot be used."""
    adapters = await hass.async_add_executor_job(find_adapters)
    if not adapters:
        return "no_adapter"
    if adapter not in adapters:
        return "cannot_connect"
    try:
        radio = P2Radio(adapter)
    except (RadioError, ValueError):
        return "cannot_connect"
    try:
        await hass.async_add_executor_job(radio.detect_mode)
    except (RadioError, OSError, ValueError):
        return "cannot_connect"
    finally:
        await hass.async_add_executor_job(radio.close)
    return None


async def _async_controller(
    hass: HomeAssistant,
    *,
    adapter: str,
    source_id: int,
    advertising_seconds: float,
    sequence_seed: int,
    beacon_key: bytes,
) -> P2Controller:
    """Build a short-lived controller for one flow operation."""
    radio = P2Radio(adapter)
    sequence = await hass.async_add_executor_job(
        SequenceStore, sequence_path(hass), sequence_seed
    )
    return P2Controller(
        AsyncP2Radio(hass, radio),
        beacon_key,
        sequence,
        source_id=source_id,
        advertising_seconds=advertising_seconds,
    )


def _user_schema(adapters: list[str], adapter: str) -> vol.Schema:
    options = list(adapters)
    if adapter not in options:
        options.append(adapter)
    return vol.Schema(
        {
            vol.Required(CONF_ADAPTER, default=adapter): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            ),
            vol.Required(
                CONF_SOURCE_ID, default=f"{DEFAULT_SOURCE_ID:04X}"
            ): str,
            vol.Required(
                CONF_SEQUENCE_SEED, default=f"{DEFAULT_SEQUENCE_SEED:04X}"
            ): str,
            vol.Required(
                CONF_ADVERTISING_SECONDS,
                default=DEFAULT_ADVERTISING_SECONDS,
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0.5,
                    max=60.0,
                    step=0.5,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
        }
    )


def _scan_schema(
    advertisements: list[P2Advertisement],
) -> vol.Schema:
    options = []
    seen = set()
    for advertisement in sorted(
        advertisements, key=lambda item: item.rssi, reverse=True
    ):
        if advertisement.mac in seen:
            continue
        seen.add(advertisement.mac)
        options.append(
            {
                "value": advertisement.mac,
                "label": (
                    f"{advertisement.mac} "
                    f"({advertisement.rssi} dBm, "
                    f"node {advertisement.dst:04X})"
                ),
            }
        )
    device_field: Any = (
        SelectSelector(
            SelectSelectorConfig(
                options=options,
                mode=SelectSelectorMode.DROPDOWN,
                custom_value=True,
            )
        )
        if options
        else str
    )
    return vol.Schema(
        {
            vol.Optional(CONF_DEVICE): device_field,
            vol.Optional(CONF_MAC, default=""): str,
        }
    )


def _device_schema(mac: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_MAC, default=mac): str,
            vol.Required(CONF_NODE_ID, default=""): str,
            vol.Required(CONF_LOCAL_KEY, default=""): str,
            vol.Required(CONF_NAME, default=""): str,
            vol.Optional(CONF_BEACON_KEY, default=""): str,
        }
    )


def _capture_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_BEACON_KEY, default=""): str,
        }
    )


def _test_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CONFIRMED, default=True): BooleanSelector(),
        }
    )


def _bulb_selector(macs: list[str]) -> Any:
    if not macs:
        return str
    return SelectSelector(
        SelectSelectorConfig(
            options=macs, mode=SelectSelectorMode.DROPDOWN
        )
    )


class TuyaP2MeshConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure one adapter and its P2 bulbs."""

    VERSION = 1

    def _init_wizard(self) -> None:
        self._adapter = DEFAULT_ADAPTER
        self._source_id = DEFAULT_SOURCE_ID
        self._sequence_seed = DEFAULT_SEQUENCE_SEED
        self._advertising_seconds = DEFAULT_ADVERTISING_SECONDS
        self._pending_mac = ""
        self._bulb: dict[str, Any] | None = None
        self._beacon_key: str | None = None
        self._test_sent = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and validate the adapter and P2 source settings."""
        errors: dict[str, str] = {}
        adapter = DEFAULT_ADAPTER
        if user_input is not None:
            adapter = str(
                user_input.get(CONF_ADAPTER) or DEFAULT_ADAPTER
            ).strip()
            source_id = DEFAULT_SOURCE_ID
            sequence_seed = DEFAULT_SEQUENCE_SEED
            try:
                source_id = _parse_u16(user_input.get(CONF_SOURCE_ID))
            except ValueError:
                errors["base"] = "invalid_source_id"
            if not errors:
                try:
                    sequence_seed = _parse_u16(
                        user_input.get(CONF_SEQUENCE_SEED)
                    )
                except ValueError:
                    errors["base"] = "invalid_sequence_seed"
            if not errors:
                try:
                    advertising_seconds = float(
                        user_input.get(
                            CONF_ADVERTISING_SECONDS,
                            DEFAULT_ADVERTISING_SECONDS,
                        )
                    )
                except (TypeError, ValueError):
                    errors["base"] = "unknown"
                else:
                    error = await _async_check_adapter(self.hass, adapter)
                    if error == "no_adapter":
                        return self.async_abort(reason="no_adapter")
                    if error is not None:
                        errors["base"] = error
                    else:
                        await self.async_set_unique_id(adapter)
                        self._abort_if_unique_id_configured()
                        self._init_wizard()
                        self._adapter = adapter
                        self._source_id = source_id
                        self._sequence_seed = sequence_seed
                        self._advertising_seconds = advertising_seconds
                        return await self.async_step_scan()
        adapters = await self.hass.async_add_executor_job(find_adapters)
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(adapters, adapter),
            errors=errors,
        )

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover P2 advertisements, with a manual MAC fallback."""
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = str(
                user_input.get(CONF_DEVICE)
                or user_input.get(CONF_MAC)
                or ""
            ).strip()
            if not chosen:
                self._pending_mac = ""
                return await self.async_step_device()
            try:
                self._pending_mac = _parse_mac(chosen)
            except ValueError:
                errors["base"] = "invalid_mac"
            else:
                return await self.async_step_device()

        advertisements: list[P2Advertisement] = []
        controller = None
        try:
            controller = await _async_controller(
                self.hass,
                adapter=self._adapter,
                source_id=self._source_id,
                advertising_seconds=self._advertising_seconds,
                sequence_seed=self._sequence_seed,
                beacon_key=bytes(16),
            )
            advertisements = await controller.scan(SCAN_SECONDS)
        except (KeyError, RadioError, OSError, TypeError, ValueError):
            errors.setdefault("base", "cannot_connect")
        finally:
            if controller is not None:
                await controller.async_close()
        if not advertisements and "base" not in errors:
            errors["base"] = "no_devices"
        return self.async_show_form(
            step_id="scan",
            data_schema=_scan_schema(advertisements),
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect one bulb's address, keys, and name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            bulb, errors = _bulb_from_input(user_input)
            if not errors:
                beacon = str(user_input.get(CONF_BEACON_KEY) or "").strip()
                if beacon:
                    try:
                        self._beacon_key = _parse_beacon_key(beacon)
                    except ValueError:
                        errors["base"] = "invalid_key"
                if not errors:
                    self._bulb = bulb
                    if self._beacon_key is None:
                        return await self.async_step_capture()
                    return await self.async_step_test()
        return self.async_show_form(
            step_id="device",
            data_schema=_device_schema(
                getattr(self, "_pending_mac", "")
            ),
            errors=errors,
        )

    async def async_step_capture(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Capture the beacon key from app OFF/ON frames, or accept it manually."""
        errors: dict[str, str] = {}
        if user_input is not None:
            manual = str(user_input.get(CONF_BEACON_KEY) or "").strip()
            if manual:
                try:
                    self._beacon_key = _parse_beacon_key(manual)
                except ValueError:
                    errors["base"] = "invalid_key"
                else:
                    return await self.async_step_test()
            else:
                key = None
                controller = None
                try:
                    controller = await _async_controller(
                        self.hass,
                        adapter=self._adapter,
                        source_id=self._source_id,
                        advertising_seconds=self._advertising_seconds,
                        sequence_seed=self._sequence_seed,
                        beacon_key=bytes(16),
                    )
                    key = await controller.capture_beacon_key(
                        self._bulb[CONF_NODE_ID],
                        key_bytes(self._bulb[CONF_LOCAL_KEY]),
                        CAPTURE_SECONDS,
                    )
                except CaptureError:
                    errors["base"] = "capture_failed"
                except (KeyError, OSError, RadioError, TypeError, ValueError):
                    errors["base"] = "cannot_connect"
                finally:
                    if controller is not None:
                        await controller.async_close()
                if key is not None:
                    self._beacon_key = key.hex()
                    return await self.async_step_test()
        return self.async_show_form(
            step_id="capture",
            data_schema=_capture_schema(),
            errors=errors,
        )

    async def async_step_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send a DP 1 test and ask the user to confirm the bulb responded."""
        if getattr(self, "_bulb", None) is None or getattr(
            self, "_beacon_key", None
        ) is None:
            return await self.async_step_device()

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_CONFIRMED):
                if self._test_sent:
                    return self.async_create_entry(
                        title=f"Tuya P2 Mesh ({self._adapter})",
                        data=self._entry_data(),
                    )
                errors["base"] = "test_failed"
                return self.async_show_form(
                    step_id="test",
                    data_schema=_test_schema(),
                    errors=errors,
                )
            self._test_sent = False

        if not self._test_sent:
            try:
                await self._async_send_test()
            except (KeyError, OSError, RadioError, TypeError, ValueError):
                errors["base"] = "test_failed"
            else:
                self._test_sent = True
        return self.async_show_form(
            step_id="test",
            data_schema=_test_schema(),
            errors=errors,
        )

    async def _async_send_test(self) -> None:
        if self._bulb is None or self._beacon_key is None:
            raise ValueError("the bulb is not configured")
        controller = await _async_controller(
            self.hass,
            adapter=self._adapter,
            source_id=self._source_id,
            advertising_seconds=self._advertising_seconds,
            sequence_seed=self._sequence_seed,
            beacon_key=key_bytes(self._beacon_key),
        )
        try:
            await controller.test(_bulb_config(self._bulb))
        finally:
            await controller.async_close()

    def _entry_data(self) -> dict[str, Any]:
        if self._bulb is None or self._beacon_key is None:
            raise ValueError("the bulb is not configured")
        return {
            CONF_ADAPTER: self._adapter,
            CONF_SOURCE_ID: self._source_id,
            CONF_SEQUENCE_SEED: self._sequence_seed,
            CONF_ADVERTISING_SECONDS: self._advertising_seconds,
            CONF_BEACON_KEY: self._beacon_key,
            CONF_BULBS: [dict(self._bulb)],
        }

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start a reauthentication flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the adapter, beacon key, or one bulb's local key."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._async_updated_data(
                entry, user_input, allow_source=False
            )
            if not errors:
                conflict = self._adapter_conflict(entry, data)
                if conflict:
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    data=data,
                    unique_id=data[CONF_ADAPTER],
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_reauth_schema(entry),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the adapter, source ID, keys, or one bulb's local key."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._async_updated_data(
                entry, user_input, allow_source=True
            )
            if not errors:
                conflict = self._adapter_conflict(entry, data)
                if conflict:
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    data=data,
                    unique_id=data[CONF_ADAPTER],
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_reconfigure_schema(entry),
            errors=errors,
        )

    def _adapter_conflict(
        self, entry: ConfigEntry, data: Mapping[str, Any]
    ) -> bool:
        adapter = data[CONF_ADAPTER]
        if adapter == entry.data[CONF_ADAPTER]:
            return False
        existing = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, adapter
        )
        return existing is not None and existing.entry_id != entry.entry_id

    async def _async_updated_data(
        self,
        entry: ConfigEntry,
        user_input: Mapping[str, Any],
        *,
        allow_source: bool,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        data = dict(entry.data)
        errors: dict[str, str] = {}
        adapter = str(
            user_input.get(CONF_ADAPTER) or entry.data[CONF_ADAPTER]
        ).strip()
        if adapter != entry.data[CONF_ADAPTER]:
            error = await _async_check_adapter(self.hass, adapter)
            if error is not None:
                errors["base"] = "cannot_connect"
            else:
                data[CONF_ADAPTER] = adapter
        if allow_source and CONF_SOURCE_ID in user_input:
            try:
                data[CONF_SOURCE_ID] = _parse_u16(
                    user_input[CONF_SOURCE_ID]
                )
            except ValueError:
                errors["base"] = "invalid_source_id"
        beacon = str(user_input.get(CONF_BEACON_KEY) or "").strip()
        if beacon:
            try:
                data[CONF_BEACON_KEY] = _parse_beacon_key(beacon)
            except ValueError:
                errors["base"] = "invalid_key"
        local_key = str(user_input.get(CONF_LOCAL_KEY) or "").strip()
        if local_key:
            try:
                local_key = _parse_key_text(local_key)
            except ValueError:
                errors["base"] = "invalid_key"
            else:
                bulbs = [dict(bulb) for bulb in entry.data[CONF_BULBS]]
                selected = str(user_input.get(CONF_BULB) or "")
                target = next(
                    (
                        bulb
                        for bulb in bulbs
                        if bulb[CONF_MAC] == selected
                    ),
                    bulbs[0] if bulbs else None,
                )
                if target is None:
                    errors["base"] = "unknown"
                else:
                    target[CONF_LOCAL_KEY] = local_key
                    data[CONF_BULBS] = bulbs
        return data, errors

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for this entry."""
        return TuyaP2MeshOptionsFlow()


def _reauth_schema(entry: ConfigEntry) -> vol.Schema:
    macs = [bulb[CONF_MAC] for bulb in entry.data[CONF_BULBS]]
    return vol.Schema(
        {
            vol.Required(
                CONF_ADAPTER, default=entry.data[CONF_ADAPTER]
            ): str,
            vol.Optional(CONF_BEACON_KEY, default=""): str,
            vol.Required(
                CONF_BULB, default=macs[0] if macs else ""
            ): _bulb_selector(macs),
            vol.Optional(CONF_LOCAL_KEY, default=""): str,
        }
    )


def _reconfigure_schema(entry: ConfigEntry) -> vol.Schema:
    macs = [bulb[CONF_MAC] for bulb in entry.data[CONF_BULBS]]
    return vol.Schema(
        {
            vol.Required(
                CONF_ADAPTER, default=entry.data[CONF_ADAPTER]
            ): str,
            vol.Required(
                CONF_SOURCE_ID,
                default=f"{int(entry.data[CONF_SOURCE_ID]):04X}",
            ): str,
            vol.Optional(CONF_BEACON_KEY, default=""): str,
            vol.Required(
                CONF_BULB, default=macs[0] if macs else ""
            ): _bulb_selector(macs),
            vol.Optional(CONF_LOCAL_KEY, default=""): str,
        }
    )


def _options_device_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_MAC, default=""): str,
            vol.Required(CONF_NODE_ID, default=""): str,
            vol.Required(CONF_LOCAL_KEY, default=""): str,
            vol.Required(CONF_NAME, default=""): str,
        }
    )


class TuyaP2MeshOptionsFlow(OptionsFlow):
    """Add another bulb while reusing the configured adapter and keys."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        names = ", ".join(
            bulb[CONF_NAME] for bulb in self.config_entry.data[CONF_BULBS]
        )
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_device"],
            description_placeholders={"bulbs": names or "none"},
        )

    async def async_step_add_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Test and append a manually entered bulb."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            bulb, errors = _bulb_from_input(user_input)
            if not errors:
                if any(
                    existing[CONF_MAC] == bulb[CONF_MAC]
                    for existing in entry.data[CONF_BULBS]
                ):
                    errors["base"] = "duplicate_mac"
                else:
                    try:
                        await self._async_test_bulb(entry, bulb)
                    except (KeyError, OSError, RadioError, TypeError, ValueError):
                        errors["base"] = "test_failed"
                    else:
                        data = dict(entry.data)
                        data[CONF_BULBS] = [
                            *entry.data[CONF_BULBS],
                            bulb,
                        ]
                        self.hass.config_entries.async_update_entry(
                            entry, data=data
                        )
                        await self.hass.config_entries.async_reload(
                            entry.entry_id
                        )
                        return self.async_create_entry(
                            title="", data=dict(entry.options)
                        )
        return self.async_show_form(
            step_id="add_device",
            data_schema=_options_device_schema(),
            errors=errors,
        )

    async def _async_test_bulb(
        self, entry: ConfigEntry, bulb: Mapping[str, Any]
    ) -> None:
        controller = await _async_controller(
            self.hass,
            adapter=entry.data[CONF_ADAPTER],
            source_id=int(
                entry.data.get(CONF_SOURCE_ID, DEFAULT_SOURCE_ID)
            ),
            advertising_seconds=float(
                entry.data.get(
                    CONF_ADVERTISING_SECONDS,
                    DEFAULT_ADVERTISING_SECONDS,
                )
            ),
            sequence_seed=int(
                entry.data.get(CONF_SEQUENCE_SEED, DEFAULT_SEQUENCE_SEED)
            ),
            beacon_key=key_bytes(entry.data[CONF_BEACON_KEY]),
        )
        try:
            await controller.test(_bulb_config(bulb))
        finally:
            await controller.async_close()
