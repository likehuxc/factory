from __future__ import annotations

import struct
from dataclasses import dataclass

from d7_factory_studio.core.models import CanFrame
from d7_factory_studio.protocols.can_console import xor_checksum

MACHINE_INFO_RESPONSE_CAN_ID = 0x08
MACHINE_INFO_SUCCESS = 0x02


@dataclass(frozen=True, slots=True)
class MachineInfoField:
    key: str
    slot: int
    value_type: str
    byte_index: int | None = None

    @property
    def type_label(self) -> str:
        return f"byte[{self.byte_index}]" if self.value_type == "byte" else self.value_type


@dataclass(frozen=True, slots=True)
class MachineInfoResponse:
    slot: int
    raw: bytes
    status: int


MACHINE_INFO_FIELDS = tuple(
    MachineInfoField(key, slot, value_type, byte_index)
    for key, slot, value_type, byte_index in (
        ("wheel_diameter", 0, "float", None),
        ("wheel_perimeter", 1, "float", None),
        ("wheel_base", 2, "float", None),
        ("pulse_per_circle", 3, "float", None),
        ("sample_times_per_pulse", 4, "float", None),
        ("reduction_ratio", 5, "float", None),
        ("is_encoder_count_inv", 6, "int", None),
        ("uwb_tag_pcb_major_version", 7, "int", None),
        ("uwb_tag_pcb_minor_version", 8, "int", None),
        ("chassis_pcb_major_version", 9, "int", None),
        ("chassis_pcb_minor_version", 10, "int", None),
        ("infrared_sensor_version", 11, "int", None),
        ("lds_sensor_version", 12, "int", None),
        ("motor_version", 13, "int", None),
        ("weigh_sensor_version", 14, "int", None),
        ("battery_version", 15, "int", None),
        ("uwb_tag_pcb_mpd_year", 16, "int", None),
        ("uwb_tag_pcb_mpd_month", 17, "int", None),
        ("uwb_tag_pcb_mpd_day", 18, "int", None),
        ("chassis_pcb_mpd_year", 19, "int", None),
        ("chassis_pcb_mpd_month", 20, "int", None),
        ("chassis_pcb_mpd_day", 21, "int", None),
        ("slam_camera_version", 22, "int", None),
        ("esp32_type", 23, "byte", 3),
        ("rgbd_type", 23, "byte", 2),
        ("machine_type", 23, "byte", 1),
        ("audio_version", 23, "byte", 0),
        ("lora_type", 24, "byte", 3),
        ("scan_code_device", 24, "byte", 2),
        ("monocular_camera", 24, "byte", 1),
        ("slam_core", 24, "byte", 0),
        ("product_type", 27, "product", None),
        ("npu_type", 28, "byte", 3),
        ("matrix_mic_type", 28, "byte", 2),
        ("host_core_board_type", 28, "byte", 1),
        ("head_board_type", 28, "byte", 0),
        ("lidar_communicate_type", 29, "byte", 3),
        ("lidar_type", 29, "byte", 2),
        ("lte_type", 29, "byte", 1),
        ("cabin_door_motor_type", 29, "byte", 0),
        ("chassis_board_type", 30, "byte", 1),
        ("function_board_type", 30, "byte", 2),
        ("cabin_door_board_type", 30, "byte", 0),
        ("laser_projection", 31, "byte", 3),
        ("vedio_output", 31, "byte", 2),
        ("distribution_area_light", 31, "byte", 1),
        ("magic_sensor", 31, "byte", 0),
        ("power_board_type", 32, "byte", 3),
        ("usbcan_board_type", 32, "byte", 2),
        ("pdu1_board_type", 32, "byte", 1),
        ("pdu2_board_type", 32, "byte", 0),
        ("rgbd_angle_type", 34, "byte", 3),
        ("lidar_communicate_type_second", 34, "byte", 2),
        ("lidar_type_second", 34, "byte", 1),
        ("machine_color", 34, "byte", 0),
    )
)


def build_machine_info_request(can_id: int, slot: int) -> CanFrame:
    if not 0 <= can_id <= 0x7FF:
        raise ValueError("MachineInfo request CAN ID must be standard")
    if not 0 <= slot < 76:
        raise ValueError("MachineInfo slot must be in 0..75")
    body = bytes((0x00, 0x53, slot, 0, 0, 0, 0))
    return CanFrame(can_id, body + bytes((xor_checksum(body),)), is_fd=False, bitrate_switch=False)


def parse_machine_info_response(
    frame: CanFrame,
    response_can_id: int = MACHINE_INFO_RESPONSE_CAN_ID,
) -> MachineInfoResponse | None:
    payload = frame.data
    if frame.arbitration_id != response_can_id or frame.is_extended or frame.is_fd or len(payload) != 8:
        return None
    if payload[0] != 0x53 or payload[7] != xor_checksum(payload[:7]):
        return None
    return MachineInfoResponse(slot=payload[1], raw=payload[2:6], status=payload[6])


def format_machine_info_value(field: MachineInfoField, raw: bytes) -> str:
    if len(raw) != 4:
        raise ValueError("MachineInfo data must be exactly four bytes")
    if field.value_type == "float":
        return f"{struct.unpack('>f', raw)[0]:.7g}"
    if field.value_type == "int":
        return str(int.from_bytes(raw, "big"))
    if field.value_type == "byte" and field.byte_index is not None:
        return str(raw[field.byte_index])
    if field.value_type == "product":
        return f"model={int.from_bytes(raw[2:4], 'big')}, version={raw[1]}.{raw[0]}"
    raise ValueError(f"unsupported MachineInfo type: {field.value_type}")
