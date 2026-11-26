import asyncio
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import controller
import p2
from radio import RawAdvertisement

LOCAL = b"0123456789ABCDEF"
MASK = bytes.fromhex("00112233445566778899aabbccddeeff")
MAC = "AA:BB:CC:DD:EE:FF"


class FakeRadio:
    def __init__(self):
        self.sent = []
        self.ads = []
        self.fail = False
        self.scan_fail = False
        self.scan_calls = 0
        self.active = 0
        self.max_active = 0

    async def advertise(self, frame, seconds):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.fail:
                raise controller.CaptureError("radio failed")
            await asyncio.sleep(0)
            self.sent.append((frame, seconds))
        finally:
            self.active -= 1

    async def scan(self, seconds):
        self.scan_calls += 1
        if self.scan_fail:
            raise OSError("radio scan failed")
        return self.ads


def make_controller(tmp_path, radio=None, initial=0x4000):
    if radio is None:
        radio = FakeRadio()
    return controller.P2Controller(
        radio,
        MASK,
        controller.SequenceStore(tmp_path / "s.json", initial),
    )


def bulb():
    return controller.BulbConfig(MAC, LOCAL, 0x0010, "Office")


def p2_ad(frame):
    return b"\x02\x01\x06\x1b\x03" + frame


def test_sequence_persists(tmp_path):
    store = controller.SequenceStore(tmp_path / "seq.json", 0x4000)
    assert store.next() == 0x4001
    store2 = controller.SequenceStore(tmp_path / "seq.json", 0x4000)
    assert store2.next() == 0x4002


def test_sequence_wraps_without_zero(tmp_path):
    store = controller.SequenceStore(tmp_path / "seq.json", 0xFFFF)
    assert store.next() == 1
    store2 = controller.SequenceStore(tmp_path / "seq.json", 0x4000)
    assert store2.next() == 2


def test_sequence_store_writes_integer_json(tmp_path):
    path = tmp_path / "seq.json"
    controller.SequenceStore(path, 0x4000).next()
    data = json.loads(path.read_text())
    assert data == {"seq": 0x4001}


def test_sequence_store_file_is_private(tmp_path):
    path = tmp_path / "seq.json"
    controller.SequenceStore(path, 0x4000).next()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_sequence_stores_share_process_wide_allocation(tmp_path):
    path = tmp_path / "seq.json"
    first = controller.SequenceStore(path, 0x4000)
    second = controller.SequenceStore(path, 0x4000)

    values = [
        (first if index % 2 == 0 else second).next()
        for index in range(10)
    ]

    assert values == list(range(0x4001, 0x400B))
    assert json.loads(path.read_text())["seq"] == 0x400A
    restarted = controller.SequenceStore(path, 0x4000)
    assert restarted.next() == 0x400B


def test_sequence_allocation_is_thread_safe(tmp_path):
    path = tmp_path / "seq.json"
    stores = [controller.SequenceStore(path, 0x4000) for _ in range(2)]
    barrier = threading.Barrier(8)

    def allocate(index):
        barrier.wait()
        return stores[index % 2].next()

    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(allocate, range(24)))

    assert sorted(values) == list(range(0x4001, 0x4019))
    assert json.loads(path.read_text())["seq"] == 0x4018


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "expected a JSON object or a bare integer"),
        ('{"other": 1}', 'missing the "seq" integer'),
        ('{"seq": "abc"}', '"seq" must be an integer'),
        ('{"seq": true}', '"seq" must be an integer'),
        ('{"seq": 70000}', "between 0 and 65535"),
        ("-1", "between 0 and 65535"),
    ],
)
def test_corrupt_sequence_file_raises_sequence_error(tmp_path, content, detail):
    path = tmp_path / "seq.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(controller.SequenceError) as excinfo:
        controller.SequenceStore(path, 0x4000)

    message = str(excinfo.value)
    assert detail in message
    assert str(path) in message
    assert "Stop Home Assistant" in message


def test_sequence_store_accepts_bare_integer_file(tmp_path):
    path = tmp_path / "seq.json"
    path.write_text("4660", encoding="utf-8")

    store = controller.SequenceStore(path, 0x4000)
    assert store.value == 0x1234
    assert store.next() == 0x1235


def test_send_builds_frame_with_sequence(tmp_path):
    radio = FakeRadio()
    ctrl = make_controller(tmp_path, radio)
    asyncio.run(ctrl.send(bulb(), [(1, "bool", True)]))
    frame, seconds = radio.sent[0]
    assert seconds == 3.0
    parsed = p2.parse_frame(frame, LOCAL, MASK)
    assert parsed["src"] == 0x7D00
    assert parsed["dst"] == 0x0010
    assert parsed["sn"] == 0x4001
    assert parsed["payload"][:3] == p2.DP_ON_PAYLOAD


def test_send_uses_configured_source_and_advertising_seconds(tmp_path):
    radio = FakeRadio()
    ctrl = controller.P2Controller(
        radio,
        MASK,
        controller.SequenceStore(tmp_path / "s.json", 0x4000),
        source_id=0x7D02,
        advertising_seconds=1.5,
    )
    asyncio.run(ctrl.send(bulb(), [(1, "bool", False)]))
    frame, seconds = radio.sent[0]
    assert seconds == 1.5
    parsed = p2.parse_frame(frame, LOCAL, MASK)
    assert parsed["src"] == 0x7D02
    assert parsed["payload"][:3] == p2.DP_OFF_PAYLOAD


def test_sequence_is_persisted_before_advertising(tmp_path):
    path = tmp_path / "s.json"

    class ObservingRadio(FakeRadio):
        def __init__(self):
            super().__init__()
            self.saved_at_send = None

        async def advertise(self, frame, seconds):
            self.saved_at_send = json.loads(path.read_text())["seq"]
            await super().advertise(frame, seconds)

    radio = ObservingRadio()
    ctrl = controller.P2Controller(
        radio, MASK, controller.SequenceStore(path, 0x4000)
    )
    asyncio.run(ctrl.send(bulb(), [(1, "bool", True)]))
    assert radio.saved_at_send == 0x4001


def test_send_serializes_radio_operations(tmp_path):
    radio = FakeRadio()
    ctrl = make_controller(tmp_path, radio)

    async def run():
        await asyncio.gather(
            ctrl.send(bulb(), [(1, "bool", True)]),
            ctrl.send(bulb(), [(1, "bool", False)]),
        )

    asyncio.run(run())
    assert radio.max_active == 1
    assert len(radio.sent) == 2
    sequences = [p2.parse_frame(frame, LOCAL, MASK)["sn"] for frame, _ in radio.sent]
    assert sequences == [0x4001, 0x4002]


def test_scan_parses_p2_advertisements(tmp_path):
    frame = p2.build(1, 5, 0x7D01, 0x0010, 7, p2.DP_ON_PAYLOAD, LOCAL, MASK)
    radio = FakeRadio()
    radio.ads = [RawAdvertisement(MAC, -42, p2_ad(frame))]
    ads = asyncio.run(make_controller(tmp_path, radio).scan(1))
    assert ads == [
        controller.P2Advertisement(MAC, -42, frame, 0x7D01, 0x0010, 7, 5)
    ]


def test_scan_skips_malformed_advertisements(tmp_path):
    frame = p2.build(1, 5, 0x7D01, 0x0010, 8, p2.DP_ON_PAYLOAD, LOCAL, MASK)
    radio = FakeRadio()
    radio.ads = [
        RawAdvertisement("11:22:33:44:55:66", -50, b"\x02\x01\x06"),
        RawAdvertisement("11:22:33:44:55:67", -51, b"\x05\xff\x03\x1b\x03" + frame),
        RawAdvertisement("11:22:33:44:55:68", -52, b"\x02\x01\x06\x1b\x03\x00" * 2),
        RawAdvertisement(MAC, -53, p2_ad(frame)),
    ]
    ads = asyncio.run(make_controller(tmp_path, radio).scan(1))
    assert len(ads) == 1
    assert ads[0].mac == MAC
    assert ads[0].seq == 8


def test_capture_requires_matching_off_and_on(tmp_path):
    off = p2.build(1, 5, 0x7D01, 0x0010, 1, p2.DP_OFF_PAYLOAD, LOCAL, MASK)
    on = p2.build(1, 5, 0x7D01, 0x0010, 2, p2.DP_ON_PAYLOAD, LOCAL, MASK)
    radio = FakeRadio()
    radio.ads = [
        RawAdvertisement("AA", -50, b"\x02\x01\x06\x1b\x03" + off),
        RawAdvertisement("AA", -50, b"\x02\x01\x06\x1b\x03" + on),
    ]
    ctrl = make_controller(tmp_path, radio)
    assert asyncio.run(ctrl.capture_beacon_key(0x0010, LOCAL, seconds=1)) == MASK


def test_capture_requires_both_off_and_on(tmp_path):
    off = p2.build(1, 5, 0x7D01, 0x0010, 1, p2.DP_OFF_PAYLOAD, LOCAL, MASK)
    radio = FakeRadio()
    radio.ads = [RawAdvertisement("AA", -50, p2_ad(off))]
    ctrl = make_controller(tmp_path, radio)
    with pytest.raises(controller.CaptureError):
        asyncio.run(ctrl.capture_beacon_key(0x0010, LOCAL, seconds=1))


def test_capture_ignores_bulb_reports_and_non_commands(tmp_path):
    report = p2.build(1, 5, 0x0010, 0x8000, 1, p2.DP_ON_PAYLOAD, MASK, MASK)
    on = p2.build(1, 5, 0x7D01, 0x0010, 2, p2.DP_ON_PAYLOAD, LOCAL, MASK)
    other = p2.build(1, 5, 0x7D01, 0x0011, 3, p2.DP_ON_PAYLOAD, LOCAL, MASK)
    heartbeat = p2.build(0, 5, 0x7D01, 0x0010, 4, p2.DP_ON_PAYLOAD, LOCAL, MASK)
    radio = FakeRadio()
    radio.ads = [
        RawAdvertisement("AA", -50, p2_ad(report)),
        RawAdvertisement("AA", -50, p2_ad(other)),
        RawAdvertisement("AA", -50, p2_ad(heartbeat)),
        RawAdvertisement("AA", -50, p2_ad(on)),
    ]
    ctrl = make_controller(tmp_path, radio)
    with pytest.raises(controller.CaptureError):
        asyncio.run(ctrl.capture_beacon_key(0x0010, LOCAL, seconds=1))


def test_capture_rejects_invalid_local_key_before_scanning(tmp_path, caplog):
    radio = FakeRadio()
    ctrl = make_controller(tmp_path, radio)
    invalid_key = b"0123456789abcde"  # synthetic 15-byte key
    with pytest.raises(controller.CaptureError) as excinfo:
        asyncio.run(ctrl.capture_beacon_key(0x0010, invalid_key, seconds=1))
    message = str(excinfo.value)
    assert message == "Local key is invalid; re-enter the key from the Tuya app."
    assert invalid_key.decode() not in message
    assert invalid_key.decode() not in caplog.text
    assert radio.scan_calls == 0


def test_capture_radio_failure_raises_capture_error(tmp_path):
    radio = FakeRadio()
    radio.scan_fail = True
    ctrl = make_controller(tmp_path, radio)
    with pytest.raises(controller.CaptureError):
        asyncio.run(ctrl.capture_beacon_key(0x0010, LOCAL, seconds=1))


def test_test_sends_on_then_off(tmp_path):
    radio = FakeRadio()
    ctrl = make_controller(tmp_path, radio)
    asyncio.run(ctrl.test(bulb()))
    assert len(radio.sent) == 2
    parsed = [p2.parse_frame(frame, LOCAL, MASK) for frame, _ in radio.sent]
    assert [entry["payload"][:3] for entry in parsed] == [
        p2.DP_ON_PAYLOAD,
        p2.DP_OFF_PAYLOAD,
    ]
    assert [entry["sn"] for entry in parsed] == [0x4001, 0x4002]


def test_available_tracks_radio_operations(tmp_path):
    radio = FakeRadio()
    ctrl = make_controller(tmp_path, radio)

    async def run():
        assert ctrl.available is True
        radio.fail = True
        with pytest.raises(controller.CaptureError):
            await ctrl.send(bulb(), [(1, "bool", True)])
        assert ctrl.available is False
        radio.fail = False
        await ctrl.send(bulb(), [(1, "bool", True)])
        assert ctrl.available is True

    asyncio.run(run())
