"""Conversions between Home Assistant light values and Tuya P2 data points.

This module is pure Python and must not import Home Assistant so the mapping
can be tested standalone. Each command group returned by
:func:`command_groups` is encoded into its own P2 frame by the caller.
"""

from __future__ import annotations

try:  # Imported as part of the integration package.
    from . import const
except ImportError:  # Imported directly by the unit tests.
    import const

_HA_BRIGHTNESS_MAX = 255
_HA_HUE_MAX = 360
_HA_PERCENT_MAX = 100
_MIRED_PER_KELVIN = 1_000_000

# Home Assistant mired limits corresponding to const.KELVIN_MIN/MAX.
MIRED_MIN = _MIRED_PER_KELVIN // const.KELVIN_MAX
MIRED_MAX = _MIRED_PER_KELVIN // const.KELVIN_MIN


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def brightness_to_dp(value: int) -> int:
    """Map Home Assistant brightness (0..255) to DP 3 (10..1000)."""
    value = _clamp(value, 0, _HA_BRIGHTNESS_MAX)
    span = const.BRIGHTNESS_MAX - const.BRIGHTNESS_MIN
    return const.BRIGHTNESS_MIN + round(value * span / _HA_BRIGHTNESS_MAX)


def mired_to_dp(value: int) -> int:
    """Map colour temperature in mireds (153..500) to DP 4 (1000..0)."""
    value = _clamp(value, MIRED_MIN, MIRED_MAX)
    span = const.TEMP_MAX - const.TEMP_MIN
    ratio = (MIRED_MAX - value) / (MIRED_MAX - MIRED_MIN)
    return const.TEMP_MIN + round(ratio * span)


def hs_to_dp11(h: int, s: int, v: int) -> bytes:
    """Encode hue/saturation/value as DP 11's four raw bytes."""
    h = _clamp(h, 0, _HA_HUE_MAX)
    s = _clamp(s, 0, _HA_PERCENT_MAX)
    v = _clamp(v, 0, _HA_PERCENT_MAX)
    return bytes([h >> 8, h & 0xFF, s, v])


def command_groups(
    cmd: dict,
) -> list[tuple[int, list[tuple[int, str, object]]]]:
    """Translate light service data into ordered, packed DP groups.

    Each group is sent as its own P2 frame. Compatible DPs are packed into
    one group while the encoded payload stays within 16 bytes, and the
    switch is always last. A colour command fits in one frame because its
    brightness is carried as the value component of DP 11. A colour
    temperature with brightness uses two frames so the switch can share the
    second frame with DP 3.
    """
    on = str(cmd.get("state", "ON")).upper() == "ON"
    brightness = cmd.get("brightness")
    color = cmd.get("color")
    color_temp = cmd.get("color_temp")

    if not on:
        return [(const.DP_SWITCH, [(const.DP_SWITCH, "bool", False)])]

    if color is not None:
        value = (
            round(int(brightness) * _HA_PERCENT_MAX / _HA_BRIGHTNESS_MAX)
            if brightness is not None
            else _HA_PERCENT_MAX
        )
        return [
            (
                const.DP_MODE,
                [
                    (const.DP_MODE, "enum", 1),
                    (
                        const.DP_COLOR,
                        "raw",
                        hs_to_dp11(int(color["h"]), int(color["s"]), value),
                    ),
                    (const.DP_SWITCH, "bool", True),
                ],
            )
        ]

    if color_temp is not None:
        mode_and_temp = [
            (const.DP_MODE, "enum", 0),
            (const.DP_TEMP, "value", mired_to_dp(int(color_temp))),
        ]
        if brightness is None:
            return [
                (
                    const.DP_MODE,
                    [*mode_and_temp, (const.DP_SWITCH, "bool", True)],
                )
            ]
        return [
            (const.DP_MODE, mode_and_temp),
            (
                const.DP_BRIGHT,
                [
                    (
                        const.DP_BRIGHT,
                        "value",
                        brightness_to_dp(int(brightness)),
                    ),
                    (const.DP_SWITCH, "bool", True),
                ],
            ),
        ]

    if brightness is not None:
        return [
            (
                const.DP_BRIGHT,
                [
                    (
                        const.DP_BRIGHT,
                        "value",
                        brightness_to_dp(int(brightness)),
                    ),
                    (const.DP_SWITCH, "bool", True),
                ],
            )
        ]

    return [(const.DP_SWITCH, [(const.DP_SWITCH, "bool", True)])]
