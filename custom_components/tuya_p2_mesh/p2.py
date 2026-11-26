"""Tuya Beacon v2 (P2) framing, crypto, DP payloads, and advertisements.

MIT License

Copyright (c) 2026 the Tuya P2 mesh integration authors

This module is based on the verified bridge/reference implementation and on
the protocol notes and reference implementation in bbangert/universal_proxy
(https://github.com/bbangert/universal_proxy), which is licensed under the MIT
License.

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

Frame layout (26 bytes):

    0      header: bit7 pair flag, bits6-3 frame type, bits1-0 hop TTL
    1..2   source address (big endian)
    3..4   destination address (big endian)
    5..6   sequence number (big endian)
    7      sub-command
    8      CRC-8 of the 16-byte plaintext payload
    9..24  XXTEA(payload) XOR mask
    25     CRC-8 of [hdr & 0xFC] + bytes 1..24, computed before masking
"""

from __future__ import annotations

import struct

DELTA = 0x9E3779B9
MASK32 = 0xFFFFFFFF

P2_FRAME_LEN = 26

# Known plaintext DP payloads used to recover the beacon (XOR) key.
DP_ON_PAYLOAD = bytes.fromhex("011101")
DP_OFF_PAYLOAD = bytes.fromhex("011100")


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def crc8(data: bytes) -> int:
    """CRC-8/SMBUS: poly 0x07, init 0, no reflection."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _words(data: bytes) -> list[int]:
    pad = (-len(data)) % 4
    data += b"\0" * pad
    return list(struct.unpack(f">{len(data) // 4}I", data))


def _unwords(words: list[int]) -> bytes:
    return struct.pack(f">{len(words)}I", *[w & MASK32 for w in words])


def _mx(z: int, y: int, s: int, p: int, e: int, k: list[int]) -> int:
    a = ((z >> 5) ^ ((y << 2) & MASK32)) & MASK32
    b = ((y >> 3) ^ ((z << 4) & MASK32)) & MASK32
    c = (s ^ y) & MASK32
    d = (k[(p & 3) ^ e] ^ z) & MASK32
    return ((a + b) & MASK32) ^ ((c + d) & MASK32)


def xxtea_encrypt(data: bytes, key: bytes) -> bytes:
    """XXTEA with big-endian word packing (Tuya's variant)."""
    v = _words(data)
    k = _words(key)
    n = len(v)
    rounds = 6 + 52 // n
    s = 0
    z = v[-1]
    for _ in range(rounds):
        s = (s + DELTA) & MASK32
        e = (s >> 2) & 3
        for p in range(n):
            y = v[(p + 1) % n]
            v[p] = (v[p] + _mx(z, y, s, p, e, k)) & MASK32
            z = v[p]
    return _unwords(v)


def xxtea_decrypt(data: bytes, key: bytes) -> bytes:
    """Inverse of :func:`xxtea_encrypt`."""
    v = _words(data)
    k = _words(key)
    n = len(v)
    rounds = 6 + 52 // n
    s = (rounds * DELTA) & MASK32
    y = v[0]
    for _ in range(rounds):
        e = (s >> 2) & 3
        for p in range(n - 1, -1, -1):
            z = v[(p - 1) % n]
            v[p] = (v[p] - _mx(z, y, s, p, e, k)) & MASK32
            y = v[p]
        s = (s - DELTA) & MASK32
    return _unwords(v)


def key_bytes(key: str | bytes) -> bytes:
    """Tuya key rule: 32 hex chars decode, 16 chars are ASCII, 16 bytes pass."""
    if isinstance(key, bytes):
        if len(key) == 16:
            return key
        raise ValueError("raw key must be 16 bytes")
    if len(key) == 32:
        return bytes.fromhex(key)
    if len(key) == 16:
        return key.encode("ascii")
    raise ValueError("key must be 16 ASCII chars or 32 hex chars")


# ---------------------------------------------------------------------------
# P2 frames
# ---------------------------------------------------------------------------


def build(
    frame_type: int,
    sub_cmd: int,
    src: int,
    dst: int,
    sn: int,
    payload: bytes,
    key: bytes,
    mask: bytes,
    hop: int = 3,
) -> bytes:
    """Build a normal (non-pair) P2 frame."""
    if not 0 <= frame_type <= 15:
        raise ValueError("frame type must be 0..15")
    if len(payload) > 16:
        raise ValueError("payload must be <= 16 bytes")
    key = key_bytes(key)
    mask = key_bytes(mask)
    padded = payload + b"\0" * (16 - len(payload))
    cipher = xxtea_encrypt(padded, key)
    hdr = (frame_type << 3) | (hop & 3)
    head = src.to_bytes(2, "big") + dst.to_bytes(2, "big") + sn.to_bytes(2, "big")
    inner = head + bytes([sub_cmd, crc8(padded)]) + cipher
    seal = crc8(bytes([hdr & 0xFC]) + inner)
    masked = bytes(a ^ b for a, b in zip(cipher, mask))
    return bytes([hdr]) + head + bytes([sub_cmd, crc8(padded)]) + masked + bytes([seal])


def advertisement(frame: bytes) -> bytes:
    """Build the complete 31-byte legacy advertising payload, flags first."""
    if len(frame) != P2_FRAME_LEN:
        raise ValueError("P2 frame must be 26 bytes")
    return b"\x02\x01\x06" + bytes([len(frame) + 1, 0x03]) + frame


def p2_outer_crc_ok(frame: bytes, beacon_key: bytes) -> bool:
    """Return True when the outer CRC matches after removing the XOR mask."""
    if len(frame) != P2_FRAME_LEN:
        return False
    mask = key_bytes(beacon_key)
    cipher = bytes(a ^ b for a, b in zip(frame[9:25], mask))
    inner = frame[1:8] + bytes([frame[8]]) + cipher
    return crc8(bytes([frame[0] & 0xFC]) + inner) == frame[25]


def parse_frame(
    frame: bytes, encryption_key: bytes, beacon_key: bytes
) -> dict | None:
    """Parse a P2 frame using an XXTEA key and a separate beacon XOR key."""
    if len(frame) != P2_FRAME_LEN:
        return None
    try:
        key = key_bytes(encryption_key)
        mask = key_bytes(beacon_key)
    except ValueError:
        return None
    if not p2_outer_crc_ok(frame, mask):
        return None
    cipher = bytes(a ^ b for a, b in zip(frame[9:25], mask))
    payload = xxtea_decrypt(cipher, key)
    if crc8(payload) != frame[8]:
        return None
    return {
        "pair": bool(frame[0] & 0x80),
        "type": (frame[0] >> 3) & 0xF,
        "src": int.from_bytes(frame[1:3], "big"),
        "dst": int.from_bytes(frame[3:5], "big"),
        "sn": int.from_bytes(frame[5:7], "big"),
        "sub_cmd": frame[7],
        "payload": payload,
    }


def parse_device(frame: bytes, beacon_key: bytes) -> dict | None:
    """Parse a device-to-phone frame using the beacon key."""
    parsed = parse_frame(frame, beacon_key, beacon_key)
    if parsed is None or parsed["dst"] not in (0x8000, 0x8001):
        return None
    return parsed


def derive_beacon_key(frame: bytes, local_key: bytes, known_payload: bytes) -> bytes:
    """Recover the beacon XOR key from a captured frame with known plaintext."""
    if len(frame) != P2_FRAME_LEN:
        raise ValueError("P2 frame must be 26 bytes")
    if ((frame[0] >> 3) & 0x0F) != 1 or frame[7] != 0x05:
        raise ValueError("not a P2 DP command")
    plaintext = known_payload.ljust(16, b"\0")
    if crc8(plaintext) != frame[8]:
        raise ValueError("known payload does not match frame CRC")
    encrypted = xxtea_encrypt(plaintext, key_bytes(local_key))
    beacon = bytes(a ^ b for a, b in zip(frame[9:25], encrypted))
    if not p2_outer_crc_ok(frame, beacon):
        raise ValueError("invalid P2 outer CRC")
    return beacon


# ---------------------------------------------------------------------------
# DP TLVs
# ---------------------------------------------------------------------------

DP_TYPES = {"raw": 0, "bool": 1, "value": 2, "string": 3, "enum": 4, "bitmap": 5}


def encode_dps(dps: list[tuple[int, str, object]]) -> bytes:
    """Encode ``[(dp_id, type_name, value), ...]`` to at most 16 bytes."""
    out = bytearray()
    for dp_id, type_name, value in dps:
        if type_name == "bool":
            raw = b"\x01" if value else b"\x00"
        elif type_name == "value":
            raw = int(value).to_bytes(4, "big", signed=True)
        elif type_name == "enum":
            raw = bytes([int(value) & 0xFF])
        elif type_name in ("string", "raw"):
            raw = value.encode() if isinstance(value, str) else bytes(value)
        elif type_name == "bitmap":
            raw = int(value).to_bytes((int(value).bit_length() + 7) // 8 or 1, "big")
        else:
            raise ValueError(f"bad DP type {type_name}")
        out += bytes([dp_id, (DP_TYPES[type_name] << 4) | len(raw)]) + raw
    if len(out) > 16:
        raise ValueError("encoded DPs exceed 16 bytes")
    return bytes(out)


def decode_dps(payload: bytes) -> list[tuple[int, int, bytes]]:
    """Decode TLVs until a zero DP id or the end of the payload."""
    out = []
    i = 0
    while i + 2 <= len(payload):
        dp_id, kind = payload[i], payload[i + 1]
        if dp_id == 0:
            break
        typ, length = kind >> 4, kind & 0x0F
        if i + 2 + length > len(payload):
            break
        out.append((dp_id, typ, payload[i + 2 : i + 2 + length]))
        i += 2 + length
    return out
