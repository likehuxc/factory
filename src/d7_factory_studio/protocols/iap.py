from __future__ import annotations

from dataclasses import dataclass

from d7_factory_studio.core.models import CanFrame

PROTOCOL_HEAD = 0x16
PROTOCOL_TAIL = (~PROTOCOL_HEAD) & 0xFF

CMD_REBOOT_TO_BOOTLOADER = 0x01
CMD_GET_RUN_ROLE = 0x02
CMD_GET_SOFT_VERSION = 0x03
CMD_ENABLE_CAN = 0x04
CMD_SET_FIRMWARE_SIZE = 0x05
CMD_SET_SEGMENT_INFO = 0x06
CMD_FILL_SEGMENT_DATA = 0x07
CMD_VALIDATE_SEGMENT_DATA = 0x08
CMD_JUMP_TO_APP = 0x09
CMD_GET_BOARD_CHIP_TYPE = 0x10

RUN_ROLE_APP = "APP"
RUN_ROLE_BOOTLOADER = "BOOT"


def checksum8(data: bytes) -> int:
    return sum(data) & 0xFF


def _build_crc32_table() -> tuple[int, ...]:
    table: list[int] = []
    for index in range(256):
        value = index << 24
        for _ in range(8):
            value = (
                ((value << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if value & 0x80000000 else (value << 1) & 0xFFFFFFFF
            )
        table.append(value)
    return tuple(table)


CRC32_TABLE = _build_crc32_table()


def d7_crc32_append(current: int, data: bytes | bytearray | memoryview) -> int:
    value = current & 0xFFFFFFFF
    for byte in bytes(data):
        value ^= byte
        for _ in range(4):
            value = ((value << 8) & 0xFFFFFFFF) ^ CRC32_TABLE[(value >> 24) & 0xFF]
    return value & 0xFFFFFFFF


def d7_crc32(data: bytes | bytearray | memoryview) -> int:
    return d7_crc32_append(0xFFFFFFFF, data)


@dataclass(frozen=True, slots=True)
class IapAck:
    command: int
    params: bytes


class IapProtocol:
    """D7 IAP byte protocol. It has no transport or UI dependencies."""

    def __init__(self, target_id: int = 0x18, can_id: int = 0x7FF) -> None:
        if not 0 <= target_id <= 0xFF:
            raise ValueError("IAP target_id must fit in the protocol byte (0..0xFF)")
        if not 0 <= can_id <= 0x7FF:
            raise ValueError("IAP CAN ID must be a standard 11-bit ID")
        self.target_id = target_id
        self.can_id = can_id

    def reboot_to_bootloader(self) -> CanFrame:
        return self._tail_command(CMD_REBOOT_TO_BOOTLOADER)

    def query_role(self) -> CanFrame:
        return self._tail_command(CMD_GET_RUN_ROLE)

    def get_software_version(self) -> CanFrame:
        return self._tail_command(CMD_GET_SOFT_VERSION)

    def get_board_chip_type(self) -> CanFrame:
        return self._tail_command(CMD_GET_BOARD_CHIP_TYPE)

    def set_can_messages_enabled(self, enabled: bool) -> CanFrame:
        return self._tail_command(CMD_ENABLE_CAN, bytes((int(enabled), 0, 0)))

    def set_firmware_size(self, total_size: int) -> CanFrame:
        if not 0 <= total_size <= 0xFFFFFF:
            raise ValueError("firmware size must fit in 24 bits")
        return self._tail_command(CMD_SET_FIRMWARE_SIZE, total_size.to_bytes(3, "big"))

    def set_segment_info(self, section_num: int, section_size: int) -> CanFrame:
        if not 0 <= section_num <= 0xFFFF:
            raise ValueError("section number must fit in 16 bits")
        if not 0 <= section_size <= 1024:
            raise ValueError("section size must be in 0..1024")
        params = section_size.to_bytes(2, "big") + section_num.to_bytes(2, "big")
        return self._checksum_command(CMD_SET_SEGMENT_INFO, params)

    def segment_data_frames(self, section_data: bytes) -> list[CanFrame]:
        if len(section_data) > 1024:
            raise ValueError("section data cannot exceed 1024 bytes")
        return [
            self._frame(
                bytes((PROTOCOL_HEAD, self.target_id, CMD_FILL_SEGMENT_DATA))
                + section_data[i : i + 5].ljust(5, b"\0")
            )
            for i in range(0, len(section_data), 5)
        ]

    def validate_segment(self, section_num: int, section_data: bytes, crc_head_extra: int = 0) -> CanFrame:
        if not 0 <= section_num <= 0xFFFF:
            raise ValueError("section number must fit in 16 bits")
        if len(section_data) > 1024:
            raise ValueError("section data cannot exceed 1024 bytes")
        head = section_num.to_bytes(2, "big") + bytes(
            (PROTOCOL_HEAD, self.target_id, CMD_VALIDATE_SEGMENT_DATA, crc_head_extra & 0xFF)
        )
        crc = d7_crc32(head + bytes(section_data))
        data = bytes((PROTOCOL_HEAD, self.target_id, CMD_VALIDATE_SEGMENT_DATA, crc_head_extra & 0xFF))
        return self._frame(data + crc.to_bytes(4, "big"))

    def jump_to_app(self) -> CanFrame:
        return self._tail_command(CMD_JUMP_TO_APP)

    def parse_ack(self, data: bytes, expected_cmd: int | None = None) -> IapAck:
        frame = bytes(data)
        if len(frame) != 8:
            raise ValueError("ACK must be exactly 8 bytes")
        if frame[0] != PROTOCOL_HEAD:
            raise ValueError(f"ACK protocol head mismatch: 0x{frame[0]:02X}")
        if frame[1] != self.target_id:
            raise ValueError(f"ACK target ID mismatch: 0x{frame[1]:02X}")
        if expected_cmd is not None and frame[2] != expected_cmd:
            raise ValueError(f"ACK command mismatch: got 0x{frame[2]:02X}, expected 0x{expected_cmd:02X}")
        expected_checksum = checksum8(frame[:7])
        if frame[7] != expected_checksum:
            raise ValueError(
                f"ACK checksum mismatch: got 0x{frame[7]:02X}, expected 0x{expected_checksum:02X}"
            )
        if frame[2] not in (CMD_SET_SEGMENT_INFO, CMD_VALIDATE_SEGMENT_DATA) and frame[6] != PROTOCOL_TAIL:
            raise ValueError(f"ACK tail mismatch: got 0x{frame[6]:02X}, expected 0x{PROTOCOL_TAIL:02X}")
        return IapAck(frame[2], frame[3:7])

    @staticmethod
    def ack_role(ack: IapAck) -> str:
        if ack.command != CMD_GET_RUN_ROLE:
            raise ValueError("ACK is not a role response")
        return RUN_ROLE_BOOTLOADER if ack.params[0] == 1 else RUN_ROLE_APP

    @staticmethod
    def ack_software_version(ack: IapAck) -> str:
        if ack.command != CMD_GET_SOFT_VERSION:
            raise ValueError("ACK is not a software-version response")
        return ".".join(str(part) for part in ack.params[:3])

    def _tail_command(self, command: int, params: bytes = b"\0\0\0") -> CanFrame:
        if len(params) != 3:
            raise ValueError("tail command params must be exactly 3 bytes")
        data = bytes((PROTOCOL_HEAD, self.target_id, command)) + params + bytes((PROTOCOL_TAIL,))
        return self._frame(data + bytes((checksum8(data),)))

    def _checksum_command(self, command: int, params: bytes) -> CanFrame:
        if len(params) != 4:
            raise ValueError("command params must be exactly 4 bytes")
        data = bytes((PROTOCOL_HEAD, self.target_id, command)) + params
        return self._frame(data + bytes((checksum8(data),)))

    def _frame(self, data: bytes) -> CanFrame:
        return CanFrame(self.can_id, data, is_fd=False, bitrate_switch=False)
