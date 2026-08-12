from __future__ import annotations

import math
from dataclasses import dataclass

from d7_factory_studio.core.models import CanFrame

POSITION_MIN_RAD = -5.0
POSITION_MAX_RAD = 5.0
POSITION_RAW_MAX = 0xFFFF
VELOCITY_MIN_RAD_S = -14.0
VELOCITY_MAX_RAD_S = 14.0
VELOCITY_RAW_MAX = 0xFFFF
MOTOR_COMMAND_CAN_ID = 0x300
MOTOR_RESPONSE_BASE_ID = 0x100
VELOCITY_COMMAND_CAN_ID = MOTOR_COMMAND_CAN_ID
VELOCITY_COMMANDS_PER_FRAME = 7
MOTOR_STATUS_RUNNING_POSITION = 0x42
HUMAN_STATE_RUNNING = 2
READ_MOTOR_STATE_DATA = bytes.fromhex("40 40 40 08 04 01")
READ_HUMAN_STATE_DATA = bytes.fromhex("40 40 00 09 1C 01 00 01 10 42 10 20")
SET_CONTROL_SOURCE_DATA = bytes.fromhex("40 40 00 19 24 01 00 01 10 01 02 20 01 00 00 00")
ENABLE_DATA = bytes.fromhex("40 40 00 19 24 01 00 01 10 01 10 20 01 00 00 00")
DISABLE_DATA = bytes.fromhex("40 40 00 19 24 01 00 01 10 01 10 20 00 00 00 00")
FAULT_RESET_DATA = bytes.fromhex("40 40 00 19 24 01 00 01 10 03 07 20 01 00 00 00")


def position_raw_to_rad(raw: int) -> float:
    if not 0 <= raw <= POSITION_RAW_MAX:
        raise ValueError("position raw value must be in 0..65535")
    return POSITION_MIN_RAD + raw * (POSITION_MAX_RAD - POSITION_MIN_RAD) / POSITION_RAW_MAX


def position_rad_to_raw(position_rad: float) -> int:
    if not math.isfinite(position_rad) or not POSITION_MIN_RAD <= position_rad <= POSITION_MAX_RAD:
        raise ValueError("position must be finite and in -5..5 rad")
    scaled = (position_rad - POSITION_MIN_RAD) * POSITION_RAW_MAX / (POSITION_MAX_RAD - POSITION_MIN_RAD)
    return max(0, min(POSITION_RAW_MAX, round(scaled)))


def velocity_rad_s_to_raw(velocity_rad_s: float) -> int:
    if (
        not math.isfinite(velocity_rad_s)
        or not VELOCITY_MIN_RAD_S <= velocity_rad_s <= VELOCITY_MAX_RAD_S
    ):
        raise ValueError("velocity must be finite and in -14..14 rad/s")
    normalized = (velocity_rad_s - VELOCITY_MIN_RAD_S) / (
        VELOCITY_MAX_RAD_S - VELOCITY_MIN_RAD_S
    )
    # std::lround rounds this non-negative scaled value away from zero.
    return max(0, min(VELOCITY_RAW_MAX, math.floor(normalized * VELOCITY_RAW_MAX + 0.5)))


def velocity_command_frames(
    device_ids: list[int] | tuple[int, ...],
    velocity_rad_s: float,
    accel_time_ms: int,
    broadcast_id: int = VELOCITY_COMMAND_CAN_ID,
) -> tuple[CanFrame, ...]:
    """Encode the Jihua D7 broadcast velocity command used by actuator_sdk."""
    validate_device_id(broadcast_id)
    if not device_ids:
        raise ValueError("at least one device ID is required")
    if not 1 <= accel_time_ms <= 0xFFFF:
        raise ValueError("acceleration time must be in 1..65535 ms")
    raw = velocity_rad_s_to_raw(velocity_rad_s)
    fields: list[bytes] = []
    for device_id in device_ids:
        validate_device_id(device_id)
        if device_id > 0xFF:
            raise ValueError("D7 velocity command device ID must fit in one byte")
        fields.append(
            bytes((0x07, device_id, 0x31))
            + raw.to_bytes(2, "little")
            + accel_time_ms.to_bytes(2, "little")
        )

    frames: list[CanFrame] = []
    for offset in range(0, len(fields), VELOCITY_COMMANDS_PER_FRAME):
        chunk = fields[offset : offset + VELOCITY_COMMANDS_PER_FRAME]
        payload = bytes((len(chunk),)) + b"".join(chunk)
        package_header = bytes.fromhex("40 00 A1") + bytes((len(payload) << 2,))
        frames.append(
            CanFrame(
                broadcast_id,
                package_header + payload,
                is_fd=True,
                bitrate_switch=True,
            )
        )
    return tuple(frames)


def _device_frame(
    device_id: int,
    data: bytes,
    command_id: int | None = MOTOR_COMMAND_CAN_ID,
) -> CanFrame:
    validate_protocol_device_id(device_id)
    if command_id is not None:
        validate_device_id(command_id)
    payload = bytearray(data)
    if len(payload) > 5:
        payload[5] = device_id
    return CanFrame(
        device_id if command_id is None else command_id,
        bytes(payload),
        is_fd=True,
        bitrate_switch=True,
    )


def validate_device_id(device_id: int) -> None:
    if not 0 <= device_id <= 0x7FF:
        raise ValueError("device ID must be a standard CAN ID")


def validate_protocol_device_id(device_id: int) -> None:
    validate_device_id(device_id)
    if device_id > 0xFF:
        raise ValueError("motor protocol device ID must fit in one byte")


def read_motor_state_frame(
    device_id: int,
    command_id: int | None = MOTOR_COMMAND_CAN_ID,
) -> CanFrame:
    validate_protocol_device_id(device_id)
    if command_id is not None:
        validate_device_id(command_id)
    return CanFrame(
        device_id if command_id is None else command_id,
        READ_MOTOR_STATE_DATA,
        is_fd=True,
        bitrate_switch=True,
    )


def read_human_state_frame(
    device_id: int,
    command_id: int | None = MOTOR_COMMAND_CAN_ID,
) -> CanFrame:
    return _device_frame(device_id, READ_HUMAN_STATE_DATA, command_id)


def set_control_source_frame(
    device_id: int,
    command_id: int | None = MOTOR_COMMAND_CAN_ID,
) -> CanFrame:
    return _device_frame(device_id, SET_CONTROL_SOURCE_DATA, command_id)


def enable_frame(
    device_id: int,
    command_id: int | None = MOTOR_COMMAND_CAN_ID,
) -> CanFrame:
    return _device_frame(device_id, ENABLE_DATA, command_id)


def disable_frame(
    device_id: int,
    command_id: int | None = MOTOR_COMMAND_CAN_ID,
) -> CanFrame:
    return _device_frame(device_id, DISABLE_DATA, command_id)


def fault_reset_frame(
    device_id: int,
    command_id: int | None = MOTOR_COMMAND_CAN_ID,
) -> CanFrame:
    return _device_frame(device_id, FAULT_RESET_DATA, command_id)


def safe_start_preamble_frames(
    device_id: int,
    command_id: int | None = MOTOR_COMMAND_CAN_ID,
) -> tuple[CanFrame, CanFrame, CanFrame]:
    """Build the ordered setup frames sent before position heartbeat and enable."""
    return (
        set_control_source_frame(device_id, command_id),
        disable_frame(device_id, command_id),
        fault_reset_frame(device_id, command_id),
    )


def position_command_frame(
    device_id: int,
    position_rad: float,
    broadcast_id: int = MOTOR_COMMAND_CAN_ID,
) -> CanFrame:
    validate_protocol_device_id(device_id)
    validate_device_id(broadcast_id)
    raw = position_rad_to_raw(position_rad)
    payload = (
        bytes.fromhex("40 00 40 28 1C 01 06") + bytes((device_id, 0x00, 0x41)) + raw.to_bytes(2, "little")
    )
    return CanFrame(broadcast_id, payload, is_fd=True, bitrate_switch=True)


@dataclass(frozen=True, slots=True)
class MotorFeedback:
    device_id: int
    status: int
    status_text: str
    position_raw: int
    position_rad: float
    speed_raw: int
    current_raw: int
    voltage_raw: int
    voltage_v: float
    torque_raw: int
    motor_temperature_c: int
    driver_temperature_c: int
    alarm_bytes: bytes

    @property
    def is_running(self) -> bool:
        return is_running_status(self.status)


STATUS_TEXT = {0x00: "已停止 · 默认模式", 0x40: "已停止 · 位置模式", 0x42: "运行中 · 位置模式"}


def is_running_status(status: int) -> bool:
    return status == MOTOR_STATUS_RUNNING_POSITION


def running_state_confirmed(
    feedback: MotorFeedback | None,
    human_state: int | None = None,
) -> bool:
    """Accept either status 0x42 or HumanJoint State 2 as running confirmation."""
    return bool(feedback and feedback.is_running) or human_state == HUMAN_STATE_RUNNING


def broadcast_response_device_id(
    frame: CanFrame,
    response_base_id: int = MOTOR_RESPONSE_BASE_ID,
) -> int | None:
    """Return the device ID encoded by a D7 motor broadcast response CAN ID."""
    device_id = frame.arbitration_id - response_base_id
    if frame.is_extended or not 1 <= device_id <= 0xFF:
        return None
    return device_id


def decode_motor_feedback(frame: CanFrame, device_id: int) -> MotorFeedback | None:
    if (
        broadcast_response_device_id(frame) != device_id
        or len(frame.data) < 27
        or frame.data[7] != device_id & 0xFF
    ):
        return None
    data = frame.data
    position_raw = int.from_bytes(data[19:21], "little")
    status = data[9]
    voltage_raw = int.from_bytes(data[15:17], "little")
    return MotorFeedback(
        device_id=device_id,
        status=status,
        status_text=STATUS_TEXT.get(status, f"未知状态 0x{status:02X}"),
        current_raw=int.from_bytes(data[13:15], "little"),
        voltage_raw=voltage_raw,
        voltage_v=voltage_raw * 100.0 / POSITION_RAW_MAX,
        torque_raw=int.from_bytes(data[17:19], "little"),
        position_raw=position_raw,
        position_rad=position_raw_to_rad(position_raw),
        speed_raw=int.from_bytes(data[21:23], "little"),
        motor_temperature_c=data[25],
        driver_temperature_c=data[26],
        alarm_bytes=data[27:32],
    )


def decode_human_state(frame: CanFrame, device_id: int) -> int | None:
    data = frame.data
    if frame.arbitration_id != 0x100 + device_id or len(data) < 14:
        return None
    if data[7] != device_id & 0xFF or data[9:12] != bytes.fromhex("42 10 20"):
        return None
    return int.from_bytes(data[12:14], "little")
