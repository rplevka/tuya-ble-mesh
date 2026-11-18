"""HTTP view that returns the latest probe result.

The payload can contain Tuya local keys and mesh keys.  It is therefore
admin-only, is never cached, and should be disabled or removed once the
device has been identified.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView, require_admin
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class TuyaMeshProbeView(HomeAssistantView):
    """Expose the latest probe document to an administrator."""

    name = "api:tuya_ble_mesh:probe"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, domain: str, url: str) -> None:
        self.hass = hass
        self.domain = domain
        self.url = url

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        """Return the latest probe result."""
        bucket: dict[str, Any] | None = None
        for value in self.hass.data.get(self.domain, {}).values():
            if isinstance(value, dict) and "result" in value:
                bucket = value
                break

        if bucket is None or bucket.get("result") is None:
            return web.json_response(
                {"ok": False, "error": "probe has not produced a result yet"},
                status=404,
                headers={"Cache-Control": "no-store"},
            )

        return web.json_response(
            bucket["result"],
            headers={"Cache-Control": "no-store"},
        )
