import dp
import p2


def test_brightness_mapping():
    assert dp.brightness_to_dp(0) == 10
    assert dp.brightness_to_dp(128) == 507
    assert dp.brightness_to_dp(255) == 1000


def test_brightness_clamps():
    assert dp.brightness_to_dp(-1) == 10
    assert dp.brightness_to_dp(300) == 1000


def test_mired_mapping():
    assert dp.mired_to_dp(153) == 1000
    assert dp.mired_to_dp(250) == 720
    assert dp.mired_to_dp(500) == 0


def test_mired_clamps():
    assert dp.mired_to_dp(100) == 1000
    assert dp.mired_to_dp(600) == 0


def test_hs_payload():
    assert dp.hs_to_dp11(240, 100, 50) == bytes([0, 240, 100, 50])


def test_hs_payload_clamps():
    assert dp.hs_to_dp11(400, 150, 150) == bytes([1, 104, 100, 100])


def test_color_command_groups():
    groups = dp.command_groups(
        {"state": "ON", "color": {"h": 0, "s": 100}, "brightness": 255}
    )
    assert groups == [
        (
            2,
            [
                (2, "enum", 1),
                (11, "raw", b"\x00\x00dd"),
                (1, "bool", True),
            ],
        )
    ]
    assert [len(p2.encode_dps(dps)) for _, dps in groups] == [12]


def test_color_temp_command_groups():
    groups = dp.command_groups(
        {"state": "ON", "color_temp": 250, "brightness": 128}
    )
    assert groups == [
        (2, [(2, "enum", 0), (4, "value", 720)]),
        (3, [(3, "value", 507), (1, "bool", True)]),
    ]
    assert [len(p2.encode_dps(dps)) for _, dps in groups] == [9, 9]


def test_color_temp_without_brightness_command_groups():
    groups = dp.command_groups({"state": "ON", "color_temp": 250})
    assert groups == [
        (2, [(2, "enum", 0), (4, "value", 720), (1, "bool", True)])
    ]
    assert [len(p2.encode_dps(dps)) for _, dps in groups] == [12]


def test_brightness_only_command_groups():
    groups = dp.command_groups({"state": "ON", "brightness": 128})
    assert groups == [
        (3, [(3, "value", 507), (1, "bool", True)]),
    ]
    assert [len(p2.encode_dps(dps)) for _, dps in groups] == [9]


def test_bare_on_command_groups():
    groups = dp.command_groups({"state": "ON"})
    assert groups == [(1, [(1, "bool", True)])]
    assert [len(p2.encode_dps(dps)) for _, dps in groups] == [3]


def test_off_command_groups():
    groups = dp.command_groups({"state": "OFF"})
    assert groups == [(1, [(1, "bool", False)])]
    assert [len(p2.encode_dps(dps)) for _, dps in groups] == [3]
