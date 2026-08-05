from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from d7_factory_studio.core.models import CanFrame, CanMode
from d7_factory_studio.transports.zlg.discovery import (
    PE_MACHINE_AMD64,
    REQUIRED_EXPORTS,
    ZlgDllDiscoveryError,
    read_pe_machine,
    validate_controlcanfd,
)
from d7_factory_studio.transports.zlg.driver import (
    STATUS_OK,
    ZCAN_USBCANFD_200U,
    ZcanReceiveData,
    ZcanReceiveFdData,
    ZlgCanTransport,
)


class FakeFunction:
    def __init__(self, function):
        self.function = function
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.function(*args)


class ExportOnlyDll:
    pass


def write_minimal_pe(path: Path, machine: int) -> None:
    content = bytearray(256)
    content[:2] = b"MZ"
    content[0x3C:0x40] = (0x80).to_bytes(4, "little")
    content[0x80:0x84] = b"PE\0\0"
    content[0x84:0x86] = machine.to_bytes(2, "little")
    path.write_bytes(content)


def test_dll_validation_requires_x64_and_exports(tmp_path: Path) -> None:
    path = tmp_path / "ControlCANFD.dll"
    write_minimal_pe(path, PE_MACHINE_AMD64)
    dll = ExportOnlyDll()
    for name in REQUIRED_EXPORTS:
        setattr(dll, name, object())
    assert read_pe_machine(path) == PE_MACHINE_AMD64
    assert validate_controlcanfd(path, loader=lambda _path: dll).path == path.resolve()
    write_minimal_pe(path, 0x014C)
    with pytest.raises(ZlgDllDiscoveryError, match="not x64"):
        validate_controlcanfd(path, loader=lambda _path: dll)


class FakeZlgDll:
    def __init__(self) -> None:
        self.ZCAN_OpenDevice = FakeFunction(lambda *_args: 0x1000)
        self.ZCAN_CloseDevice = FakeFunction(lambda *_args: STATUS_OK)
        self.ZCAN_InitCAN = FakeFunction(lambda *_args: 0x2000)
        self.ZCAN_StartCAN = FakeFunction(lambda *_args: STATUS_OK)
        self.ZCAN_ResetCAN = FakeFunction(lambda *_args: STATUS_OK)
        self.ZCAN_SetAbitBaud = FakeFunction(lambda *_args: STATUS_OK)
        self.ZCAN_SetDbitBaud = FakeFunction(lambda *_args: STATUS_OK)
        self.ZCAN_Transmit = FakeFunction(lambda *_args: 1)
        self.ZCAN_TransmitFD = FakeFunction(lambda *_args: 1)
        self.ZCAN_Receive = FakeFunction(self._receive_classic)
        self.ZCAN_ReceiveFD = FakeFunction(self._receive_fd)

    @staticmethod
    def _receive_classic(_handle, pointer, _size, _timeout):
        buffer = ctypes.cast(pointer, ctypes.POINTER(ZcanReceiveData))
        buffer[0].frame.can_id = 0x18
        buffer[0].frame.can_dlc = 1
        buffer[0].frame.data[0] = 0xAA
        return 1

    @staticmethod
    def _receive_fd(_handle, pointer, _size, _timeout):
        buffer = ctypes.cast(pointer, ctypes.POINTER(ZcanReceiveFdData))
        buffer[0].frame.can_id = 0x300
        buffer[0].frame.len = 12
        buffer[0].frame.brs = 1
        for index in range(12):
            buffer[0].frame.data[index] = index
        return 1


def test_type_41_fd_open_and_dual_queue_receive() -> None:
    dll = FakeZlgDll()
    transport = ZlgCanTransport(dll=dll)
    transport.open(0, CanMode.FD)
    assert dll.ZCAN_OpenDevice.calls[0][0] == ZCAN_USBCANFD_200U == 41
    frames = transport.receive(0)
    assert [(frame.arbitration_id, frame.is_fd) for frame in frames] == [(0x18, False), (0x300, True)]
    transport.send(CanFrame(0x7FF, b"\x16", is_fd=False, bitrate_switch=False))
    transport.send(CanFrame(0x300, bytes(12), is_fd=True, bitrate_switch=True))
    assert len(dll.ZCAN_Transmit.calls) == 1 and len(dll.ZCAN_TransmitFD.calls) == 1
    transport.close()
