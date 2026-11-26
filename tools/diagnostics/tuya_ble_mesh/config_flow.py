"""Config flow for the Tuya BLE Mesh integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import (
    CONF_SCAN_SECONDS,
    CONF_TARGET_MAC,
    DEFAULT_SCAN_SECONDS,
    DOMAIN,
    NAME,
)


class TuyaBLEMeshConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya BLE Mesh.

    The first release of this integration is a diagnostic build.  It creates
    one entry that scans the Home Assistant Bluetooth fabric, reads the
    authenticated Tuya mobile session from the built-in Tuya integration, and
    exposes the result at ``/api/tuya_ble_mesh/probe``.  The light platform is
    added on top of the same entry once the device family and keys are known.
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the diagnostic entry."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            return self.async_create_entry(
                title=NAME,
                data={
                    CONF_SCAN_SECONDS: int(
                        user_input.get(CONF_SCAN_SECONDS, DEFAULT_SCAN_SECONDS)
                    ),
                    CONF_TARGET_MAC: (
                        user_input.get(CONF_TARGET_MAC) or ""
                    ).strip().upper(),
                    CONF_ADDRESS: (user_input.get(CONF_TARGET_MAC) or "")
                    .strip()
                    .upper(),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TARGET_MAC, default=""): str,
                    vol.Optional(
                        CONF_SCAN_SECONDS, default=DEFAULT_SCAN_SECONDS
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
                }
            ),
        )
