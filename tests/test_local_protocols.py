from __future__ import annotations

import struct

import pytest

from d7_factory_studio.core.models import CanFrame
from d7_factory_studio.features.firmware.profiles import BATTERY_PROFILE, PMU_PROFILE, battery_profile
from d7_factory_studio.protocols.can_console import build_battery_frame, build_light_frame, xor_checksum
from d7_factory_studio.protocols.iap import IapAck, IapProtocol, d7_crc32, d7_crc32_append
from d7_factory_studio.protocols.machine_info import (
    MACHINE_INFO_FIELDS,
    MACHINE_INFO_SUCCESS,
    build_machine_info_request,
    format_machine_info_value,
    parse_machine_info_response,
)
from d7_factory_studio.protocols.serial485 import (
    Aa55ProtocolError,
    Aa55StreamDecoder,
    build_0e_write_multi,
    build_echo,
    comm_id_write_item,
    decode_aa55_frame,
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
