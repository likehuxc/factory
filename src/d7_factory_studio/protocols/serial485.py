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

PARAMETER_FLAG_INT8 = 0x08
PARAMETER_FLAG_INT16 = 0x10
PARAMETER_FLAG_INT32 = 0x20
PARAMETER_FLAG_STRING = 0x00
PARAMETER_FLAG_ERROR = 0xFF
PARAMETER_WIDTHS = {
    PARAMETER_FLAG_INT8: 1,
    PARAMETER_FLAG_INT16: 2,
    PARAMETER_FLAG_INT32: 4,
}

COMM_ID_ADDRESS = 0x200711
DEVICE_ID_ADDRESS = 0x200129
CONTROL_AUTHORITY_ADDRESS = 0x200201
ACTUAL_POSITION_ADDRESS = 0x200804
CIA402_POSITION_ACTUAL_ADDRESS = 0x606400
SERVO_STATUS_ADDRESS = 0x604100
MOTOR_IDENTIFICATION_CONTROL_ADDRESS = 0x200705
MOTOR_IDENTIFICATION_STATE_ADDRESS = 0x20032C
MOTOR_PHASE_SEQUENCE_ADDRESS = 0x20011E
ABS_ENCODER_OFFSET_ADDRESS = 0x200015
DEVICE_NAME_ADDRESS = 0x100800
HARDWARE_VERSION_ADDRESS = 0x100900
SOFTWARE_VERSION_ADDRESS = 0x100A00
# JiHua's 485 motion objects use the same 24-bit revolution scale as the
# working quick-config tool (5 rpm == 0x00155555 counts/s).
D7_ENCODER_BITS = 24
D7_COUNTS_PER_REVOLUTION = 1 << D7_ENCODER_BITS


class Aa55ProtocolError(ValueError):
    pass


class Aa55ParameterReadError(Aa55ProtocolError):
    def __init__(self, message: str, items: tuple[ParameterReadItem, ...] = ()) -> None:
        super().__init__(message)
        self.items = items


@dataclass(frozen=True, slots=True)
class Aa55Frame:
    station: int
    function: int
    data: bytes
    raw: bytes


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    address: int
    length_flag: int
    signed: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.address <= 0xFFFFFF:
            raise ValueError("parameter address must fit in 24 bits")
        if self.length_flag not in PARAMETER_WIDTHS and self.length_flag != PARAMETER_FLAG_STRING:
            raise ValueError("parameter length flag must be 0x00, 0x08, 0x10 or 0x20")


@dataclass(frozen=True, slots=True)
class ParameterReadItem:
    address: int
    length_flag: int
    raw_value: bytes
    value: int | str | None
    error_code: int | None = None


COMM_ID_PARAMETER = ParameterSpec(COMM_ID_ADDRESS, PARAMETER_FLAG_INT16)
DEVICE_ID_PARAMETER = ParameterSpec(DEVICE_ID_ADDRESS, PARAMETER_FLAG_INT16)
CONTROL_AUTHORITY_PARAMETER = ParameterSpec(CONTROL_AUTHORITY_ADDRESS, PARAMETER_FLAG_INT16)
ACTUAL_POSITION_PARAMETER = ParameterSpec(
    ACTUAL_POSITION_ADDRESS,
    PARAMETER_FLAG_INT32,
    signed=True,
)
CIA402_POSITION_ACTUAL_PARAMETER = ParameterSpec(
    CIA402_POSITION_ACTUAL_ADDRESS,
    PARAMETER_FLAG_INT32,
    signed=True,
)
SERVO_STATUS_PARAMETER = ParameterSpec(SERVO_STATUS_ADDRESS, PARAMETER_FLAG_INT16)
MOTOR_IDENTIFICATION_STATE_PARAMETER = ParameterSpec(
    MOTOR_IDENTIFICATION_STATE_ADDRESS,
    PARAMETER_FLAG_INT16,
)
MOTOR_PHASE_SEQUENCE_PARAMETER = ParameterSpec(MOTOR_PHASE_SEQUENCE_ADDRESS, PARAMETER_FLAG_INT16)
ABS_ENCODER_OFFSET_PARAMETER = ParameterSpec(ABS_ENCODER_OFFSET_ADDRESS, PARAMETER_FLAG_INT32)
DEVICE_NAME_PARAMETER = ParameterSpec(DEVICE_NAME_ADDRESS, PARAMETER_FLAG_STRING)
HARDWARE_VERSION_PARAMETER = ParameterSpec(HARDWARE_VERSION_ADDRESS, PARAMETER_FLAG_STRING)
SOFTWARE_VERSION_PARAMETER = ParameterSpec(SOFTWARE_VERSION_ADDRESS, PARAMETER_FLAG_STRING)


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


def encode_0d_item(spec: ParameterSpec) -> bytes:
    head = spec.address.to_bytes(3, "big") + bytes((spec.length_flag,))
    return head[::-1]


def build_0d_read_multi(*, station: int, parameters: Iterable[ParameterSpec]) -> bytes:
    specs = tuple(parameters)
    if not 1 <= len(specs) <= 0xFF:
        raise ValueError("parameter count must be in 1..255")
    payload = len(specs).to_bytes(2, "little") + b"".join(encode_0d_item(spec) for spec in specs)
    return build_aa55_frame(station=station, function=0x0D, data=payload)


def parse_0d_response(
    frame: Aa55Frame,
    *,
    expected: Iterable[ParameterSpec] | None = None,
) -> tuple[ParameterReadItem, ...]:
    if frame.function not in (0x0D, 0x8D):
        raise Aa55ProtocolError(f"expected parameter-read response, got function 0x{frame.function:02X}")
    if len(frame.data) < 2:
        raise Aa55ProtocolError("parameter-read response is missing item count")

    count = int.from_bytes(frame.data[:2], "little")
    if not 1 <= count <= 0xFF:
        raise Aa55ProtocolError(f"invalid parameter-read item count: {count}")

    expected_specs = tuple(expected) if expected is not None else None
    expected_by_address: dict[int, ParameterSpec] = {}
    if expected_specs is not None:
        if count != len(expected_specs):
            raise Aa55ProtocolError(
                f"parameter-read item count mismatch: response {count}, expected {len(expected_specs)}"
            )
        expected_by_address = {spec.address: spec for spec in expected_specs}
        if len(expected_by_address) != len(expected_specs):
            raise ValueError("expected parameter addresses must be unique")

    items: list[ParameterReadItem] = []
    seen: set[int] = set()
    offset = 2
    for index in range(count):
        if offset + 4 > len(frame.data):
            raise Aa55ProtocolError(f"parameter-read item {index} header is truncated")
        head = frame.data[offset : offset + 4][::-1]
        offset += 4
        address = int.from_bytes(head[:3], "big")
        length_flag = head[3]
        if address in seen:
            raise Aa55ProtocolError(f"duplicate parameter-read address 0x{address:06X}")
        seen.add(address)

        spec = expected_by_address.get(address) if expected_specs is not None else None
        if expected_specs is not None and spec is None:
            raise Aa55ProtocolError(f"unexpected parameter-read address 0x{address:06X}")
        if length_flag == PARAMETER_FLAG_ERROR:
            width = 1
        elif spec is not None and spec.length_flag == PARAMETER_FLAG_STRING:
            # JiHua string requests use flag 0x00. In the response this byte
            # carries the dynamic string byte length instead.
            width = length_flag
        else:
            try:
                width = PARAMETER_WIDTHS[length_flag]
            except KeyError as exc:
                raise Aa55ProtocolError(
                    f"unsupported parameter-read length flag 0x{length_flag:02X}"
                ) from exc
            if spec is not None and length_flag != spec.length_flag:
                raise Aa55ProtocolError(
                    f"parameter 0x{address:06X} length flag mismatch: "
                    f"0x{length_flag:02X}, expected 0x{spec.length_flag:02X}"
                )
        if offset + width > len(frame.data):
            raise Aa55ProtocolError(f"parameter-read item 0x{address:06X} value is truncated")
        raw_value = frame.data[offset : offset + width]
        offset += width
        if length_flag == PARAMETER_FLAG_ERROR:
            items.append(ParameterReadItem(address, length_flag, raw_value, None, raw_value[0]))
        elif spec is not None and spec.length_flag == PARAMETER_FLAG_STRING:
            try:
                text = raw_value.decode("utf-8")
            except UnicodeDecodeError:
                text = raw_value.decode("gbk", errors="replace")
            items.append(ParameterReadItem(address, length_flag, raw_value, text.rstrip("\x00")))
        else:
            value = int.from_bytes(raw_value, "little", signed=bool(spec and spec.signed))
            items.append(ParameterReadItem(address, length_flag, raw_value, value))

    if offset != len(frame.data):
        raise Aa55ProtocolError(f"parameter-read response has {len(frame.data) - offset} trailing bytes")
    if expected_specs is not None and seen != set(expected_by_address):
        missing = sorted(set(expected_by_address) - seen)
        raise Aa55ProtocolError(
            "parameter-read response is missing " + ", ".join(f"0x{address:06X}" for address in missing)
        )

    parsed = tuple(items)
    errors = tuple(item for item in parsed if item.error_code is not None)
    if frame.function == 0x8D or errors:
        details = ", ".join(
            f"0x{item.address:06X}=0x{item.error_code:02X}" for item in errors
        ) or "function 0x8D"
        raise Aa55ParameterReadError(f"parameter-read failed: {details}", parsed)
    return parsed


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


def motor_identification_control_item(mode: int) -> bytes:
    if mode not in {0, 2, 4}:
        raise ValueError("motor identification mode must be 0, 2 or 4")
    return _u16_item(MOTOR_IDENTIFICATION_CONTROL_ADDRESS, mode)


def control_authority_item(value: int = 0) -> bytes:
    return _u16_item(CONTROL_AUTHORITY_ADDRESS, value)


def controlword_item(value: int) -> bytes:
    return _u16_item(0x604000, value)


def speed_mode_items(
    target_velocity: int,
    *,
    acceleration_counts_s2: int = 0x00155555,
    deceleration_counts_s2: int = 0x00155555,
) -> list[bytes]:
    if not -(1 << 31) <= target_velocity < (1 << 31):
        raise ValueError("target velocity must fit in INT32")
    if not 1 <= acceleration_counts_s2 <= 0xFFFFFFFF:
        raise ValueError("acceleration must be in 1..0xFFFFFFFF counts/s^2")
    if not 1 <= deceleration_counts_s2 <= 0xFFFFFFFF:
        raise ValueError("deceleration must be in 1..0xFFFFFFFF counts/s^2")
    return [
        encode_0e_item(
            address=0x60FF00,
            ram_flag=0x20,
            value=(target_velocity & 0xFFFFFFFF).to_bytes(4, "big"),
        ),
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


def absolute_position_mode_items(
    *,
    target_counts: int,
    profile_velocity_counts_s: int,
    acceleration_counts_s2: int,
    deceleration_counts_s2: int,
) -> list[bytes]:
    return relative_position_mode_items(
        delta_counts=target_counts,
        profile_velocity_counts_s=profile_velocity_counts_s,
        acceleration_counts_s2=acceleration_counts_s2,
        deceleration_counts_s2=deceleration_counts_s2,
    )
