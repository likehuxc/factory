from __future__ import annotations

import struct

import pytest

from d7_factory_studio.core.models import CanFrame, CanMode
from d7_factory_studio.core.ports import CancellationToken, OperationCancelled
from d7_factory_studio.features.firmware.controller import (
    FirmwareUpgradeController,
    UpgradeOptions,
    build_upgrade_preview_frames,
)
from d7_factory_studio.features.firmware.image import FirmwareImage
from d7_factory_studio.features.firmware.profiles import BATTERY_PROFILE
from d7_factory_studio.protocols.iap import (
    CMD_GET_RUN_ROLE,
    CMD_JUMP_TO_APP,
    CMD_SET_SEGMENT_INFO,
    CMD_VALIDATE_SEGMENT_DATA,
    IapProtocol,
    checksum8,
)


def make_ack(command: int, target: int, params: bytes = b"\0\0\0\xe9") -> CanFrame:
    data = bytes((0x16, target, command)) + params[:4].ljust(4, b"\0")
    if command not in (CMD_SET_SEGMENT_INFO, CMD_VALIDATE_SEGMENT_DATA):
        data = data[:6] + b"\xe9"
    data += bytes((checksum8(data),))
    return CanFrame(target, data, is_fd=False, bitrate_switch=False)


class FakeIapTransport:
    def __init__(self, target: int = 0x18) -> None:
        self.target = target
        self.sent: list[CanFrame] = []
        self.replies: list[CanFrame] = []
        self.opened = False
        self.mode: CanMode | None = None
        self.jumped = False

    @property
    def is_open(self) -> bool:
        return self.opened

    def open(self, channel: int, mode: CanMode) -> None:
        assert channel == 0
        self.opened = True
        self.mode = mode

    def close(self) -> None:
        self.opened = False

    def send(self, frame: CanFrame) -> None:
        self.sent.append(frame)
        command = frame.data[2]
        if command == CMD_JUMP_TO_APP:
            self.jumped = True
        elif command == CMD_GET_RUN_ROLE:
            role = 0 if self.jumped else 1
            self.replies.append(make_ack(command, self.target, bytes((role, 0, 0, 0xE9))))
        elif command == CMD_SET_SEGMENT_INFO:
            self.replies.append(make_ack(command, self.target, b"\0\0\0\0"))
        elif command == CMD_VALIDATE_SEGMENT_DATA:
            self.replies.append(make_ack(command, self.target, b"\x01\0\0\x80"))
        elif command != 0x07:
            self.replies.append(make_ack(command, self.target))

    def receive(self, timeout_ms: int = 50) -> list[CanFrame]:
        return [self.replies.pop(0)] if self.replies else []


def test_full_upgrade_uses_fd_channel_with_classic_iap_frames() -> None:
    image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + b"\x01\x02\x03\x04")
    transport = FakeIapTransport()
    progress: list[int] = []
    controller = FirmwareUpgradeController(
        transport, on_progress=lambda value, _message: progress.append(value)
    )
    controller.upgrade(
        image,
        UpgradeOptions(boot_wait_ms=0, app_start_wait_ms=0, data_frame_delay_ms=0),
    )
    assert transport.mode is CanMode.FD
    assert all(not frame.is_fd for frame in transport.sent)
    assert [frame.data[2] for frame in transport.sent][-2:] == [CMD_JUMP_TO_APP, CMD_GET_RUN_ROLE]
    assert progress[-1] == 100


def test_pre_cancelled_upgrade_sends_nothing() -> None:
    image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9))
    transport = FakeIapTransport()
    token = CancellationToken()
    token.cancel()
    with pytest.raises(OperationCancelled):
        FirmwareUpgradeController(transport, IapProtocol()).upgrade(image, token=token)
    assert transport.sent == []


def test_profile_controller_carries_battery_upgrade_defaults() -> None:
    controller = FirmwareUpgradeController.for_profile(FakeIapTransport(0x42), BATTERY_PROFILE)
    assert controller.protocol.target_id == 0x42
    assert controller.default_options.pre_upgrade_wakeup_ms == 1_000
    assert controller.default_options.disable_target_can_messages is True
    assert controller.default_options.ignore_validate_ack_failure is True


def test_battery_preview_is_available_without_transport_io() -> None:
    image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + bytes(64))
    options = UpgradeOptions.for_profile(BATTERY_PROFILE)
    preview = build_upgrade_preview_frames(
        IapProtocol(BATTERY_PROFILE.target_id), image, options, max_data_frames=2
    )
    assert [frame.data[2] for _label, frame in preview] == [
        CMD_GET_RUN_ROLE,
        CMD_GET_RUN_ROLE,
        0x01,
        CMD_GET_RUN_ROLE,
        0x04,
        0x05,
        CMD_SET_SEGMENT_INFO,
        0x07,
        0x07,
    ]


def test_firmware_image_rejects_bad_stack_and_splits_1024() -> None:
    with pytest.raises(ValueError, match="SP"):
        FirmwareImage.from_bytes(struct.pack("<II", 0x10000000, 0x000202C9))
    image = FirmwareImage.from_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + bytes(1100))
    assert [len(section.data) for section in image.sections()] == [1024, 84]
