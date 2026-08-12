from __future__ import annotations

from pathlib import Path

import pytest

from d7_factory_studio.core.models import CanFrame, CanMode
from d7_factory_studio.features.firmware.pace_battery_controller import (
    PaceBatteryUpgradeController,
    PaceNegativeAck,
    PaceUpgradeOptions,
)
from d7_factory_studio.protocols.pace_bms_upgrade import (
    COMMAND_DATA,
    COMMAND_ERASE,
    COMMAND_FINISH,
    COMMAND_PREPARE,
    PaceBmsUpgradeProtocol,
    checksum8,
    firmware_identifier,
    padded_blocks,
)


def ack(protocol: PaceBmsUpgradeProtocol, command: int, result: int = 0x01) -> CanFrame:
    prefix = bytes((0xAA, 0x55, result))
    return CanFrame(
        protocol.rx_id(command),
        prefix + bytes((checksum8(prefix), 0, 0, 0, 0)),
        is_extended=True,
        is_fd=False,
        bitrate_switch=False,
    )


class FakePaceTransport:
    def __init__(self, protocol: PaceBmsUpgradeProtocol) -> None:
        self.protocol = protocol
        self.sent: list[CanFrame] = []
        self.replies: list[CanFrame] = []
        self.is_open = True
        self.data_frames = 0
        self.identifier_ack_pending = False

    def open(self, _channel: int, _mode: CanMode) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def send(self, frame: CanFrame) -> None:
        self.sent.append(frame)
        command = (frame.arbitration_id >> 16) & 0xFF
        if command == COMMAND_PREPARE and frame.data[0] == 1:
            self.identifier_ack_pending = True
            self.replies.append(ack(self.protocol, command))
        elif command == COMMAND_PREPARE and frame.data == bytes.fromhex("AA 55 01 00 00 00 00 00"):
            if self.identifier_ack_pending:
                raise RuntimeError("jump frame sent before identifier ACK was consumed")
            self.replies.append(ack(self.protocol, command))
        elif command == COMMAND_ERASE:
            self.replies.append(ack(self.protocol, command))
        elif command == COMMAND_DATA:
            self.data_frames += 1
            if self.data_frames % 17 == 0:
                self.replies.append(ack(self.protocol, command))
        elif command == COMMAND_FINISH:
            self.replies.append(ack(self.protocol, command))

    def receive(self, _timeout_ms: int = 50) -> list[CanFrame]:
        if not self.replies:
            return []
        frame = self.replies.pop(0)
        if self.identifier_ack_pending:
            self.identifier_ack_pending = False
        return [frame]


def test_pace_ids_and_ack_require_extended_classic_frame() -> None:
    protocol = PaceBmsUpgradeProtocol(address=0)
    assert protocol.tx_id(COMMAND_PREPARE) == 0x14831101
    assert protocol.rx_id(COMMAND_PREPARE) == 0x14830111
    assert protocol.tx_id(COMMAND_ERASE) == 0x14841101
    assert protocol.rx_id(COMMAND_DATA) == 0x14860111
    assert protocol.tx_id(COMMAND_FINISH) == 0x14871101
    valid = ack(protocol, COMMAND_PREPARE)
    assert protocol.is_valid_ack(valid, COMMAND_PREPARE)
    negative = ack(protocol, COMMAND_PREPARE, 0xFF)
    assert protocol.ack_result(negative, COMMAND_PREPARE) == 0xFF
    assert not protocol.is_valid_ack(negative, COMMAND_PREPARE)
    assert not protocol.is_valid_ack(
        CanFrame(valid.arbitration_id, valid.data, is_extended=True, is_fd=True),
        COMMAND_PREPARE,
    )


def test_identifier_padding_data_control_and_finish_golden_frames() -> None:
    protocol = PaceBmsUpgradeProtocol()
    assert firmware_identifier("C50194V110-50195-1.19-001.bin") == "50194V110"
    identifier = protocol.identifier_frames("50194V110")
    assert [frame.data for frame in identifier] == [
        bytes.fromhex("01 35 30 31 39 34 56 31"),
        bytes.fromhex("02 31 30 00 00 00 00 00"),
    ]
    block = bytes(range(128))
    frames = protocol.data_block_frames(0x80, block)
    assert len(frames) == 17
    assert frames[0].data == bytes.fromhex("AA 55 80 00 00 00 80 C0")
    assert b"".join(frame.data for frame in frames[1:]) == block
    assert protocol.finish_frame().data == bytes.fromhex("AA 55 45 4E 44 D6 00 00")
    assert PaceBmsUpgradeProtocol().identifier_frames("ABC")[0].data == b"ABC\0\0\0\0\0"


def test_real_firmware_size_pads_to_778_blocks() -> None:
    blocks = padded_blocks(bytes(99_534))
    assert len(blocks) == 778
    assert sum(map(len, blocks)) == 99_584
    assert blocks[-1][-50:] == bytes((0xFF,)) * 50


def test_controller_sends_17_frames_per_block_and_reports_block_progress() -> None:
    protocol = PaceBmsUpgradeProtocol()
    transport = FakePaceTransport(protocol)
    messages: list[str] = []
    controller = PaceBatteryUpgradeController(
        transport,
        protocol,
        on_progress=lambda _value, message: messages.append(message),
    )
    controller.upgrade(
        bytes(range(256)) + bytes(44),
        "50194V110",
        PaceUpgradeOptions(pre_upgrade_pause_ms=0, post_erase_pause_ms=0),
    )
    commands = [(frame.arbitration_id >> 16) & 0xFF for frame in transport.sent]
    assert commands.count(COMMAND_DATA) == 3 * 17
    assert transport.sent[2].data == bytes.fromhex("AA 55 01 00 00 00 00 00")
    assert commands[-1] == COMMAND_FINISH
    assert "已写入 3/3 块" in messages
    assert messages[-1] == "Pace 电池升级完成"


def test_upgrade_file_rejects_non_bin_before_transport_io(tmp_path: Path) -> None:
    source = tmp_path / "C50194V110.hex"
    source.write_bytes(b"data")
    protocol = PaceBmsUpgradeProtocol()
    transport = FakePaceTransport(protocol)
    controller = PaceBatteryUpgradeController(transport, protocol)
    try:
        controller.upgrade_file(source)
    except ValueError as exc:
        assert ".bin" in str(exc)
    else:
        raise AssertionError("non-BIN Pace image was accepted")
    assert transport.sent == []


def test_pace_address_range_and_negative_ack_are_rejected() -> None:
    with pytest.raises(ValueError, match="0..16"):
        PaceBmsUpgradeProtocol(address=0x42)

    protocol = PaceBmsUpgradeProtocol()

    class NegativeEraseTransport(FakePaceTransport):
        def send(self, frame: CanFrame) -> None:
            command = (frame.arbitration_id >> 16) & 0xFF
            if command == COMMAND_ERASE:
                self.sent.append(frame)
                self.replies.append(ack(self.protocol, command, 0xFF))
                return
            super().send(frame)

    controller = PaceBatteryUpgradeController(NegativeEraseTransport(protocol), protocol)
    with pytest.raises(PaceNegativeAck, match="0x84.*0xFF"):
        controller.upgrade(
            bytes(128),
            "50194V110",
            PaceUpgradeOptions(pre_upgrade_pause_ms=0, post_erase_pause_ms=0),
        )
