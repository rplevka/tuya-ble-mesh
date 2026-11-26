import json
from pathlib import Path

import const

COMPONENT = Path("custom_components/tuya_p2_mesh")


def _load_json(name: str) -> dict:
    return json.loads((COMPONENT / name).read_text())


def test_manifest_is_valid_and_config_flow_enabled():
    m = json.loads((Path("custom_components/tuya_p2_mesh/manifest.json")).read_text())
    assert m["domain"] == const.DOMAIN
    assert m["config_flow"] is True
    assert m["version"]


def test_dp_and_defaults():
    assert const.DP_SWITCH == 1
    assert const.DP_COLOR == 11
    assert const.DEFAULT_SOURCE_ID == 0x7D00
    assert const.DEFAULT_SEQUENCE_SEED == 0x4000


def test_manifest_metadata():
    m = _load_json("manifest.json")
    assert m["name"] == "Tuya P2 Mesh"
    assert m["version"] == "0.1.0"
    assert m["iot_class"] == "local_polling"
    assert m["documentation"] == "https://github.com/rplevka/tuya-ble-mesh"
    assert m["issue_tracker"] == "https://github.com/rplevka/tuya-ble-mesh/issues"
    assert "requirements" not in m


def test_dp_map():
    assert const.DP_SWITCH == 1
    assert const.DP_MODE == 2
    assert const.DP_BRIGHT == 3
    assert const.DP_TEMP == 4
    assert const.DP_COLOR == 11


def test_defaults():
    assert const.DOMAIN == "tuya_p2_mesh"
    assert const.DEFAULT_ADAPTER == "hci0"
    assert const.DEFAULT_SOURCE_ID == 0x7D00
    assert const.DEFAULT_SEQUENCE_SEED == 0x4000
    assert const.DEFAULT_ADVERTISING_SECONDS == 0.5


def test_brightness_and_temperature_ranges():
    assert const.BRIGHTNESS_MIN == 10
    assert const.BRIGHTNESS_MAX == 1000
    assert const.TEMP_MIN == 0
    assert const.TEMP_MAX == 1000
    assert const.KELVIN_MIN == 2000
    assert const.KELVIN_MAX == 6500


def test_conf_constants():
    assert const.CONF_ADAPTER == "adapter"
    assert const.CONF_SOURCE_ID == "source_id"
    assert const.CONF_SEQUENCE_SEED == "sequence_seed"
    assert const.CONF_BEACON_KEY == "beacon_key"
    assert const.CONF_BULBS == "bulbs"
    assert const.CONF_MAC == "mac"
    assert const.CONF_NODE_ID == "node_id"
    assert const.CONF_LOCAL_KEY == "local_key"
    assert const.CONF_NAME == "name"
    assert const.CONF_ADVERTISING_SECONDS == "advertising_seconds"


def test_strings_match_translations_and_cover_config_flow():
    strings = _load_json("strings.json")
    translations = _load_json("translations/en.json")
    assert translations == strings

    config = strings["config"]
    assert set(config["step"]) >= {
        "user",
        "scan",
        "device",
        "capture",
        "test",
        "reauth_confirm",
        "reconfigure",
    }
    for step in config["step"].values():
        assert step["title"]

    assert set(config["step"]["user"]["data"]) >= {
        "adapter",
        "source_id",
        "sequence_seed",
        "advertising_seconds",
    }
    assert set(config["step"]["device"]["data"]) >= {
        "mac",
        "node_id",
        "local_key",
        "name",
        "beacon_key",
    }
    assert config["error"]
    assert set(config["abort"]) >= {
        "already_configured",
        "no_adapter",
        "reauth_successful",
        "reconfigure_successful",
    }

    options = strings["options"]
    assert set(options["step"]) >= {"init", "add_device"}
    assert options["step"]["init"]["title"]
    assert options["step"]["add_device"]["title"]
