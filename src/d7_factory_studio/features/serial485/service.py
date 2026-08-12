from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from d7_factory_studio.protocols.serial485 import (
    Aa55Frame,
    Aa55StreamDecoder,
    ParameterReadItem,
    ParameterSpec,
    build_0d_read_multi,
    parse_0d_response,
)


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
            # ServoStudio clears both directions and gives the converter/motor
            # 500 ms to settle before issuing the first protocol request.
            reset_input = getattr(self._serial, "reset_input_buffer", None)
            if callable(reset_input):
                reset_input()
            reset_output = getattr(self._serial, "reset_output_buffer", None)
            if callable(reset_output):
                reset_output()
            self._decoder.reset()
            self._pending.clear()
            time.sleep(0.5)
        except Exception as exc:
            serial_port = self._serial
            if serial_port is not None:
                with suppress(Exception):
                    serial_port.close()
            self._serial = None
            self._decoder.reset()
            self._pending.clear()
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
                try:
                    # Do not reconfigure the Windows COM timeout after TX.  On
                    # the production USB-RS485 adapter that SetCommTimeouts
                    # call races the motor's ~3 ms reply and drops the frame.
                    # Keep the timeout chosen when the port was opened.
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
        response = self.transact(
            request,
            timeout_s=timeout_s,
            matches=lambda frame: frame.raw == request,
        )
        return response is not None

    def read_parameters(
        self,
        station: int,
        parameters: tuple[ParameterSpec, ...] | list[ParameterSpec],
        *,
        timeout_s: float = 0.5,
    ) -> tuple[ParameterReadItem, ...]:
        specs = tuple(parameters)
        request = build_0d_read_multi(station=station, parameters=specs)
        response = self.transact(
            request,
            timeout_s=timeout_s,
            matches=lambda frame: frame.station == station and frame.function in (0x0D, 0x8D),
        )
        if response is None:
            raise Serial485Error(
                f"parameter-read timeout at station 0x{station:02X} after {timeout_s:.3f}s"
            )
        return parse_0d_response(response, expected=specs)

    def transact(
        self,
        request: bytes,
        *,
        timeout_s: float,
        matches: Callable[[Aa55Frame], bool],
    ) -> Aa55Frame | None:
        deadline = time.monotonic() + max(timeout_s, 0)
        deferred: list[Aa55Frame] = []
        with self._lock:
            previously_pending = list(self._pending)
            self._pending.clear()
            try:
                self.write(request)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    frame = self.read_frame(remaining)
                    if frame is None:
                        return None
                    if matches(frame):
                        return frame
                    deferred.append(frame)
            finally:
                self._pending.extendleft(reversed(previously_pending + deferred))

    def _require_open(self) -> Any:
        if not self.is_open:
            raise Serial485Error("串口未打开")
        return self._serial
