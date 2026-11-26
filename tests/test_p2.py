import p2

KEY = bytes.fromhex("0123456789abcdeffedcba9876543210")
PLAIN = bytes.fromhex("00112233445566778899aabbccddeeff")
LOCAL = b"0123456789ABCDEF"
MASK = bytes.fromhex("00112233445566778899aabbccddeeff")


def test_xxtea_known_vector():
    assert p2.xxtea_encrypt(PLAIN, KEY).hex() == "2cb628af2b144a8d17eb1e3d83d1b56f"
    assert p2.xxtea_decrypt(bytes.fromhex("2cb628af2b144a8d17eb1e3d83d1b56f"), KEY) == PLAIN


def test_advertisement_is_flags_first_and_31_bytes():
    frame = p2.build(1, 5, 0x0001, 0x8002, 0x1234, b"\x01\x01\x01\x01", KEY, b"\xa5" * 16)
    adv = p2.advertisement(frame)
    assert len(adv) == 31
    assert adv[:5] == b"\x02\x01\x06\x1b\x03"
    assert adv[5:] == frame


def test_frame_round_trip_known_vector():
    frame = p2.build(1, 5, 0x0001, 0x8002, 0x1234, b"\x01\x01\x01\x01", KEY, b"\xa5" * 16)
    assert frame.hex() == "0b00018002123405c64f318331c42d21ffa70308a40769030074"


def test_parse_frame_any_destination():
    frame = p2.build(1, 5, 0x7D00, 0x0010, 0x1234, p2.DP_ON_PAYLOAD, LOCAL, MASK)
    parsed = p2.parse_frame(frame, LOCAL, MASK)
    assert parsed["src"] == 0x7D00
    assert parsed["dst"] == 0x0010
    assert parsed["sn"] == 0x1234
    assert parsed["payload"][:3] == p2.DP_ON_PAYLOAD


def test_derive_beacon_key_from_off_and_on():
    off = p2.build(1, 5, 0x7D00, 0x0010, 1, p2.DP_OFF_PAYLOAD, LOCAL, MASK)
    on = p2.build(1, 5, 0x7D00, 0x0010, 2, p2.DP_ON_PAYLOAD, LOCAL, MASK)
    assert p2.derive_beacon_key(off, LOCAL, p2.DP_OFF_PAYLOAD) == MASK
    assert p2.derive_beacon_key(on, LOCAL, p2.DP_ON_PAYLOAD) == MASK


def test_derive_beacon_key_rejects_wrong_payload():
    off = p2.build(1, 5, 0x7D00, 0x0010, 1, p2.DP_OFF_PAYLOAD, LOCAL, MASK)
    try:
        p2.derive_beacon_key(off, LOCAL, p2.DP_ON_PAYLOAD)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_dp11_raw_round_trip():
    payload = p2.encode_dps([(11, "raw", bytes([0, 240, 100, 50]))])
    assert payload.hex() == "0b0400f06432"
    assert p2.decode_dps(payload) == [(11, 0, bytes([0, 240, 100, 50]))]


def test_value_dps_are_four_bytes():
    assert p2.encode_dps([(3, "value", 507)]).hex() == "0324000001fb"


def test_parse_frame_uses_separate_crypto_keys():
    frame = p2.build(1, 5, 0x7D00, 0x0010, 9, p2.DP_ON_PAYLOAD, KEY, MASK)
    parsed = p2.parse_frame(frame, KEY, MASK)
    assert parsed is not None
    assert parsed["payload"][:3] == p2.DP_ON_PAYLOAD
    assert p2.parse_frame(frame, MASK, KEY) is None


def test_parse_device_accepts_report_destination():
    frame = p2.build(1, 5, 0x1234, 0x8000, 0x0102, p2.DP_ON_PAYLOAD, MASK, MASK)
    parsed = p2.parse_device(frame, MASK)
    assert parsed is not None
    assert parsed["src"] == 0x1234
    assert parsed["dst"] == 0x8000
    assert parsed["sn"] == 0x0102
    assert parsed["payload"][:3] == p2.DP_ON_PAYLOAD


def test_parse_device_rejects_non_report_destination():
    frame = p2.build(1, 5, 0x7D00, 0x0010, 1, p2.DP_ON_PAYLOAD, MASK, MASK)
    assert p2.parse_device(frame, MASK) is None


def test_parse_frame_rejects_tampered_frame():
    frame = bytearray(p2.build(1, 5, 0x7D00, 0x0010, 1, p2.DP_ON_PAYLOAD, KEY, MASK))
    frame[10] ^= 0x01
    assert p2.parse_frame(bytes(frame), KEY, MASK) is None


def test_advertisement_rejects_wrong_frame_length():
    try:
        p2.advertisement(b"\x00" * 25)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
