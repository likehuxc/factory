from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

FRAME_HEAD = 0xAA
FRAME_TAIL = 0x55
MAX_FRAME_SIZE = 512
BROADCAST_STATION = 0xFE
DEFAULT_STATION_AFTER_RESET = 0x00

CONTROLWORD_ENABLE = 0x0006
CONTROLWORD_BRAKE_ON = 0x0007
CONTROLWORD_BRAKE_RELEASE = 0x000F
CONTROLWORD_START_ABSOLUTE = 0x003F
CONTROLWORD_START_RELATIVE = 0x007F
CONTROLWORD_STOP_POSITION = 0x010F


class Aa55ProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Aa55Frame:
    station: int
    function: int
    data: bytes
    raw: bytes


def crc16_modbus_a001(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_aa55_frame(*, station: int, function: int, data: bytes) -> bytes:
    if not 0 <= station <= 0xFF:
        raise ValueError("station must be in 0..255")
    if not 0 <= function <= 0xFF:
        raise ValueError("function must be in 0..255")
    message_length = 6 + len(data)
    if message_length + 2 > MAX_FRAME_SIZE:
        raise ValueError("AA55 frame exceeds maximum size")
    body = message_length.to_bytes(2, "little") + bytes((station, function)) + bytes(data)
    crc = crc16_modbus_a001(body)
    return bytes((FRAME_HEAD,)) + body + crc.to_bytes(2, "big") + bytes((FRAME_TAIL,))


def decode_aa55_frame(raw: bytes) -> Aa55Frame:
    frame = bytes(raw)
    if len(frame) < 8:
        raise Aa55ProtocolError("AA55 frame is too short")
    if frame[0] != FRAME_HEAD:
        raise Aa55ProtocolError("AA55 frame head mismatch")
    if frame[-1] != FRAME_TAIL:
        raise Aa55ProtocolError("AA55 frame tail mismatch")
    declared_length = int.from_bytes(frame[1:3], "little")
    if declared_length + 2 != len(frame):
        raise Aa55ProtocolError(f"AA55 length mismatch: declared {declared_length}, actual {len(frame) - 2}")
    expected_crc = crc16_modbus_a001(frame[1:-3])
    received_crc = int.from_bytes(frame[-3:-1], "big")
    if received_crc != expected_crc:
        raise Aa55ProtocolError(f"AA55 CRC mismatch: got 0x{received_crc:04X}, expected 0x{expected_crc:04X}")
    return Aa55Frame(station=frame[3], function=frame[4], data=frame[5:-3], raw=frame)


class Aa55StreamDecoder:
    """Incrementally extracts verified AA55 frames from noisy serial data."""

    def __init__(self, maximum_frame_size: int = MAX_FRAME_SIZE) -> None:
        if maximum_frame_size < 8:
            raise ValueError("maximum frame size must be at least 8")
        self.maximum_frame_size = maximum_frame_size
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> list[Aa55Frame]:
        self._buffer.extend(data)
        frames: list[Aa55Frame] = []
        while self._buffer:
            head = self._buffer.find(FRAME_HEAD)
            if head < 0:
                self._buffer.clear()
                break
            if head:
                del self._buffer[:head]
            if len(self._buffer) < 3:
                break
            total_length = int.from_bytes(self._buffer[1:3], "little") + 2
            if total_length < 8 or total_length > self.maximum_frame_size:
                del self._buffer[0]
                continue
            if len(self._buffer) < total_length:
                break
            candidate = bytes(self._buffer[:total_length])
            try:
                frame = decode_aa55_frame(candidate)
            except Aa55ProtocolError:
                del self._buffer[0]
                continue
            del self._buffer[:total_length]
            frames.append(frame)
        return frames


def encode_0e_item(*, address: int, ram_flag: int, value: bytes) -> bytes:
    if not 0 <= address <= 0xFFFFFF:
        raise ValueError("object address must fit in 24 bits")
    if not 0 <= ram_flag <= 0xFF:
        raise ValueError("RAM flag must fit in one byte")
    head = address.to_bytes(3, "big") + bytes((ram_flag,))
    return head[::-1] + bytes(value)[::-1]


def build_0e_write_multi(*, station: int, items: Iterable[bytes]) -> bytes:
    parts = tuple(bytes(item) for item in items)
    if not 1 <= len(parts) <= 0xFF:
        raise ValueError("item count must be in 1..255")
    payload = len(parts).to_bytes(2, "little") + b"".join(parts)
    return build_aa55_frame(station=station, function=0x0E, data=payload)


def build_echo(*, station: int, pattern: bytes = b"\x99\x99") -> bytes:
    if len(pattern) != 2:
        raise ValueError("echo pattern must be exactly 2 bytes")
    return build_aa55_frame(station=station, function=0x08, data=pattern)


def comm_id_register_value(comm_id: int) -> int:
    if not 0 <= comm_id <= 0xFF:
        raise ValueError("communication ID must be in 0..255")
    return 0x0100 | comm_id


def station_from_comm_id(comm_id: int) -> int:
    if not 0 <= comm_id <= 0xFF:
        raise ValueError("communication ID must be in 0..255")
    return 0 if comm_id == 0 else comm_id - 1


def comm_id_from_station(station: int) -> int:
    if not 0 <= station <= 0xFF:
        raise ValueError("station must be in 0..255")
    return (station + 1) & 0xFF


def _u16_item(address: int, value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError("UINT16 value out of range")
    return encode_0e_item(address=address, ram_flag=0x10, value=value.to_bytes(2, "big"))


def comm_id_write_item(comm_id: int) -> bytes:
    return _u16_item(0x200711, comm_id_register_value(comm_id))


def comm_id_broadcast_reset_item() -> bytes:
    return comm_id_write_item(1)


def eeprom_save_item() -> bytes:
    return _u16_item(0x200706, 1)


def system_reset_item() -> bytes:
    return _u16_item(0x200701, 1)


def control_authority_item(value: int = 0) -> bytes:
    return _u16_item(0x200201, value)


def controlword_item(value: int) -> bytes:
    return _u16_item(0x604000, value)


def speed_mode_items(target_velocity: int) -> list[bytes]:
    if not 0 <= target_velocity <= 0xFFFFFFFF:
        raise ValueError("target velocity must fit in UINT32")
    return [
        encode_0e_item(address=0x60FF00, ram_flag=0x20, value=target_velocity.to_bytes(4, "big")),
        encode_0e_item(address=0x608300, ram_flag=0x20, value=(0x00155555).to_bytes(4, "big")),
        encode_0e_item(address=0x608400, ram_flag=0x20, value=(0x00155555).to_bytes(4, "big")),
        encode_0e_item(address=0x606000, ram_flag=0x08, value=b"\x03"),
        encode_0e_item(address=0x608600, ram_flag=0x10, value=b"\x00\x00"),
    ]


def relative_position_mode_items(
    *,
    delta_counts: int,
    profile_velocity_counts_s: int,
    acceleration_counts_s2: int,
    deceleration_counts_s2: int,
) -> list[bytes]:
    if not -(1 << 31) <= delta_counts < (1 << 31):
        raise ValueError("delta_counts must fit in INT32")
    values = (profile_velocity_counts_s, acceleration_counts_s2, deceleration_counts_s2)
    if any(not 1 <= value <= 0xFFFFFFFF for value in values):
        raise ValueError("velocity, acceleration and deceleration must be in 1..0xFFFFFFFF")
    return [
        encode_0e_item(
            address=0x607A00,
            ram_flag=0x20,
            value=(delta_counts & 0xFFFFFFFF).to_bytes(4, "big"),
        ),
        encode_0e_item(
            address=0x608100,
            ram_flag=0x20,
            value=profile_velocity_counts_s.to_bytes(4, "big"),
        ),
        encode_0e_item(address=0x608200, ram_flag=0x20, value=b"\0\0\0\0"),
        encode_0e_item(
            address=0x608300,
            ram_flag=0x20,
            value=acceleration_counts_s2.to_bytes(4, "big"),
        ),
        encode_0e_item(
            address=0x608400,
            ram_flag=0x20,
            value=deceleration_counts_s2.to_bytes(4, "big"),
        ),
        encode_0e_item(address=0x606000, ram_flag=0x08, value=b"\x01"),
        encode_0e_item(address=0x608600, ram_flag=0x10, value=b"\0\0"),
    ]
