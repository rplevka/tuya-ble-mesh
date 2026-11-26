import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from homeassistant import runner

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "custom_components" / "tuya_p2_mesh"))

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest_asyncio.fixture(autouse=True)
async def configure_event_loop() -> None:
    """Override the harness fixture so sync tests can use it too."""
    runner.configure_event_loop(asyncio.get_running_loop())


@pytest_asyncio.fixture(autouse=True)
async def enable_event_loop_debug() -> None:
    """Override the harness fixture so sync tests can use it too."""
    asyncio.get_running_loop().set_debug(True)


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir):
    """Give every test an isolated Home Assistant config directory."""
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
