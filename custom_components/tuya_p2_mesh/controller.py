"""Controller for Tuya P2 mesh bulbs.

This module is pure Python: the radio is an injected asynchronous transport,
so no Home Assistant code is imported here. The controller owns the persisted
P2 sequence counter and serializes all radio access.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

try:  # Imported as part of the integration package.
    from . import p2
    from .radio import RawAdvertisement, parse_p2_advertisement
except ImportError:  # Imported directly by the unit tests.
    import p2
    from radio import RawAdvertisement, parse_p2_advertisement


DEFAULT_SOURCE_ID = 0x7D00
DEFAULT_ADVERTISING_SECONDS = 3.0

DP_COMMAND_FRAME_TYPE = 1
DP_WRITE_SUB_CMD = 5

_SEQUENCE_MAX = 0xFFFF
_CAPTURE_SCAN_FAILED = (
    "Bluetooth scan failed; check the adapter and permissions"
)
_CAPTURE_BAD_LOCAL_KEY = (
    "Local key is invalid; re-enter the key from the Tuya app."
)
_CAPTURE_NO_PAIR = (
    "No matching DP1 OFF/ON command pair was captured. Toggle the bulb "
    "from the Tuya app near the adapter and try again."
)


class _AsyncRadio(Protocol):
    """The subset of the radio transport used by the controller."""

    async def advertise(self, frame: bytes, seconds: float) -> None: ...

    async def scan(self, seconds: float) -> list[RawAdvertisement]: ...


@dataclass(frozen=True)
class BulbConfig:
    """Static configuration for one bulb."""

    mac: str
    local_key: bytes
    node_id: int
    name: str


@dataclass(frozen=True)
class P2Advertisement:
    """A well-formed P2 frame seen in an advertising report."""

    mac: str
    rssi: int
    frame: bytes
    src: int
    dst: int
    seq: int
    subcmd: int


class CaptureError(Exception):
    """Raised when a beacon key cannot be captured safely."""


class SequenceStore:
    """Persistent 16-bit P2 sequence counter stored as JSON.

    The file contains ``{"seq": <integer>}`` and is replaced atomically, so
    an interrupted write cannot corrupt the counter. The value is persisted
    before the controller transmits it.
    """

    def __init__(self, path: Path, initial: int) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.value = self._load(initial)

    def _load(self, initial: int) -> int:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return int(initial) & _SEQUENCE_MAX
        data = json.loads(raw)
        value = data["seq"] if isinstance(data, dict) else data
        return int(value) & _SEQUENCE_MAX

    def next(self) -> int:
        """Increment, save, and return the next non-zero sequence number."""
        with self._lock:
            value = (self.value + 1) & _SEQUENCE_MAX
            if value == 0:
                value = 1
            self._save(value)
            self.value = value
            return value

    def _save(self, value: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"seq": value}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


class P2Controller:
    """Send P2 commands and capture the beacon key from app commands."""

    def __init__(
        self,
        radio: _AsyncRadio,
        beacon_key: bytes,
        sequence: SequenceStore,
        source_id: int = DEFAULT_SOURCE_ID,
        advertising_seconds: float = DEFAULT_ADVERTISING_SECONDS,
    ) -> None:
        self._radio = radio
        self.beacon_key = p2.key_bytes(beacon_key)
        self.sequence = sequence
        self.source_id = source_id
        self.advertising_seconds = advertising_seconds
        self._radio_lock = asyncio.Lock()
        self._available = True

    @property
    def available(self) -> bool:
        """Whether the last radio operation succeeded."""
        return self._available

    async def send(
        self, bulb: BulbConfig, dps: list[tuple[int, str, object]]
    ) -> None:
        """Encode DPs, persist the next sequence, and advertise one frame."""
        payload = p2.encode_dps(dps)
        async with self._radio_lock:
            sequence = await asyncio.to_thread(self.sequence.next)
            frame = p2.build(
                DP_COMMAND_FRAME_TYPE,
                DP_WRITE_SUB_CMD,
                self.source_id,
                bulb.node_id,
                sequence,
                payload,
                bulb.local_key,
                self.beacon_key,
            )
            await self._advertise(frame)

    async def scan(self, seconds: float) -> list[P2Advertisement]:
        """Return well-formed P2 advertisements seen during a scan."""
        async with self._radio_lock:
            try:
                reports = await self._radio.scan(seconds)
            except Exception:
                self._available = False
                raise
            else:
                self._available = True

        advertisements = []
        for report in reports:
            frame = parse_p2_advertisement(report.data)
            if frame is None or len(frame) != p2.P2_FRAME_LEN:
                continue
            advertisements.append(
                P2Advertisement(
                    mac=report.address,
                    rssi=report.rssi,
                    frame=frame,
                    src=int.from_bytes(frame[1:3], "big"),
                    dst=int.from_bytes(frame[3:5], "big"),
                    seq=int.from_bytes(frame[5:7], "big"),
                    subcmd=frame[7],
                )
            )
        return advertisements

    async def capture_beacon_key(
        self,
        node_id: int,
        local_key: bytes,
        seconds: float = 60.0,
    ) -> bytes:
        """Recover the beacon key from matching app OFF and ON commands."""
        try:
            local_key = p2.key_bytes(local_key)
        except ValueError as err:
            raise CaptureError(_CAPTURE_BAD_LOCAL_KEY) from err

        try:
            advertisements = await self.scan(seconds)
        except CaptureError:
            raise
        except Exception as err:
            raise CaptureError(_CAPTURE_SCAN_FAILED) from err

        off_keys: set[bytes] = set()
        on_keys: set[bytes] = set()
        known_payloads = (
            (p2.DP_OFF_PAYLOAD, off_keys),
            (p2.DP_ON_PAYLOAD, on_keys),
        )
        for advertisement in advertisements:
            if advertisement.dst != node_id:
                continue
            if advertisement.src == node_id:
                continue
            if advertisement.subcmd != DP_WRITE_SUB_CMD:
                continue
            if (
                (advertisement.frame[0] >> 3) & 0x0F
            ) != DP_COMMAND_FRAME_TYPE:
                continue
            for known_payload, keys in known_payloads:
                try:
                    keys.add(
                        p2.derive_beacon_key(
                            advertisement.frame, local_key, known_payload
                        )
                    )
                except ValueError:
                    continue

        common = off_keys & on_keys
        if common:
            return common.pop()
        raise CaptureError(_CAPTURE_NO_PAIR)

    async def test(self, bulb: BulbConfig) -> None:
        """Send DP 1 ON followed by DP 1 OFF as a transmission test."""
        await self.send(bulb, [(1, "bool", True)])
        await self.send(bulb, [(1, "bool", False)])

    async def _advertise(self, frame: bytes) -> None:
        try:
            await self._radio.advertise(frame, self.advertising_seconds)
        except Exception:
            self._available = False
            raise
        else:
            self._available = True
