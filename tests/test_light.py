"""Regression tests for the Tuya P2 light entity.

Home Assistant is not installed in the unit-test environment, so these tests
load ``light.py`` with a small faithful stub of the Home Assistant light
platform. If Home Assistant is available, the real modules are used instead.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

COMPONENT_DIR = (
    Path(__file__).parents[1] / "custom_components" / "tuya_p2_mesh"
)
PACKAGE = "_tuya_p2_mesh_light_test"


class StubHomeAssistantError(Exception):
    """Stand-in for ``homeassistant.exceptions.HomeAssistantError``."""


class StubColorMode:
    """Stand-in for ``homeassistant.components.light.ColorMode``."""

    HS = "hs"
    COLOR_TEMP = "color_temp"


class StubLightEntity:
    """Minimal LightEntity with the contract that matters here.

    ``LightEntity.state_attributes`` raises when an on light does not report
    a colour mode, and when the colour mode is not supported.
    """

    _attr_is_on: bool | None = None
    _attr_brightness: int | None = None
    _attr_color_mode: str | None = None
    _attr_hs_color: tuple[float, float] | None = None
    _attr_color_temp_kelvin: int | None = None
    _attr_supported_color_modes: set[str] | None = None
    _attr_min_color_temp_kelvin: int | None = None
    _attr_max_color_temp_kelvin: int | None = None
    _attr_assumed_state = False
    _attr_should_poll = True
    _attr_name: str | None = None
    _attr_unique_id: str | None = None
    _attr_device_info: Any = None

    @property
    def is_on(self) -> bool | None:
        return self._attr_is_on

    @property
    def brightness(self) -> int | None:
        return self._attr_brightness

    @property
    def color_mode(self) -> str | None:
        return self._attr_color_mode

    @property
    def hs_color(self) -> tuple[float, float] | None:
        return self._attr_hs_color

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._attr_color_temp_kelvin

    @property
    def supported_color_modes(self) -> set[str] | None:
        return self._attr_supported_color_modes

    @property
    def min_color_temp_kelvin(self) -> int | None:
        return self._attr_min_color_temp_kelvin

    @property
    def max_color_temp_kelvin(self) -> int | None:
        return self._attr_max_color_temp_kelvin

    @property
    def assumed_state(self) -> bool:
        return self._attr_assumed_state

    @property
    def should_poll(self) -> bool:
        return self._attr_should_poll

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def state_attributes(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        is_on = self.is_on
        color_mode = self.color_mode if is_on else None
        if is_on and color_mode is None:
            raise StubHomeAssistantError("Light does not report a color mode")

        supported = self.supported_color_modes or set()
        if color_mode is not None and color_mode not in supported:
            raise StubHomeAssistantError(
                f"Light set to unsupported color mode {color_mode}"
            )

        data["color_mode"] = color_mode
        if color_mode == StubColorMode.COLOR_TEMP:
            data["color_temp_kelvin"] = self.color_temp_kelvin
        if color_mode == StubColorMode.HS:
            data["hs_color"] = self.hs_color
        return data

    def async_write_ha_state(self) -> None:
        raise AssertionError("test must patch async_write_ha_state")


def _install_ha_stubs() -> dict[str, types.ModuleType | None]:
    """Install the minimal Home Assistant modules needed by light.py.

    Return the modules that were present before, so the caller can remove
    the stubs again and leave ``sys.modules`` clean for the HA test harness.
    """
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []  # type: ignore[attr-defined]
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []  # type: ignore[attr-defined]
    light_component = types.ModuleType("homeassistant.components.light")
    light_component.ATTR_BRIGHTNESS = "brightness"
    light_component.ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"
    light_component.ATTR_HS_COLOR = "hs_color"
    light_component.ColorMode = StubColorMode
    light_component.LightEntity = StubLightEntity
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = StubHomeAssistantError
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []  # type: ignore[attr-defined]
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.light": light_component,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.entity_platform": entity_platform,
    }
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    return previous


def _restore_ha_modules(
    previous: dict[str, types.ModuleType | None],
) -> None:
    """Restore ``sys.modules`` after loading light.py with stubs."""
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _load_light_module() -> types.ModuleType:
    """Load light.py as a package so its relative imports resolve."""
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
        sys.modules[PACKAGE] = package

    if f"{PACKAGE}.light" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            f"{PACKAGE}.light", COMPONENT_DIR / "light.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{PACKAGE}.light"]


def _load_light() -> types.ModuleType:
    """Load light.py with real Home Assistant, or with temporary stubs."""
    try:
        import homeassistant.components.light  # noqa: F401
    except ImportError:
        previous = _install_ha_stubs()
        try:
            return _load_light_module()
        finally:
            _restore_ha_modules(previous)
    return _load_light_module()


light = _load_light()


class FakeController:
    """Controller double that records state writes and sends in order."""

    available = True

    def __init__(self) -> None:
        self.calls: list[tuple[Any, list[tuple[int, str, object]]]] = []
        self.events: list[tuple[str, Any]] = []
        self.fail = False

    async def send(self, bulb: Any, dps: list[tuple[int, str, object]]) -> None:
        self.events.append(("send", dps))
        if self.fail:
            self.available = False
            raise OSError("radio failed")
        self.calls.append((bulb, dps))


def make_entity() -> tuple[Any, FakeController, list[dict[str, Any]]]:
    """Create an entity with a state writer that emulates HA's write path."""
    controller = FakeController()
    bulb = light.BulbConfig(
        "AA:BB:CC:DD:EE:FF", b"0123456789ABCDEF", 16, "Office"
    )
    entity = light.TuyaP2Light(controller, bulb)
    writes: list[dict[str, Any]] = []

    def write_state() -> None:
        # Home Assistant computes state_attributes while writing state.
        attributes = entity.state_attributes
        writes.append(attributes)
        controller.events.append(("write", attributes))

    entity.async_write_ha_state = write_state
    return entity, controller, writes


def test_bare_turn_on_writes_state_with_default_color_mode() -> None:
    entity, controller, writes = make_entity()

    asyncio.run(entity.async_turn_on())

    assert controller.calls == [(entity._bulb, [(1, "bool", True)])]
    assert entity.is_on is True
    assert entity.color_mode == light.ColorMode.COLOR_TEMP
    assert writes[-1]["color_mode"] == light.ColorMode.COLOR_TEMP


def test_brightness_only_turn_on_preserves_color_mode() -> None:
    entity, controller, writes = make_entity()
    default_mode = entity.color_mode
    assert default_mode == light.ColorMode.COLOR_TEMP

    asyncio.run(entity.async_turn_on(brightness=128))

    assert entity.brightness == 128
    assert entity.color_mode == default_mode
    assert writes[-1]["color_mode"] == default_mode
    assert controller.calls[-1:] == [
        (entity._bulb, [(3, "value", 507), (1, "bool", True)]),
    ]


def test_turn_on_writes_optimistic_state_before_sending() -> None:
    entity, controller, writes = make_entity()

    asyncio.run(entity.async_turn_on(brightness=128))

    assert [kind for kind, _ in controller.events] == ["write", "send"]
    assert writes[-1]["color_mode"] == light.ColorMode.COLOR_TEMP
    assert entity.is_on is True
    assert entity.brightness == 128


def test_turn_off_writes_optimistic_state_before_sending() -> None:
    entity, controller, writes = make_entity()

    asyncio.run(entity.async_turn_off())

    assert [kind for kind, _ in controller.events] == ["write", "send"]
    assert entity.is_on is False
    assert writes[-1]["color_mode"] is None
    assert controller.calls == [(entity._bulb, [(1, "bool", False)])]


def test_send_failure_keeps_optimistic_state_and_raises() -> None:
    entity, controller, writes = make_entity()
    controller.fail = True

    with pytest.raises(OSError):
        asyncio.run(entity.async_turn_on(brightness=128))

    assert [kind for kind, _ in controller.events] == ["write", "send"]
    assert entity.is_on is True
    assert entity.brightness == 128
    assert writes[-1]["color_mode"] == light.ColorMode.COLOR_TEMP
    assert entity.available is False


def test_hs_turn_on_sets_hs_color_mode() -> None:
    entity, _controller, writes = make_entity()

    asyncio.run(entity.async_turn_on(hs_color=(240.0, 100.0)))

    assert entity.color_mode == light.ColorMode.HS
    assert entity.hs_color == (240.0, 100.0)
    assert writes[-1]["color_mode"] == light.ColorMode.HS


def test_color_temp_turn_on_sets_color_temp_mode() -> None:
    entity, _controller, writes = make_entity()

    asyncio.run(entity.async_turn_on(color_temp_kelvin=2700))

    assert entity.color_mode == light.ColorMode.COLOR_TEMP
    assert entity.color_temp_kelvin == 2700
    assert writes[-1]["color_mode"] == light.ColorMode.COLOR_TEMP
