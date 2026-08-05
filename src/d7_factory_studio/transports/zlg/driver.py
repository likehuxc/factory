from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d7_factory_studio.core.models import CanFrame, CanMode
from d7_factory_studio.core.ports import CanTransport
from d7_factory_studio.transports.zlg.discovery import discover_controlcanfd

STATUS_OK = 1
ZCAN_USBCANFD_200U = 41
TYPE_CAN = 0
TYPE_CANFD = 1


class ZlgTransportError(RuntimeError):
    pass


class ZcanChannelCanInitConfig(ctypes.Structure):
    _fields_ = [
        ("acc_code", ctypes.c_uint),
        ("acc_mask", ctypes.c_uint),
        ("reserved", ctypes.c_uint),
        ("filter", ctypes.c_ubyte),
        ("timing0", ctypes.c_ubyte),
        ("timing1", ctypes.c_ubyte),
        ("mode", ctypes.c_ubyte),
    ]


class ZcanChannelCanfdInitConfig(ctypes.Structure):
    _fields_ = [
        ("acc_code", ctypes.c_uint),
        ("acc_mask", ctypes.c_uint),
        ("abit_timing", ctypes.c_uint),
        ("dbit_timing", ctypes.c_uint),
        ("brp", ctypes.c_uint),
        ("filter", ctypes.c_ubyte),
        ("mode", ctypes.c_ubyte),
        ("pad", ctypes.c_ushort),
        ("reserved", ctypes.c_uint),
    ]


class ZcanChannelInitUnion(ctypes.Union):
    _fields_ = [("can", ZcanChannelCanInitConfig), ("canfd", ZcanChannelCanfdInitConfig)]


class ZcanChannelInitConfig(ctypes.Structure):
    _fields_ = [("can_type", ctypes.c_uint), ("config", ZcanChannelInitUnion)]


class ZcanCanFrame(ctypes.Structure):
    _fields_ = [
        ("can_id", ctypes.c_uint, 29),
        ("err", ctypes.c_uint, 1),
        ("rtr", ctypes.c_uint, 1),
        ("eff", ctypes.c_uint, 1),
        ("can_dlc", ctypes.c_ubyte),
        ("pad", ctypes.c_ubyte),
        ("reserved0", ctypes.c_ubyte),
        ("reserved1", ctypes.c_ubyte),
        ("data", ctypes.c_ubyte * 8),
    ]


class ZcanCanfdFrame(ctypes.Structure):
    _fields_ = [
        ("can_id", ctypes.c_uint, 29),
        ("err", ctypes.c_uint, 1),
        ("rtr", ctypes.c_uint, 1),
        ("eff", ctypes.c_uint, 1),
        ("len", ctypes.c_ubyte),
        ("brs", ctypes.c_ubyte, 1),
        ("esi", ctypes.c_ubyte, 1),
        ("reserved", ctypes.c_ubyte, 6),
        ("reserved0", ctypes.c_ubyte),
        ("reserved1", ctypes.c_ubyte),
        ("data", ctypes.c_ubyte * 64),
    ]


class ZcanTransmitData(ctypes.Structure):
    _fields_ = [("frame", ZcanCanFrame), ("transmit_type", ctypes.c_uint)]


class ZcanReceiveData(ctypes.Structure):
    _fields_ = [("frame", ZcanCanFrame), ("timestamp", ctypes.c_ulonglong)]


class ZcanTransmitFdData(ctypes.Structure):
    _fields_ = [("frame", ZcanCanfdFrame), ("transmit_type", ctypes.c_uint)]


class ZcanReceiveFdData(ctypes.Structure):
    _fields_ = [("frame", ZcanCanfdFrame), ("timestamp", ctypes.c_ulonglong)]


@dataclass(frozen=True, slots=True)
class ZlgTransportConfig:
    dll_path: str | Path | None = None
    device_index: int = 0
    arbitration_baudrate: int = 1_000_000
    data_baudrate: int = 5_000_000
    receive_batch_size: int = 100

    def __post_init__(self) -> None:
        if self.device_index < 0:
            raise ValueError("device_index cannot be negative")
        if self.arbitration_baudrate <= 0 or self.data_baudrate <= 0:
            raise ValueError("CAN bitrates must be positive")
        if not 1 <= self.receive_batch_size <= 10_000:
            raise ValueError("receive_batch_size must be in 1..10000")


class ZlgCanTransport(CanTransport):
    """64-bit ControlCANFD transport for USBCANFD-200U (device type 41)."""

    def __init__(self, config: ZlgTransportConfig | None = None, *, dll: Any | None = None) -> None:
        self.config = config or ZlgTransportConfig()
        self._dll = dll
        self._device_handle: int | None = None
        self._channel_handle: int | None = None
        self._channel = 0
        self._mode = CanMode.FD
        self._lock = threading.RLock()
        self._dll_directory_handle: Any | None = None
        self.dll_path: Path | None = None

    @property
    def is_open(self) -> bool:
        return self._device_handle is not None and self._channel_handle is not None

    def open(self, channel: int, mode: CanMode = CanMode.FD) -> None:
        if self.is_open:
            return
        if channel not in (0, 1):
            raise ZlgTransportError("USBCANFD-200U channel must be 0 or 1")
        if not isinstance(mode, CanMode):
            mode = CanMode(mode)
        self._ensure_dll()
        assert self._dll is not None
        self._resolve_functions()
        device = self._dll.ZCAN_OpenDevice(ZCAN_USBCANFD_200U, self.config.device_index, 0)
        if not device:
            raise ZlgTransportError("ZCAN_OpenDevice failed for USBCANFD-200U type 41")
        try:
            init = self._make_init_config(device, channel, mode)
            channel_handle = self._dll.ZCAN_InitCAN(device, channel, ctypes.byref(init))
            if not channel_handle:
                raise ZlgTransportError(f"ZCAN_InitCAN failed for channel {channel}")
            if self._dll.ZCAN_StartCAN(channel_handle) != STATUS_OK:
                self._dll.ZCAN_ResetCAN(channel_handle)
                raise ZlgTransportError(f"ZCAN_StartCAN failed for channel {channel}")
        except Exception:
            self._dll.ZCAN_CloseDevice(device)
            raise
        self._device_handle = device
        self._channel_handle = channel_handle
        self._channel = channel
        self._mode = mode

    def close(self) -> None:
        with self._lock:
            if self._dll is not None and self._channel_handle is not None:
                self._dll.ZCAN_ResetCAN(self._channel_handle)
            if self._dll is not None and self._device_handle is not None:
                self._dll.ZCAN_CloseDevice(self._device_handle)
            self._channel_handle = None
            self._device_handle = None

    def send(self, frame: CanFrame) -> None:
        with self._lock:
            channel = self._require_channel()
            if frame.is_fd:
                if self._mode is not CanMode.FD:
                    raise ZlgTransportError("CAN FD frame requires an FD channel")
                packet = frame_to_zcan_fd(frame)
                sent = self._dll.ZCAN_TransmitFD(channel, ctypes.byref(packet), 1)
            else:
                packet = frame_to_zcan(frame)
                sent = self._dll.ZCAN_Transmit(channel, ctypes.byref(packet), 1)
            if sent != 1:
                raise ZlgTransportError(f"CAN transmit failed: sent={sent}")

    def receive(self, timeout_ms: int = 50) -> list[CanFrame]:
        with self._lock:
            channel = self._require_channel()
            if self._mode is CanMode.CLASSIC:
                return self._receive_classic(channel, timeout_ms)
            deadline = time.monotonic() + max(timeout_ms, 0) / 1000
            while True:
                frames = self._receive_classic(channel, 0) + self._receive_fd(channel, 0)
                if frames or timeout_ms <= 0 or time.monotonic() >= deadline:
                    return frames
                time.sleep(min(0.001, max(0.0, deadline - time.monotonic())))

    def _receive_classic(self, channel: int, timeout_ms: int) -> list[CanFrame]:
        size = self.config.receive_batch_size
        buffer = (ZcanReceiveData * size)()
        count = self._dll.ZCAN_Receive(channel, ctypes.byref(buffer), size, timeout_ms)
        if count == 0xFFFFFFFF:
            raise ZlgTransportError("ZCAN_Receive failed")
        return [zcan_to_frame(buffer[index]) for index in range(count)]

    def _receive_fd(self, channel: int, timeout_ms: int) -> list[CanFrame]:
        size = self.config.receive_batch_size
        buffer = (ZcanReceiveFdData * size)()
        count = self._dll.ZCAN_ReceiveFD(channel, ctypes.byref(buffer), size, timeout_ms)
        if count == 0xFFFFFFFF:
            raise ZlgTransportError("ZCAN_ReceiveFD failed")
        return [zcan_fd_to_frame(buffer[index]) for index in range(count)]

    def _ensure_dll(self) -> None:
        if self._dll is not None:
            return
        self.dll_path = discover_controlcanfd(explicit_path=self.config.dll_path)
        try:
            if hasattr(os, "add_dll_directory"):
                self._dll_directory_handle = os.add_dll_directory(str(self.dll_path.parent))
            self._dll = ctypes.WinDLL(str(self.dll_path))
        except OSError as exc:
            raise ZlgTransportError(f"failed to load {self.dll_path}: {exc}") from exc

    def _resolve_functions(self) -> None:
        required = (
            "ZCAN_OpenDevice",
            "ZCAN_CloseDevice",
            "ZCAN_InitCAN",
            "ZCAN_StartCAN",
            "ZCAN_ResetCAN",
            "ZCAN_Transmit",
            "ZCAN_Receive",
            "ZCAN_TransmitFD",
            "ZCAN_ReceiveFD",
        )
        missing = [name for name in required if not hasattr(self._dll, name)]
        if missing:
            raise ZlgTransportError(f"ControlCANFD.dll missing exports: {', '.join(missing)}")
        self._dll.ZCAN_OpenDevice.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        self._dll.ZCAN_OpenDevice.restype = ctypes.c_void_p
        self._dll.ZCAN_CloseDevice.argtypes = [ctypes.c_void_p]
        self._dll.ZCAN_CloseDevice.restype = ctypes.c_uint
        self._dll.ZCAN_InitCAN.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
        self._dll.ZCAN_InitCAN.restype = ctypes.c_void_p
        for name in ("ZCAN_StartCAN", "ZCAN_ResetCAN"):
            function = getattr(self._dll, name)
            function.argtypes = [ctypes.c_void_p]
            function.restype = ctypes.c_uint
        for name in ("ZCAN_Transmit", "ZCAN_TransmitFD"):
            function = getattr(self._dll, name)
            function.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
            function.restype = ctypes.c_uint
        for name in ("ZCAN_Receive", "ZCAN_ReceiveFD"):
            function = getattr(self._dll, name)
            function.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
            function.restype = ctypes.c_uint
        if hasattr(self._dll, "ZCAN_SetValue"):
            self._dll.ZCAN_SetValue.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
            self._dll.ZCAN_SetValue.restype = ctypes.c_uint
        for name in ("ZCAN_SetAbitBaud", "ZCAN_SetDbitBaud"):
            if hasattr(self._dll, name):
                function = getattr(self._dll, name)
                function.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
                function.restype = ctypes.c_uint

    def _make_init_config(self, device: int, channel: int, mode: CanMode) -> ZcanChannelInitConfig:
        init = ZcanChannelInitConfig()
        if mode is CanMode.FD:
            self._set_fd_baudrates(device, channel)
            init.can_type = TYPE_CANFD
            init.config.canfd.acc_code = 0
            init.config.canfd.acc_mask = 0xFFFFFFFF
            init.config.canfd.filter = 0
            init.config.canfd.mode = 0
        else:
            timing0, timing1 = timing_for_baudrate(self.config.arbitration_baudrate)
            init.can_type = TYPE_CAN
            init.config.can.acc_code = 0
            init.config.can.acc_mask = 0xFFFFFFFF
            init.config.can.filter = 0
            init.config.can.timing0 = timing0
            init.config.can.timing1 = timing1
            init.config.can.mode = 0
        return init

    def _set_fd_baudrates(self, device: int, channel: int) -> None:
        if hasattr(self._dll, "ZCAN_SetAbitBaud") and hasattr(self._dll, "ZCAN_SetDbitBaud"):
            if self._dll.ZCAN_SetAbitBaud(device, channel, self.config.arbitration_baudrate) != STATUS_OK:
                raise ZlgTransportError("failed to set CAN FD arbitration bitrate")
            if self._dll.ZCAN_SetDbitBaud(device, channel, self.config.data_baudrate) != STATUS_OK:
                raise ZlgTransportError("failed to set CAN FD data bitrate")
            return
        if hasattr(self._dll, "ZCAN_SetValue"):
            settings = (
                (f"{channel}/canfd_abit_baud_rate", self.config.arbitration_baudrate),
                (f"{channel}/canfd_dbit_baud_rate", self.config.data_baudrate),
            )
            for path, value in settings:
                result = self._dll.ZCAN_SetValue(device, path.encode(), str(value).encode())
                if result != STATUS_OK:
                    raise ZlgTransportError(f"failed to set {path}={value}")
            return
        raise ZlgTransportError("ControlCANFD.dll has no supported CAN FD bitrate API")

    def _require_channel(self) -> int:
        if not self.is_open or self._channel_handle is None:
            raise ZlgTransportError("CAN transport is not open")
        return self._channel_handle


def timing_for_baudrate(baudrate: int) -> tuple[int, int]:
    timing = {
        1_000_000: (0x00, 0x14),
        500_000: (0x00, 0x1C),
        250_000: (0x01, 0x1C),
        125_000: (0x03, 0x1C),
        100_000: (0x04, 0x1C),
    }
    try:
        return timing[baudrate]
    except KeyError as exc:
        raise ZlgTransportError(f"unsupported classic CAN bitrate: {baudrate}") from exc


def frame_to_zcan(frame: CanFrame) -> ZcanTransmitData:
    if frame.is_fd:
        raise ValueError("expected a Classic CAN frame")
    packet = ZcanTransmitData()
    packet.frame.can_id = frame.arbitration_id
    packet.frame.eff = int(frame.is_extended)
    packet.frame.can_dlc = len(frame.data)
    for index, value in enumerate(frame.data):
        packet.frame.data[index] = value
    return packet


def frame_to_zcan_fd(frame: CanFrame) -> ZcanTransmitFdData:
    if not frame.is_fd:
        raise ValueError("expected a CAN FD frame")
    packet = ZcanTransmitFdData()
    packet.frame.can_id = frame.arbitration_id
    packet.frame.eff = int(frame.is_extended)
    packet.frame.len = len(frame.data)
    packet.frame.brs = int(frame.bitrate_switch)
    for index, value in enumerate(frame.data):
        packet.frame.data[index] = value
    return packet


def zcan_to_frame(packet: ZcanReceiveData) -> CanFrame:
    return CanFrame(
        packet.frame.can_id,
        bytes(packet.frame.data[: packet.frame.can_dlc]),
        is_extended=bool(packet.frame.eff),
        is_fd=False,
        bitrate_switch=False,
        timestamp=float(packet.timestamp),
    )


def zcan_fd_to_frame(packet: ZcanReceiveFdData) -> CanFrame:
    return CanFrame(
        packet.frame.can_id,
        bytes(packet.frame.data[: packet.frame.len]),
        is_extended=bool(packet.frame.eff),
        is_fd=True,
        bitrate_switch=bool(packet.frame.brs),
        timestamp=float(packet.timestamp),
    )
