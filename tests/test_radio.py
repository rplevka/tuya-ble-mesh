import asyncio
import struct
import threading
import time

import pytest

import radio

ADDRESS = bytes.fromhex("060504030201")
ADDRESS_STR = "01:02:03:04:05:06"
FOREIGN_ADDRESS = bytes.fromhex("0a0908070605")
FOREIGN_ADDRESS_STR = "05:06:07:08:09:0A"


def legacy_scan_report(address: bytes, data: bytes, rssi: int) -> bytes:
    """Build a synthetic HCI LE Advertising Report event (legacy)."""
    report = (
        b"\x01"  # event type: connectable undirected
        + b"\x00"  # address type: public
        + address
        + bytes([len(data)])
        + data
        + struct.pack("b", rssi)
    )
    return b"\x04\x3e" + bytes([len(report) + 2]) + b"\x02\x01" + report


def extended_scan_report(address: bytes, data: bytes, rssi: int) -> bytes:
    """Build a synthetic HCI LE Advertising Report event (extended)."""
    report = (
        b"\x13\x00"  # event type
        + b"\x01"  # address type: random
        + address
        + b"\x01"  # primary PHY: LE 1M
        + b"\x01"  # secondary PHY: LE 1M
        + b"\x00"  # advertising SID
        + b"\x7f"  # TX power: no preference
        + struct.pack("b", rssi)
        + b"\x00\x00"  # periodic advertising interval
        + b"\x00"  # direct address type
        + b"\x00" * 6  # direct address
        + bytes([len(data)])
        + data
    )
    return b"\x04\x3e" + bytes([len(report) + 2]) + b"\x0d\x01" + report


def p2_ad(frame: bytes) -> bytes:
    return b"\x02\x01\x06\x1b\x03" + frame


def test_find_adapters(tmp_path):
    (tmp_path / "hci0").mkdir()
    (tmp_path / "hci1").mkdir()
    (tmp_path / "not-hci").mkdir()
    assert radio.find_adapters(tmp_path) == ["hci0", "hci1"]


def test_find_adapters_missing_root(tmp_path):
    assert radio.find_adapters(tmp_path / "missing") == []


def test_parse_p2_advertisement_flags_first():
    frame = bytes(range(26))
    raw = b"\x02\x01\x06\x1b\x03" + frame
    assert radio.parse_p2_advertisement(raw) == frame


def test_parse_p2_advertisement_skips_other_structures():
    frame = bytes(range(26))
    foreign = b"\x03\xff\x00\x00"
    assert radio.parse_p2_advertisement(foreign + p2_ad(frame)) == frame
    assert radio.parse_p2_advertisement(foreign) is None


def test_parse_p2_advertisement_ignores_malformed():
    assert radio.parse_p2_advertisement(b"\x00") is None
    assert radio.parse_p2_advertisement(b"\x02") is None
    assert radio.parse_p2_advertisement(b"\x1b\x03" + bytes(3)) is None
    assert radio.parse_p2_advertisement(b"\x02\x01") is None
    assert radio.parse_p2_advertisement(b"\x1b\x09" + bytes(26)) is None


def test_parse_hci_scan_report_legacy():
    ad = p2_ad(bytes(range(26)))
    report = radio.parse_hci_scan_report(legacy_scan_report(ADDRESS, ad, -60))
    assert report == radio.RawAdvertisement(ADDRESS_STR, -60, ad)


def test_parse_hci_scan_report_extended():
    ad = p2_ad(bytes(range(26)))
    report = radio.parse_hci_scan_report(extended_scan_report(ADDRESS, ad, -42))
    assert report == radio.RawAdvertisement(ADDRESS_STR, -42, ad)


def test_parse_hci_scan_report_ignores_malformed():
    header = b"\x04\x3e"
    assert radio.parse_hci_scan_report(b"") is None
    assert radio.parse_hci_scan_report(b"\x04") is None
    assert radio.parse_hci_scan_report(header + b"\x02\x02\x01") is None
    assert radio.parse_hci_scan_report(header + b"\x02\x0d\x01") is None
    assert radio.parse_hci_scan_report(header + b"\x02\x09\x01") is None
    assert radio.parse_hci_scan_report(b"\x04\x0e\x02\x01\x03\x0c") is None
    # A legitimate report whose declared data length runs past the packet.
    truncated = (
        header
        + b"\x05\x02\x01"
        + b"\x01\x00"
        + ADDRESS
        + b"\x1f"
        + bytes(3)
    )
    assert radio.parse_hci_scan_report(truncated) is None


def test_advertise_sends_flags_first_data(monkeypatch):
    frame = bytes(range(26))
    calls = []
    monkeypatch.setattr("time.sleep", lambda _s: None)
    r = radio.P2Radio(
        "hci0",
        command_fn=lambda ocf, payload=b"", allow_disabled=False: calls.append(
            (ocf, payload)
        ),
    )
    r.extended = False
    r.advertise(frame, seconds=0.01)
    data_calls = [payload for ocf, payload in calls if ocf == 0x08]
    assert data_calls[0] == bytes([31]) + b"\x02\x01\x06\x1b\x03" + frame


def test_advertise_extended_uses_extended_data(monkeypatch):
    frame = bytes(range(26))
    calls = []
    monkeypatch.setattr("time.sleep", lambda _s: None)
    r = radio.P2Radio(
        "hci0",
        command_fn=lambda ocf, payload=b"", allow_disabled=False: calls.append(
            (ocf, payload)
        ),
    )
    r.extended = True
    r.advertise(frame, seconds=0.01)
    data_calls = [payload for ocf, payload in calls if ocf == 0x37]
    assert data_calls[0] == bytes([1, 3, 1, 31]) + p2_ad(frame)
    assert (0x08, bytes([31]) + p2_ad(frame)) not in calls
    assert any(ocf == 0x39 for ocf, _payload in calls)


def test_command_sends_raw_hci_packet(monkeypatch):
    sock = FakeSocket([b"\x04\x0e\x04\x01\x08\x20\x00"])
    monkeypatch.setattr(radio.socket, "socket", lambda *args: sock)
    r = radio.P2Radio("hci0")

    assert r.command(0x08, b"\x01") == b""
    assert sock.bound == (0,)
    assert sock.sent == [b"\x01\x08\x20\x01\x01"]


def test_command_status_error_becomes_radio_error(monkeypatch):
    sock = FakeSocket([b"\x04\x0f\x04\x12\x01\x08\x20"])
    monkeypatch.setattr(radio.socket, "socket", lambda *args: sock)
    r = radio.P2Radio("hci0")

    with pytest.raises(radio.RadioError, match="status 0x12"):
        r.command(0x08, b"\x01")


def test_command_oserror_becomes_radio_error():
    def failing_command(ocf, payload=b"", allow_disabled=False):
        raise OSError(13, "Permission denied")

    r = radio.P2Radio("hci0", command_fn=failing_command)
    with pytest.raises(radio.RadioError) as excinfo:
        r.command(0x03)
    assert "hci0" in str(excinfo.value)


def test_scan_returns_p2_reports_and_disables_scanning(monkeypatch):
    frame = bytes(range(26))
    ad = p2_ad(frame)
    foreign_ad = b"\x02\x01\x06"
    packets = [
        b"\x00",
        legacy_scan_report(ADDRESS, ad, -40),
        legacy_scan_report(FOREIGN_ADDRESS, foreign_ad, -90),
    ]
    sock = FakeSocket(packets)
    monkeypatch.setattr(radio.socket, "socket", lambda *args: sock)
    calls = []
    r = radio.P2Radio(
        "hci0",
        command_fn=lambda ocf, payload=b"", allow_disabled=False: calls.append(
            (ocf, payload)
        ),
    )
    r.extended = False

    advertisements = r.scan(0.5)

    assert advertisements == [radio.RawAdvertisement(ADDRESS_STR, -40, ad)]
    assert sock.closed
    assert (0x0C, b"\x01\0") in calls
    assert calls[-1] == (0x0C, b"\0\0")


def test_scan_socket_error_becomes_radio_error(monkeypatch):
    def failing_socket(*args):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(radio.socket, "socket", failing_socket)
    r = radio.P2Radio("hci0", command_fn=lambda *args, **kwargs: b"")
    r.extended = False
    with pytest.raises(radio.RadioError) as excinfo:
        r.scan(0.1)
    assert "hci0" in str(excinfo.value)


def test_close_prevents_further_commands():
    r = radio.P2Radio("hci0", command_fn=lambda *args, **kwargs: b"")
    r.close()
    with pytest.raises(radio.RadioError):
        r.command(0x03)


def test_invalid_adapter_name():
    with pytest.raises(ValueError):
        radio.P2Radio("ttyUSB0")


def test_radios_for_same_adapter_share_adapter_lock():
    first = radio.P2Radio("hci0")
    second = radio.P2Radio("hci0")
    other = radio.P2Radio("hci1")

    assert first._lock is second._lock
    assert first._lock is not other._lock


def test_radios_for_same_adapter_serialize_commands():
    started = threading.Barrier(2)
    guard = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def command(ocf, payload=b"", allow_disabled=False):
        with guard:
            state["active"] += 1
            state["max_active"] = max(
                state["max_active"], state["active"]
            )
        time.sleep(0.02)
        with guard:
            state["active"] -= 1
        return b""

    first = radio.P2Radio("hci0", command_fn=command)
    second = radio.P2Radio("hci0", command_fn=command)

    def run(instance):
        started.wait()
        instance.command(0x03)

    threads = [
        threading.Thread(target=run, args=(instance,))
        for instance in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["max_active"] == 1


def test_async_radio_delegates_to_executor():
    class FakeHass:
        def __init__(self):
            self.jobs = []

        async def async_add_executor_job(self, target, *args):
            self.jobs.append((target, args))
            return target(*args)

    class FakeRadio:
        def __init__(self):
            self.sent = []
            self.scanned = [radio.RawAdvertisement(ADDRESS_STR, -50, b"ad")]

        def advertise(self, frame, seconds):
            self.sent.append((frame, seconds))

        def scan(self, seconds):
            return list(self.scanned)

    hass = FakeHass()
    fake = FakeRadio()
    async_radio = radio.AsyncP2Radio(hass, fake)

    asyncio.run(async_radio.advertise(b"\x00" * 26, 2.0))
    advertisements = asyncio.run(async_radio.scan(1.5))

    assert fake.sent == [(b"\x00" * 26, 2.0)]
    assert advertisements == fake.scanned
    assert [job[0] for job in hass.jobs] == [fake.advertise, fake.scan]


class FakeSocket:
    def __init__(self, packets):
        self.packets = list(packets)
        self.closed = False
        self.bound = None
        self.sent = []
        self.sockopts = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def bind(self, address):
        self.bound = address

    def setsockopt(self, *args):
        self.sockopts.append(args)

    def settimeout(self, _timeout):
        pass

    def send(self, data):
        self.sent.append(data)

    def recv(self, _size):
        if self.packets:
            return self.packets.pop(0)
        raise TimeoutError

    def close(self):
        self.closed = True
