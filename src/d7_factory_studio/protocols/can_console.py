from __future__ import annotations

from d7_factory_studio.core.models import CanFrame


def xor_checksum(data: bytes) -> int:
    result = 0
    for value in data:
        result ^= value
    return result


def parse_standard_can_id(value: str | int) -> int:
    can_id = int(value, 0) if isinstance(value, str) else int(value)
    if not 0 <= can_id <= 0x7FF:
        raise ValueError("CAN ID must be a standard 11-bit ID")
    return can_id


def parse_hex_payload(value: str, *, maximum: int = 64) -> bytes:
    compact = "".join(char for char in value if char not in " \t\r\n-_:,")
    if len(compact) % 2:
        raise ValueError("hex payload must contain complete bytes")
    try:
        payload = bytes.fromhex(compact)
    except ValueError as exc:
        raise ValueError("invalid hex payload") from exc
    if len(payload) > maximum:
        raise ValueError(f"payload cannot exceed {maximum} bytes")
    return payload


def build_battery_frame(can_id: int, subcommand: int, value: int, pack_index: int = 0) -> CanFrame:
    if not 0x41 <= can_id <= 0x4F:
        raise ValueError("battery CAN ID must be in 0x41..0x4F")
    body = bytes(
        (
            0x82,
            subcommand & 0xFF,
            (value >> 24) & 0xFF,
            (value >> 16) & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
            ((pack_index & 0x0F) << 4) | 0x01,
        )
    )
    return CanFrame(can_id, body + bytes((xor_checksum(body),)), is_fd=False, bitrate_switch=False)


def build_light_frame(
    can_id: int,
    strip_id: int,
    color_id: int,
    mode: int,
    count: int,
    cycle_ms: int,
    rgb: tuple[int, int, int],
) -> CanFrame:
    parse_standard_can_id(can_id)
    if not 200 <= strip_id <= 203:
        raise ValueError("light strip ID must be in 200..203")
    if not 0 <= mode < 12:
        raise ValueError("invalid light mode")
    params = rgb if mode == 5 else (count & 0xFF, (cycle_ms >> 8) & 0xFF, cycle_ms & 0xFF)
    if any(not 0 <= value <= 0xFF for value in params):
        raise ValueError("RGB values must be in 0..255")
    body = bytes((0x90, strip_id, color_id & 0xFF, mode, *params))
    return CanFrame(can_id, body + bytes((xor_checksum(body),)), is_fd=False, bitrate_switch=False)
