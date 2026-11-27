"""Raw-HCI Bluetooth transport for Tuya P2 mesh advertisements.

MIT License

Copyright (c) 2026 the Tuya P2 mesh integration authors

This module is adapted from the raw-HCI transport in beacon-cooked
(https://github.com/MadeinTaly/beacon-cooked), Copyright (c) 2026 MadeinTaly,
which is distributed under the MIT License.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The transport talks to Linux's raw HCI socket directly: it needs CAP_NET_RAW
(root or an equivalent capability) and an adapter dedicated to this
integration. Blocking operations are wrapped by :class:`AsyncP2Radio` so the
Home Assistant event loop is never blocked.
"""

from __future__ import annotations

import errno
import re
import secrets
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

try:  # Imported as part of the integration package.
    from . import p2
except ImportError:  # Imported directly by the unit tests.
    import p2


# HCI packet and event identifiers.
HCI_EVENT_PACKET = 0x04
HCI_COMMAND_COMPLETE_EVENT = 0x0E
HCI_COMMAND_STATUS_EVENT = 0x0F
HCI_LE_META_EVENT = 0x3E
HCI_LE_ADVERTISING_REPORT_EVENT = 0x02
HCI_LE_EXTENDED_ADVERTISING_REPORT_EVENT = 0x0D

HCI_OGF_LE_CONTROLLER = 0x2000
HCI_COMMAND_DISALLOWED = 0x0C

# LE Controller command opcodes (OCF, without the OGF bits).
LE_READ_LOCAL_SUPPORTED_FEATURES = 0x03
LE_SET_RANDOM_ADDRESS = 0x05
LE_SET_ADVERTISING_PARAMETERS = 0x06
LE_SET_ADVERTISING_DATA = 0x08
LE_SET_SCAN_RESPONSE_DATA = 0x09
LE_SET_ADVERTISING_ENABLE = 0x0A
LE_SET_SCAN_PARAMETERS = 0x0B
LE_SET_SCAN_ENABLE = 0x0C
LE_SET_ADVERTISING_SET_RANDOM_ADDRESS = 0x35
LE_SET_EXTENDED_ADVERTISING_PARAMETERS = 0x36
LE_SET_EXTENDED_ADVERTISING_DATA = 0x37
LE_SET_EXTENDED_ADVERTISING_ENABLE = 0x39
LE_READ_MAXIMUM_ADVERTISING_DATA_LENGTH = 0x3B
LE_SET_EXTENDED_SCAN_PARAMETERS = 0x41
LE_SET_EXTENDED_SCAN_ENABLE = 0x42

# P2 commands are broadcast and never need a connection. Use non-connectable
# PDUs so a scanning central cannot occupy the controller's advertising and
# connection resources (the RTL8761B otherwise raises HCI status 0x09 after a
# few commands).
_LEGACY_ADVERTISING_PARAMETERS = bytes.fromhex(
    "200020000301000000000000000700"
)
_LEGACY_EMPTY_SCAN_RESPONSE = b"\0" * 32
_EXTENDED_ADVERTISING_PARAMETERS = bytes.fromhex(
    "010000200000200000070100000000000000007f0100010000"
)
_EXTENDED_ADVERTISING_ENABLE = bytes.fromhex("010101000000")
_EXTENDED_SCAN_PARAMETERS = bytes.fromhex("0000010110001000")
_EXTENDED_SCAN_ENABLE = bytes.fromhex("010000000000")
_LEGACY_SCAN_PARAMETERS = bytes.fromhex("01100010000000")

_ADAPTER_LOCKS: dict[int, threading.RLock] = {}
_ADAPTER_LOCKS_GUARD = threading.Lock()


def _adapter_lock(index: int) -> threading.RLock:
    """Return the process-wide radio lock for one adapter index."""
    with _ADAPTER_LOCKS_GUARD:
        lock = _ADAPTER_LOCKS.get(index)
        if lock is None:
            lock = threading.RLock()
            _ADAPTER_LOCKS[index] = lock
        return lock


class RadioError(Exception):
    """Raised when the HCI adapter cannot be opened or a command fails."""


@dataclass(frozen=True)
class RawAdvertisement:
    """One advertising report: controller address, RSSI, and AD structures."""

    address: str
    rssi: int
    data: bytes


def find_adapters(sysfs_root: Path = Path("/sys/class/bluetooth")) -> list[str]:
    """Return the HCI adapter names present in ``sysfs_root``, in numeric order."""
    try:
        entries = list(Path(sysfs_root).iterdir())
    except OSError:
        return []
    names = [
        entry.name
        for entry in entries
        if entry.is_dir() and re.fullmatch(r"hci\d+", entry.name)
    ]
    return sorted(names, key=lambda name: int(name[3:]))


def parse_p2_advertisement(raw_ad: bytes) -> bytes | None:
    """Return the 26-byte P2 frame from an AD payload, or ``None``.

    The P2 frame is carried in a complete list of 16-bit service UUIDs
    (AD type ``0x03``) with a 27-byte value: the type byte plus 26 frame
    bytes. Other AD structures and malformed lengths are skipped safely.
    """
    offset = 0
    while offset < len(raw_ad):
        length = raw_ad[offset]
        if length == 0:
            break
        end = offset + 1 + length
        if end > len(raw_ad):
            return None
        ad_type = raw_ad[offset + 1]
        if ad_type == 0x03 and length == 0x1B:
            return raw_ad[offset + 2 : end]
        offset = end
    return None


def _format_address(raw: bytes) -> str:
    """Format a six-byte little-endian HCI address as an uppercase MAC."""
    return ":".join(f"{byte:02X}" for byte in reversed(raw))


def _iter_scan_reports(packet: bytes) -> Iterator[RawAdvertisement]:
    """Yield every well-formed report in an LE Advertising Report event."""
    if len(packet) < 5 or packet[:2] != bytes([HCI_EVENT_PACKET, HCI_LE_META_EVENT]):
        return
    subevent = packet[3]
    count = packet[4]
    if subevent not in (
        HCI_LE_ADVERTISING_REPORT_EVENT,
        HCI_LE_EXTENDED_ADVERTISING_REPORT_EVENT,
    ):
        return
    offset = 5
    for _ in range(count):
        if subevent == HCI_LE_ADVERTISING_REPORT_EVENT:
            if offset + 10 > len(packet):
                return
            data_length = packet[offset + 8]
            data_start = offset + 9
            data_end = data_start + data_length
            if data_end >= len(packet):  # RSSI byte must follow the data
                return
            yield RawAdvertisement(
                _format_address(packet[offset + 2 : offset + 8]),
                struct.unpack("b", packet[data_end : data_end + 1])[0],
                packet[data_start:data_end],
            )
            offset = data_end + 1
        else:
            if offset + 24 > len(packet):
                return
            data_length = packet[offset + 23]
            data_start = offset + 24
            data_end = data_start + data_length
            if data_end > len(packet):
                return
            yield RawAdvertisement(
                _format_address(packet[offset + 3 : offset + 9]),
                struct.unpack("b", packet[offset + 13 : offset + 14])[0],
                packet[data_start:data_end],
            )
            offset = data_end


def parse_hci_scan_report(packet: bytes) -> RawAdvertisement | None:
    """Parse the first report in an HCI LE Advertising Report event.

    Legacy (subevent ``0x02``) and extended (subevent ``0x0D``) reports are
    supported. Foreign packets, truncated reports, and malformed lengths
    return ``None`` instead of raising.
    """
    return next(_iter_scan_reports(packet), None)


class P2Radio:
    """Own one raw-HCI adapter for P2 advertising and scanning."""

    def __init__(
        self,
        adapter: str = "hci0",
        *,
        command_fn: Callable[..., bytes | None] | None = None,
    ) -> None:
        if not re.fullmatch(r"hci\d+", adapter):
            raise ValueError(f"invalid adapter name: {adapter!r}")
        self.adapter = adapter
        self.index = int(adapter[3:])
        self.extended: bool | None = None
        self._command_fn = command_fn
        self._lock = _adapter_lock(self.index)
        self._closed = False

    # -- low-level HCI -----------------------------------------------------

    def command(
        self, ocf: int, payload: bytes = b"", allow_disabled: bool = False
    ) -> bytes | None:
        """Send one LE Controller command and return its return parameters."""
        with self._lock:
            self._ensure_open()
            if self._command_fn is not None:
                try:
                    return self._command_fn(ocf, payload, allow_disabled)
                except RadioError:
                    raise
                except TimeoutError as err:
                    raise RadioError(
                        f"Bluetooth command {ocf:#04x} timed out on {self.adapter}"
                    ) from err
                except OSError as err:
                    raise self._access_error(err) from err
            return self._raw_command(ocf, payload, allow_disabled)

    def _raw_command(
        self, ocf: int, payload: bytes, allow_disabled: bool
    ) -> bytes:
        opcode = HCI_OGF_LE_CONTROLLER | ocf
        opcode_bytes = opcode.to_bytes(2, "little")
        try:
            with socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI
            ) as channel:
                channel.bind((self.index,))
                # HCI event packets; match Command Complete and Command Status.
                channel.setsockopt(
                    0,
                    2,
                    struct.pack(
                        "<IIIH2x",
                        0x10,
                        (1 << 14) | (1 << 15),
                        0,
                        opcode,
                    ),
                )
                channel.settimeout(5)
                channel.send(
                    b"\x01"
                    + opcode_bytes
                    + bytes([len(payload)])
                    + payload
                )
                deadline = time.monotonic() + 5
                while True:
                    channel.settimeout(max(0.01, deadline - time.monotonic()))
                    packet = channel.recv(512)
                    if (
                        len(packet) >= 7
                        and packet[:2]
                        == bytes([HCI_EVENT_PACKET, HCI_COMMAND_COMPLETE_EVENT])
                        and packet[4:6] == opcode_bytes
                    ):
                        status, reply = packet[6], packet[7:]
                        break
                    if (
                        len(packet) >= 7
                        and packet[:2]
                        == bytes([HCI_EVENT_PACKET, HCI_COMMAND_STATUS_EVENT])
                        and packet[5:7] == opcode_bytes
                        and packet[3]
                    ):
                        status, reply = packet[3], b""
                        break
                    if time.monotonic() >= deadline:
                        raise RadioError(
                            f"Bluetooth command {ocf:#04x} timed out on "
                            f"{self.adapter}"
                        )
        except RadioError:
            raise
        except TimeoutError as err:
            raise RadioError(
                f"Bluetooth command {ocf:#04x} timed out on {self.adapter}"
            ) from err
        except OSError as err:
            raise self._access_error(err) from err
        if status and not (allow_disabled and status == HCI_COMMAND_DISALLOWED):
            raise RadioError(
                f"Bluetooth command {ocf:#04x} failed on {self.adapter}: "
                f"status {status:#04x}"
            )
        return reply

    def _access_error(self, err: OSError) -> RadioError:
        if err.errno in (errno.EACCES, errno.EPERM):
            detail = (
                "permission denied; grant CAP_NET_RAW (run as root or use "
                "setcap)"
            )
        elif err.errno in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
            detail = "adapter not found"
        else:
            detail = err.strerror or str(err)
        return RadioError(
            f"Cannot access Bluetooth adapter {self.adapter}: {detail}"
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RadioError("Bluetooth radio is closed")

    # -- advertising -------------------------------------------------------

    def detect_mode(self) -> None:
        """Select legacy or extended advertising based on controller support."""
        with self._lock:
            if self.extended is not None:
                return
            self.command(LE_READ_LOCAL_SUPPORTED_FEATURES)
            try:
                supported = self.command(
                    LE_READ_MAXIMUM_ADVERTISING_DATA_LENGTH
                )
            except RadioError:
                self.extended = False
            else:
                self.extended = bool(supported and supported[0])

    def stop_advertising(self) -> None:
        """Disable advertising on this adapter (safe when already disabled)."""
        with self._lock:
            self.detect_mode()
            if self.extended:
                self.command(
                    LE_SET_EXTENDED_ADVERTISING_ENABLE,
                    b"\0\0",
                    allow_disabled=True,
                )
            else:
                self.command(
                    LE_SET_ADVERTISING_ENABLE, b"\0", allow_disabled=True
                )

    def _prepare_advertising(self) -> None:
        sender = bytes([0xC2]) + secrets.token_bytes(5)
        if self.extended:
            self.command(
                LE_SET_EXTENDED_ADVERTISING_PARAMETERS,
                _EXTENDED_ADVERTISING_PARAMETERS,
            )
            self.command(
                LE_SET_ADVERTISING_SET_RANDOM_ADDRESS,
                b"\x01" + sender[::-1],
            )
        else:
            self.command(LE_SET_RANDOM_ADDRESS, sender[::-1])
            self.command(
                LE_SET_ADVERTISING_PARAMETERS, _LEGACY_ADVERTISING_PARAMETERS
            )
            self.command(
                LE_SET_SCAN_RESPONSE_DATA, _LEGACY_EMPTY_SCAN_RESPONSE
            )

    def _start_advertising(self, advertisement: bytes) -> None:
        if self.extended:
            self.command(
                LE_SET_EXTENDED_ADVERTISING_DATA,
                bytes([1, 3, 1, len(advertisement)]) + advertisement,
            )
            self.command(
                LE_SET_EXTENDED_ADVERTISING_ENABLE,
                _EXTENDED_ADVERTISING_ENABLE,
            )
        else:
            self.command(
                LE_SET_ADVERTISING_DATA,
                bytes([len(advertisement)])
                + advertisement.ljust(31, b"\0"),
            )
            self.command(LE_SET_ADVERTISING_ENABLE, b"\x01")

    def advertise(self, frame: bytes, seconds: float = 3.0) -> None:
        """Advertise one 26-byte P2 frame continuously for ``seconds``.

        Advertising is enabled once and left running for the whole duration.
        Re-enabling an already-enabled advertising set is rejected by some
        controllers (for example the RTL8761B returns status 0x12), and it is
        unnecessary because the controller repeats the advertisement on its
        own interval.
        """
        advertisement = p2.advertisement(frame)
        with self._lock:
            self._ensure_open()
            self.detect_mode()
            self.stop_advertising()
            self._prepare_advertising()
            try:
                self._start_advertising(advertisement)
                time.sleep(seconds)
            finally:
                self.stop_advertising()

    # -- scanning ----------------------------------------------------------

    def _enable_scanning(self) -> None:
        if self.extended:
            self.command(
                LE_SET_EXTENDED_SCAN_ENABLE, bytes(6), allow_disabled=True
            )
            self.command(
                LE_SET_EXTENDED_SCAN_PARAMETERS, _EXTENDED_SCAN_PARAMETERS
            )
            self.command(
                LE_SET_EXTENDED_SCAN_ENABLE, _EXTENDED_SCAN_ENABLE
            )
        else:
            self.command(
                LE_SET_SCAN_ENABLE, b"\0\0", allow_disabled=True
            )
            self.command(LE_SET_SCAN_PARAMETERS, _LEGACY_SCAN_PARAMETERS)
            self.command(LE_SET_SCAN_ENABLE, b"\x01\0")

    def _disable_scanning(self) -> None:
        if self.extended:
            self.command(
                LE_SET_EXTENDED_SCAN_ENABLE, bytes(6), allow_disabled=True
            )
        else:
            self.command(LE_SET_SCAN_ENABLE, b"\0\0", allow_disabled=True)

    def scan(self, seconds: float) -> list[RawAdvertisement]:
        """Scan for P2 advertisements for ``seconds``.

        Only well-formed reports that contain a 26-byte P2 frame are
        returned; malformed reports and foreign advertisements are ignored.
        """
        with self._lock:
            self._ensure_open()
            self.detect_mode()
            try:
                channel = socket.socket(
                    socket.AF_BLUETOOTH,
                    socket.SOCK_RAW,
                    socket.BTPROTO_HCI,
                )
            except OSError as err:
                raise self._access_error(err) from err
            try:
                try:
                    channel.bind((self.index,))
                    # HCI event packets, LE Meta Event (0x3E) only.
                    channel.setsockopt(
                        0,
                        2,
                        struct.pack("<IIIH2x", 0x10, 0, 1 << 30, 0),
                    )
                except OSError as err:
                    raise self._access_error(err) from err
                self.stop_advertising()
                advertisements: list[RawAdvertisement] = []
                try:
                    self._enable_scanning()
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        channel.settimeout(
                            max(0.05, deadline - time.monotonic())
                        )
                        try:
                            packet = channel.recv(4096)
                        except (TimeoutError, socket.timeout):
                            break
                        except OSError as err:
                            raise self._access_error(err) from err
                        for report in _iter_scan_reports(packet):
                            if parse_p2_advertisement(report.data) is not None:
                                advertisements.append(report)
                finally:
                    self._disable_scanning()
                return advertisements
            finally:
                channel.close()

    def close(self) -> None:
        """Mark the radio closed. Later commands raise :class:`RadioError`."""
        with self._lock:
            self._closed = True


class AsyncP2Radio:
    """Run blocking :class:`P2Radio` calls in Home Assistant's executor."""

    def __init__(self, hass, radio: P2Radio) -> None:
        self.hass = hass
        self.radio = radio

    async def advertise(self, frame: bytes, seconds: float) -> None:
        """Advertise ``frame`` without blocking the event loop."""
        await self.hass.async_add_executor_job(
            self.radio.advertise, frame, seconds
        )

    async def scan(self, seconds: float) -> list[RawAdvertisement]:
        """Scan for advertisements without blocking the event loop."""
        return await self.hass.async_add_executor_job(
            self.radio.scan, seconds
        )

    async def close(self) -> None:
        """Close the underlying radio without blocking the event loop."""
        await self.hass.async_add_executor_job(self.radio.close)
