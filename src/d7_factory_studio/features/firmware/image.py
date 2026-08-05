from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

SRAM_BASE = 0x20000000
SRAM_SIZE = 192 * 1024
APP_BASE = 0x00020000
MAX_APP_SIZE = 376 * 1024
SECTION_SIZE = 1024


@dataclass(frozen=True, slots=True)
class FirmwareSection:
    number: int
    data: bytes


@dataclass(frozen=True, slots=True)
class FirmwareImage:
    data: bytes
    initial_sp: int
    reset_vector: int
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_file(cls, path: str | Path) -> FirmwareImage:
        try:
            return cls.from_bytes(Path(path).read_bytes())
        except OSError as exc:
            raise ValueError(f"无法读取固件文件: {exc}") from exc

    @classmethod
    def from_bytes(cls, data: bytes) -> FirmwareImage:
        image_data = bytes(data)
        if not image_data:
            raise ValueError("固件文件为空")
        if len(image_data) > MAX_APP_SIZE:
            raise ValueError(f"固件超过最大大小 {MAX_APP_SIZE} bytes")
        if len(image_data) < 8:
            raise ValueError("固件过小，缺少向量表")
        initial_sp, reset_vector = struct.unpack_from("<II", image_data)
        if not SRAM_BASE < initial_sp <= SRAM_BASE + SRAM_SIZE:
            raise ValueError(f"初始 SP 0x{initial_sp:08X} 不在 SRAM 范围内")
        warnings: list[str] = []
        if reset_vector < APP_BASE:
            warnings.append("固件疑似按 0x00000000 链接，不是 0x00020000 APP 镜像")
        elif reset_vector >= APP_BASE + MAX_APP_SIZE:
            warnings.append(f"ResetVector 0x{reset_vector:08X} 超出预期 APP 区间")
        return cls(image_data, initial_sp, reset_vector, tuple(warnings))

    @property
    def size(self) -> int:
        return len(self.data)

    def sections(self, section_size: int = SECTION_SIZE) -> list[FirmwareSection]:
        if not 1 <= section_size <= SECTION_SIZE:
            raise ValueError("section_size must be in 1..1024")
        return [
            FirmwareSection(offset // section_size, self.data[offset : offset + section_size])
            for offset in range(0, self.size, section_size)
        ]
