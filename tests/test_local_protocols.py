from __future__ import annotations

import struct

import pytest

from d7_factory_studio.core.models import CanFrame
from d7_factory_studio.features.firmware.profiles import BATTERY_PROFILE, PMU_PROFILE, battery_profile
from d7_factory_studio.protocols.can_console import build_battery_frame, build_light_frame, xor_checksum
from d7_factory_studio.protocols.can_motor import (
    MOTOR_COMMAND_CAN_ID,
    decode_human_state,
    decode_motor_feedback,
    enable_frame,
    is_running_status,
    position_command_frame,
    read_human_state_frame,
    read_motor_state_frame,
    running_state_confirmed,
    safe_start_preamble_frames,
    velocity_command_frames,
    velocity_rad_s_to_raw,
)
from d7_factory_studio.protocols.iap import IapAck, IapProtocol, d7_crc32, d7_crc32_append
from d7_factory_studio.protocols.machine_info import (
    MACHINE_INFO_FIELDS,
    MACHINE_INFO_SUCCESS,
    build_machine_info_request,
    format_machine_info_value,
    parse_machine_info_response,
)
from d7_factory_studio.protocols.serial485 import (
    COMM_ID_PARAMETER,
    DEVICE_ID_PARAMETER,
    DEVICE_NAME_PARAMETER,
    Aa55ParameterReadError,
    Aa55ProtocolError,
    Aa55StreamDecoder,
    ParameterSpec,
    build_0d_read_multi,
    build_0e_write_multi,
    build_aa55_frame,
    build_echo,
    comm_id_write_item,
    decode_aa55_frame,
    parse_0d_response,
    relative_position_mode_items,
)


def test_d7_crc32_board_vectors_and_append() -> None:
    assert d7_crc32(b"") == 0xFFFFFFFF
    assert d7_crc32(b"123456789") == 0x1556F485
    assert d7_crc32(bytes(range(16))) == 0xEB99FA90
    assert d7_crc32_append(d7_crc32_append(0xFFFFFFFF, b"123"), b"456789") == 0x1556F485


def test_iap_pmu_defaults_and_golden_frames() -> None:
    protocol = IapProtocol()
    assert protocol.target_id == 0x18
    assert protocol.can_id == 0x7FF
    assert protocol.query_role() == CanFrame(
        0x7FF,
        bytes((0x16, 0x18, 0x02, 0, 0, 0, 0xE9, 0x19)),
        is_fd=False,
        bitrate_switch=False,
    )
    assert protocol.set_firmware_size(0x012345).data == bytes.fromhex("16 18 05 01 23 45 E9 85")
    assert protocol.set_segment_info(3, 0x123).data == bytes.fromhex("16 18 06 01 23 00 03 5B")


def test_iap_segment_crc_ack_and_custom_standard_can_id() -> None:
    protocol = IapProtocol(target_id=0x42, can_id=0x6A5)
    frames = protocol.segment_data_frames(b"\x01\x02\x03\x04\x05\x06\x07")
    assert [frame.data for frame in frames] == [
        bytes.fromhex("16 42 07 01 02 03 04 05"),
        bytes.fromhex("16 42 07 06 07 00 00 00"),
    ]
    data = bytes.fromhex("16 42 08 01 00 00 80 E1")
    assert protocol.parse_ack(data, 0x08) == IapAck(0x08, bytes.fromhex("01 00 00 80"))
    with pytest.raises(ValueError, match="11-bit"):
        IapProtocol(can_id=0x800)


def test_firmware_profiles_use_new_defaults_and_battery_shortcuts() -> None:
    assert PMU_PROFILE.target_id == 0x18
    assert BATTERY_PROFILE.target_id == 0x42
    assert [battery_profile(value).target_id for value in (0x41, 0x42, 0x43, 0x55)] == [
        0x41,
        0x42,
        0x43,
        0x55,
    ]
    assert battery_profile(can_id=0x6A5).can_id == 0x6A5


def test_can_console_protocol_frames() -> None:
    battery = build_battery_frame(0x42, 0x02, 52_000)
    assert battery.data[:7] == bytes.fromhex("82 02 00 00 CB 20 01")
    assert battery.data[7] == xor_checksum(battery.data[:7])
    light = build_light_frame(0x08, 200, 2, 3, 4, 500, (1, 2, 3))
    assert light.data == bytes.fromhex("90 C8 02 03 04 01 F4 A8")


def test_jihua_d7_velocity_golden_frame_and_group_split() -> None:
    assert [velocity_rad_s_to_raw(value) for value in (-14.0, -0.5, 0.0, 0.5, 14.0)] == [
        0x0000,
        0x7B6D,
        0x8000,
        0x8492,
        0xFFFF,
    ]
    frame = velocity_command_frames([0x10], 0.0, 1000)[0]
    assert frame == CanFrame(
        0x300,
        bytes.fromhex("40 00 A1 20 01 07 10 31 00 80 E8 03"),
        is_fd=True,
        bitrate_switch=True,
    )
    split = velocity_command_frames(list(range(0x10, 0x18)), 0.1, 1000)
    assert len(split) == 2
    assert split[0].data[4] == 7
    assert split[1].data[4] == 1


def test_jihua_motor_commands_default_to_0x300_and_embed_target_id() -> None:
    state = read_motor_state_frame(0x21)
    human = read_human_state_frame(0x21)
    enabled = enable_frame(0x21)

    assert MOTOR_COMMAND_CAN_ID == 0x300
    assert [frame.arbitration_id for frame in (state, human, enabled)] == [0x300] * 3
    assert state.data == bytes.fromhex("40 40 40 08 04 01")
    assert human.data == bytes.fromhex("40 40 00 09 1C 21 00 01 10 42 10 20")
    assert enabled.data == bytes.fromhex(
        "40 40 00 19 24 21 00 01 10 01 10 20 01 00 00 00"
    )
    assert enable_frame(0x21, 0x321).arbitration_id == 0x321


def test_jihua_safe_start_preamble_and_position_heartbeat_frames() -> None:
    preamble = safe_start_preamble_frames(0x21)

    assert [frame.arbitration_id for frame in preamble] == [0x300, 0x300, 0x300]
    assert [frame.data[9:12] for frame in preamble] == [
        bytes.fromhex("01 02 20"),
        bytes.fromhex("01 10 20"),
        bytes.fromhex("03 07 20"),
    ]
    heartbeat = position_command_frame(0x21, 0.0)
    assert heartbeat.arbitration_id == 0x300
    assert heartbeat.data == bytes.fromhex("40 00 40 28 1C 01 06 21 00 41 00 80")


def test_jihua_feedback_status_0x42_confirms_running() -> None:
    data = bytearray(32)
    data[7] = 0x21
    data[9] = 0x42
    data[15:17] = (0x8000).to_bytes(2, "little")
    data[19:21] = (0x8000).to_bytes(2, "little")
    feedback = decode_motor_feedback(CanFrame(0x121, bytes(data)), 0x21)

    assert feedback is not None
    assert feedback.status == 0x42
    assert feedback.status_text == "运行中 · 位置模式"
    assert feedback.is_running is True
    assert is_running_status(0x42) is True
    assert is_running_status(0x40) is False
    assert running_state_confirmed(feedback) is True

    human_data = bytearray(14)
    human_data[7] = 0x21
    human_data[9:12] = bytes.fromhex("42 10 20")
    human_data[12:14] = (2).to_bytes(2, "little")
    human_state = decode_human_state(CanFrame(0x121, bytes(human_data)), 0x21)
    assert human_state == 2
    assert running_state_confirmed(None, human_state) is True
    assert running_state_confirmed(None, 1) is False


@pytest.mark.parametrize("velocity", [-14.0001, 14.0001, float("nan")])
def test_jihua_d7_velocity_rejects_invalid_values(velocity: float) -> None:
    with pytest.raises(ValueError, match="-14..14"):
        velocity_rad_s_to_raw(velocity)
    with pytest.raises(ValueError, match="1..65535"):
        velocity_command_frames([0x10], 0.0, 0)


def test_machine_info_request_and_response() -> None:
    request = build_machine_info_request(0x18, 3)
    assert request.data[:3] == bytes((0, 0x53, 3))
    body = bytes((0x53, 3)) + struct.pack(">f", 1.5) + bytes((MACHINE_INFO_SUCCESS,))
    response = CanFrame(0x08, body + bytes((xor_checksum(body),)), is_fd=False, bitrate_switch=False)
    parsed = parse_machine_info_response(response)
    assert parsed is not None and parsed.slot == 3 and parsed.status == MACHINE_INFO_SUCCESS
    assert format_machine_info_value(MACHINE_INFO_FIELDS[3], parsed.raw) == "1.5"


def test_aa55_crc_validation_streaming_and_noise_recovery() -> None:
    raw = build_echo(station=0x10)
    decoded = decode_aa55_frame(raw)
    assert decoded.station == 0x10 and decoded.function == 0x08 and decoded.data == b"\x99\x99"
    damaged = raw[:-3] + bytes((raw[-3] ^ 1,)) + raw[-2:]
    with pytest.raises(Aa55ProtocolError, match="CRC"):
        decode_aa55_frame(damaged)
    decoder = Aa55StreamDecoder()
    assert decoder.feed(b"noise" + raw[:4]) == []
    assert decoder.feed(raw[4:] + raw) == [decoded, decoded]


def test_serial485_id_and_relative_position_golden_encoding() -> None:
    id_frame = build_0e_write_multi(station=0, items=[comm_id_write_item(0x11)])
    assert bytes.fromhex("10 11 07 20 11 01") in id_frame
    items = relative_position_mode_items(
        delta_counts=-4_194_304,
        profile_velocity_counts_s=1,
        acceleration_counts_s2=1,
        deceleration_counts_s2=1,
    )
    assert items[0] == bytes.fromhex("20 00 7A 60 00 00 C0 FF")


def test_serial485_0d_read_request_and_response_golden_frames() -> None:
    request = build_0d_read_multi(station=0, parameters=[COMM_ID_PARAMETER])
    assert request == bytes.fromhex("AA 0C 00 00 0D 01 00 10 11 07 20 04 3A 55")

    response = bytes.fromhex("AA 0E 00 00 0D 01 00 10 11 07 20 01 01 F4 2A 55")
    items = parse_0d_response(decode_aa55_frame(response), expected=[COMM_ID_PARAMETER])
    assert len(items) == 1
    assert items[0].address == 0x200711
    assert items[0].value == 0x0101

    device_request = build_0d_read_multi(station=0x20, parameters=[DEVICE_ID_PARAMETER])
    assert device_request == bytes.fromhex("AA 0C 00 20 0D 01 00 10 29 01 20 71 BA 55")

    device_response = bytes.fromhex("AA 0E 00 20 0D 01 00 10 29 01 20 21 00 D3 52 55")
    device_items = parse_0d_response(
        decode_aa55_frame(device_response), expected=[DEVICE_ID_PARAMETER]
    )
    assert device_items[0].value == 33


def test_serial485_0d_string_uses_dynamic_response_length() -> None:
    request = build_0d_read_multi(station=1, parameters=[DEVICE_NAME_PARAMETER])
    assert request[5:11] == bytes.fromhex("01 00 00 00 08 10")

    raw_text = b"JH-SERVO-D7"
    response = build_aa55_frame(
        station=1,
        function=0x0D,
        data=b"\x01\x00" + bytes((len(raw_text), 0x00, 0x08, 0x10)) + raw_text,
    )
    items = parse_0d_response(decode_aa55_frame(response), expected=[DEVICE_NAME_PARAMETER])

    assert items[0].address == 0x100800
    assert items[0].value == "JH-SERVO-D7"


def test_serial485_0d_rejects_error_items_wrong_types_and_trailing_data() -> None:
    error_frame = build_aa55_frame(
        station=0,
        function=0x0D,
        data=bytes.fromhex("01 00 FF 11 07 20 08"),
    )
    with pytest.raises(Aa55ParameterReadError, match="0x200711=0x08") as error:
        parse_0d_response(decode_aa55_frame(error_frame), expected=[COMM_ID_PARAMETER])
    assert error.value.items[0].error_code == 0x08

    legacy_error = build_aa55_frame(
        station=0,
        function=0x8D,
        data=bytes.fromhex("01 00 FF 11 07 20 08"),
    )
    with pytest.raises(Aa55ParameterReadError, match="0x200711=0x08"):
        parse_0d_response(decode_aa55_frame(legacy_error), expected=[COMM_ID_PARAMETER])

    wrong_type = build_aa55_frame(
        station=0,
        function=0x0D,
        data=bytes.fromhex("01 00 20 11 07 20 01 01 00 00"),
    )
    with pytest.raises(Aa55ProtocolError, match="length flag mismatch"):
        parse_0d_response(decode_aa55_frame(wrong_type), expected=[COMM_ID_PARAMETER])

    trailing = build_aa55_frame(
        station=0,
        function=0x0D,
        data=bytes.fromhex("01 00 10 11 07 20 01 01 00"),
    )
    with pytest.raises(Aa55ProtocolError, match="trailing bytes"):
        parse_0d_response(decode_aa55_frame(trailing), expected=[COMM_ID_PARAMETER])


def test_serial485_0d_rejects_count_address_and_truncation_mismatches() -> None:
    other = ParameterSpec(0x200804, 0x20, signed=True)
    count_mismatch = build_aa55_frame(station=0, function=0x0D, data=b"\x02\x00")
    with pytest.raises(Aa55ProtocolError, match="item count mismatch"):
        parse_0d_response(decode_aa55_frame(count_mismatch), expected=[COMM_ID_PARAMETER])

    unexpected = build_aa55_frame(
        station=0,
        function=0x0D,
        data=bytes.fromhex("01 00 20 04 08 20 01 00 00 00"),
    )
    with pytest.raises(Aa55ProtocolError, match="unexpected parameter-read address"):
        parse_0d_response(decode_aa55_frame(unexpected), expected=[COMM_ID_PARAMETER])

    truncated = build_aa55_frame(
        station=0,
        function=0x0D,
        data=bytes.fromhex("01 00 20 04 08 20 01 00"),
    )
    with pytest.raises(Aa55ProtocolError, match="value is truncated"):
        parse_0d_response(decode_aa55_frame(truncated), expected=[other])
