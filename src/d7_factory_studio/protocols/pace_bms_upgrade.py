from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from d7_factory_studio.core.models import CanFrame

PACE_PRIORITY = 5
COMMAND_PREPARE = 0x83
COMMAND_ERASE = 0x84
COMMAND_DATA = 0x86
COMMAND_FINISH = 0x87
BLOCK_SIZE = 128
DATA_FRAME_SIZE = 8


def checksum8(data: bytes | bytearray | Iterable[int]) -> int:
    return sum(data) & 0xFF


def firmware_identifier(path: str | Path) -> str:
    """Return the identifier used by V9213, e.g. C50194V110-... -> 50194V110."""
    stem_prefix = Path(path).stem.split("-", 1)[0]
    if len(stem_prefix) < 2:
        raise ValueError("Pace 固件文件名缺少型号标识")
    identifier = stem_prefix[1:]
    try:
        encoded = identifier.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Pace 固件型号标识必须是 ASCII 字符") from exc
    if not encoded:
        raise ValueError("Pace 固件型号标识不能为空")
    return identifier


def padded_blocks(data: bytes, block_size: int = BLOCK_SIZE) -> tuple[bytes, ...]:
    if not data:
        raise ValueError("固件文件为空")
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    padded_size = ((len(data) + block_size - 1) // block_size) * block_size
    padded = bytes(data).ljust(padded_size, b"\xFF")
    return tuple(padded[offset : offset + block_size] for offset in range(0, padded_size, block_size))


class PaceBmsUpgradeProtocol:
    """Pure encoder/decoder for the Pace V9213 BIN upgrade protocol."""

    def __init__(self, address: int = 0) -> None:
        if not 0 <= address <= 16:
            raise ValueError("Pace 电池地址必须在 0..16")
        self.address = address

    def tx_id(self, command: int) -> int:
        self._validate_command(command)
        return (PACE_PRIORITY << 26) | (command << 16) | ((self.address + 0x11) << 8) | 0x01

    def rx_id(self, command: int) -> int:
        self._validate_command(command)
        return (PACE_PRIORITY << 26) | (command << 16) | (0x01 << 8) | (self.address + 0x11)

    def identifier_frames(self, identifier: str) -> tuple[CanFrame, ...]:
        try:
            payload = identifier.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Pace 固件型号标识必须是 ASCII 字符") from exc
        if not payload:
            raise ValueError("Pace 固件型号标识不能为空")
        if len(payload) < 8:
            return (self._frame(COMMAND_PREPARE, payload.ljust(8, b"\0")),)
        chunks = tuple(payload[offset : offset + 7] for offset in range(0, len(payload), 7))
        if len(chunks) > 0xFF:
            raise ValueError("Pace 固件型号标识过长")
        return tuple(
            self._frame(COMMAND_PREPARE, bytes((sequence,)) + chunk.ljust(7, b"\0"))
            for sequence, chunk in enumerate(chunks, start=1)
        )

    def prepare_frame(self) -> CanFrame:
        return self._frame(COMMAND_PREPARE, bytes.fromhex("AA 55 01 00 00 00 00 00"))

    def erase_frame(self) -> CanFrame:
        return self._frame(COMMAND_ERASE, bytes.fromhex("AA 55 01 00 00 00 00 00"))

    def data_block_frames(self, offset: int, block: bytes) -> tuple[CanFrame, ...]:
        if offset < 0 or offset > 0xFFFFFFFF:
            raise ValueError("固件偏移超出 uint32 范围")
        if len(block) != BLOCK_SIZE:
            raise ValueError(f"Pace 数据块必须是 {BLOCK_SIZE} 字节")
        control = bytes.fromhex("AA 55 80") + offset.to_bytes(4, "big") + bytes((checksum8(block),))
        frames = [self._frame(COMMAND_DATA, control)]
        frames.extend(
            self._frame(COMMAND_DATA, block[index : index + DATA_FRAME_SIZE])
            for index in range(0, BLOCK_SIZE, DATA_FRAME_SIZE)
        )
        return tuple(frames)

    def finish_frame(self) -> CanFrame:
        prefix = bytes.fromhex("AA 55 45 4E 44")
        return self._frame(COMMAND_FINISH, prefix + bytes((checksum8(prefix), 0, 0)))

    def is_valid_ack(self, frame: CanFrame, command: int) -> bool:
        return self.ack_result(frame, command) == 0x01

    def ack_result(self, frame: CanFrame, command: int) -> int | None:
        if (
            frame.arbitration_id != self.rx_id(command)
            or not frame.is_extended
            or frame.is_fd
            or len(frame.data) < 4
        ):
            return None
        if frame.data[3] != checksum8(frame.data[:3]):
            return None
        return frame.data[2]

    def _frame(self, command: int, data: bytes) -> CanFrame:
        if len(data) != 8:
            raise ValueError("Pace 升级帧必须是 8 字节")
        return CanFrame(
            self.tx_id(command),
            data,
            is_extended=True,
            is_fd=False,
            bitrate_switch=False,
        )

    @staticmethod
    def _validate_command(command: int) -> None:
        if not 0 <= command <= 0xFF:
            raise ValueError("Pace command must fit in one byte")
