"""Light platform for the Tuya P2 Mesh integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import dp, p2
from .const import (
    CONF_BULBS,
    CONF_LOCAL_KEY,
    CONF_MAC,
    CONF_NAME,
    CONF_NODE_ID,
    DOMAIN,
    KELVIN_MAX,
    KELVIN_MIN,
)
from .controller import BulbConfig, P2Controller

_MIRED_PER_KELVIN = 1_000_000


def _clamp_kelvin(value: float) -> int:
    return max(KELVIN_MIN, min(KELVIN_MAX, int(value)))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one light entity per configured bulb."""
    controller: P2Controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        TuyaP2Light(controller, _bulb_config(data))
        for data in entry.data[CONF_BULBS]
    )


def _bulb_config(data: dict[str, Any]) -> BulbConfig:
    """Build a controller bulb config from config-entry data."""
    return BulbConfig(
        mac=data[CONF_MAC],
        local_key=p2.key_bytes(data[CONF_LOCAL_KEY]),
        node_id=int(data[CONF_NODE_ID]),
        name=data[CONF_NAME],
    )


class TuyaP2Light(LightEntity):
    """A Tuya P2 mesh bulb controlled through the local radio."""

    _attr_assumed_state = True
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = KELVIN_MIN
    _attr_max_color_temp_kelvin = KELVIN_MAX

    def __init__(self, controller: P2Controller, bulb: BulbConfig) -> None:
        self._controller = controller
        self._bulb = bulb
        self._attr_name = bulb.name
        self._attr_unique_id = bulb.mac
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, bulb.mac)},
            name=bulb.name,
            manufacturer="Tuya",
            model="P2 Mesh Bulb",
        )
        self._attr_is_on = None
        self._attr_brightness = None
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_hs_color = None
        self._attr_color_temp_kelvin = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Write optimistic state, then send the requested light state."""
        cmd: dict[str, Any] = {"state": "ON"}
        hs_color: tuple[float, float] | None = None
        color_temp_kelvin: int | None = None
        if ATTR_BRIGHTNESS in kwargs:
            cmd["brightness"] = kwargs[ATTR_BRIGHTNESS]
        if ATTR_HS_COLOR in kwargs:
            hs_color = kwargs[ATTR_HS_COLOR]
            cmd["color"] = {"h": hs_color[0], "s": hs_color[1]}
            if "brightness" not in cmd and self._attr_brightness is not None:
                cmd["brightness"] = self._attr_brightness
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            color_temp_kelvin = _clamp_kelvin(kwargs[ATTR_COLOR_TEMP_KELVIN])
            cmd["color_temp"] = round(_MIRED_PER_KELVIN / color_temp_kelvin)
        groups = dp.command_groups(cmd)
        self._attr_is_on = True
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        if hs_color is not None:
            self._attr_hs_color = hs_color
            self._attr_color_mode = ColorMode.HS
        elif color_temp_kelvin is not None:
            self._attr_color_temp_kelvin = color_temp_kelvin
            self._attr_color_mode = ColorMode.COLOR_TEMP
        self.async_write_ha_state()
        await self._send(groups)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Write optimistic off state, then send DP 1 false."""
        groups = dp.command_groups({"state": "OFF"})
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._send(groups)

    async def _send(
        self, groups: list[tuple[int, list[tuple[int, str, object]]]]
    ) -> None:
        for _group_id, dps in groups:
            await self._controller.send(self._bulb, dps)
