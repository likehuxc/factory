from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from d7_factory_studio.protocols.serial485 import Aa55Frame, Aa55StreamDecoder


class Serial485Error(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Serial485Config:
    port: str
    baudrate: int = 115_200
    read_timeout_s: float = 0.25
    write_timeout_s: float = 1.0

    def __post_init__(self) -> None:
        if not self.port.strip():
            raise ValueError("serial port is required")
        if self.baudrate <= 0:
            raise ValueError("baudrate must be positive")


class Serial485Service:
    """Thread-safe pyserial adapter with verified AA55 frame reception."""

    def __init__(
        self,
        config: Serial485Config,
        *,
        serial_factory: Callable[..., Any] | None = None,
        on_frame: Callable[[str, bytes], None] | None = None,
    ) -> None:
        self.config = config
        self._serial_factory = serial_factory
        self._serial: Any | None = None
        self._decoder = Aa55StreamDecoder()
        self._pending: deque[Aa55Frame] = deque()
        self._lock = threading.RLock()
        self.on_frame = on_frame or (lambda _direction, _frame: None)

    @property
    def is_open(self) -> bool:
        return self._serial is not None and bool(self._serial.is_open)

    def open(self) -> None:
        if self.is_open:
            return
        if self._serial_factory is None:
            import serial

            factory = serial.Serial
            kwargs = {
                "bytesize": serial.EIGHTBITS,
                "parity": serial.PARITY_NONE,
                "stopbits": serial.STOPBITS_ONE,
            }
        else:
            factory = self._serial_factory
            kwargs = {"bytesize": 8, "parity": "N", "stopbits": 1}
        try:
            self._serial = factory(
                port=self.config.port,
                baudrate=self.config.baudrate,
                timeout=self.config.read_timeout_s,
                write_timeout=self.config.write_timeout_s,
                **kwargs,
            )
        except Exception as exc:
            self._serial = None
            raise Serial485Error(f"打开串口 {self.config.port} 失败: {exc}") from exc

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
                self._decoder.reset()
                self._pending.clear()

    def write(self, frame: bytes, *, clear_input: bool = True) -> None:
        serial_port = self._require_open()
        with self._lock:
            try:
                if clear_input:
                    serial_port.reset_input_buffer()
                    self._decoder.reset()
                    self._pending.clear()
                written = serial_port.write(frame)
                serial_port.flush()
            except Exception as exc:
                raise Serial485Error(f"串口发送失败: {exc}") from exc
        if written is not None and written != len(frame):
            raise Serial485Error(f"串口发送不完整: {written}/{len(frame)} bytes")
        self.on_frame("TX", bytes(frame))

    def read_frame(self, timeout_s: float) -> Aa55Frame | None:
        serial_port = self._require_open()
        deadline = time.monotonic() + max(timeout_s, 0)
        with self._lock:
            if self._pending:
                return self._pending.popleft()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                serial_port.timeout = max(0.01, min(0.1, remaining))
                try:
                    chunk = serial_port.read(max(1, int(getattr(serial_port, "in_waiting", 0)) or 1))
                except Exception as exc:
                    raise Serial485Error(f"串口接收失败: {exc}") from exc
                frames = self._decoder.feed(chunk)
                for frame in frames:
                    self.on_frame("RX", frame.raw)
                self._pending.extend(frames)
                if self._pending:
                    return self._pending.popleft()

    def echo(self, request: bytes, *, timeout_s: float = 0.5) -> bool:
        self.write(request)
        response = self.read_frame(timeout_s)
        return response is not None and response.raw == request

    def _require_open(self) -> Any:
        if not self.is_open:
            raise Serial485Error("串口未打开")
        return self._serial
