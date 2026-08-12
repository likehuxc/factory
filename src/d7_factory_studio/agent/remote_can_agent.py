#!/usr/bin/env python3
"""Session-scoped SocketCAN bridge for D7 Factory Studio.

The bridge intentionally has no actuator_sdk dependency. Motor protocol and
safety sequencing stay in the desktop application; this process only moves
validated CAN frames between the SSH JSONL session and Linux SocketCAN.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import select
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROTOCOL = "d7-factory-can-agent-jsonl"
MAX_LINE_BYTES = 1024 * 1024
CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x000007FF
CANFD_BRS = 0x01
SOL_CAN_RAW = getattr(socket, "SOL_CAN_RAW", 101)
CAN_RAW_FD_FRAMES = getattr(socket, "CAN_RAW_FD_FRAMES", 5)
CLASSIC_FRAME = struct.Struct("=IB3x8s")
FD_FRAME = struct.Struct("=IBB2x64s")
BUS_PATTERN = re.compile(r"can[0-9]+\Z")


class BridgeError(RuntimeError):
    pass


class SocketCanBridge:
    def __init__(self, config_path: Path, deadman_timeout_s: float = 1.5) -> None:
        self._configured_motors = self._read_motor_inventory(config_path)
        self._deadman_timeout_s = deadman_timeout_s
        self._sockets: dict[str, socket.socket] = {}
        self._socket_buses: dict[socket.socket, str] = {}
        self._subscribed_buses: set[str] = set()
        self._touched_motors: set[tuple[str, int]] = set()
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._last_heartbeat = time.monotonic()
        self._deadman_triggered = False
        self._receiver = threading.Thread(target=self._receive_loop, name="can-rx", daemon=True)
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="deadman", daemon=True)

    def run(self) -> int:
        self._receiver.start()
        self._watchdog.start()
        try:
            for raw_line in sys.stdin.buffer:
                if len(raw_line) > MAX_LINE_BYTES:
                    self._emit_error("", "request_too_large", "request line exceeds 1 MiB")
                    continue
                try:
                    request = json.loads(raw_line.decode("utf-8"))
                    self._handle(request)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._emit_error("", "invalid_json", f"invalid JSON request: {exc}")
                except BaseException as exc:
                    identifier = ""
                    if isinstance(locals().get("request"), dict):
                        identifier = str(request.get("id", ""))
                    self._emit_error(identifier, "operation_failed", str(exc))
                if self._stop.is_set():
                    break
        finally:
            self._safe_disable_all()
            self._stop.set()
            self._close_sockets()
        return 0

    def _handle(self, request: Any) -> None:
        if not isinstance(request, dict) or request.get("v") != 1:
            raise BridgeError("unsupported or missing protocol version")
        identifier = str(request.get("id", ""))
        operation = str(request.get("op", ""))
        if not identifier or not operation:
            raise BridgeError("request requires id and op")
        args = request.get("args") or {}
        if not isinstance(args, dict):
            raise BridgeError("args must be an object")

        if operation == "hello":
            self._emit_result(
                identifier,
                {
                    "protocol": PROTOCOL,
                    "transport": "socketcan",
                    "sdk_dependency": False,
                    "pid": os.getpid(),
                },
            )
            return
        if operation == "heartbeat":
            self._last_heartbeat = time.monotonic()
            self._deadman_triggered = False
            self._emit_result(identifier, {"alive": True})
            return
        if operation == "can.subscribe":
            bus = self._validate_bus(args.get("bus"))
            self._open_bus(bus)
            with self._lock:
                self._subscribed_buses.add(bus)
            self._emit_result(identifier, {"bus": bus, "subscribed": True})
            return
        if operation == "can.unsubscribe":
            bus = self._validate_bus(args.get("bus"))
            with self._lock:
                self._subscribed_buses.discard(bus)
            self._emit_result(identifier, {"bus": bus, "subscribed": False})
            return
        if operation == "can.send":
            self._send_request(args)
            self._emit_result(identifier, {"sent": True})
            return
        if operation == "can.send_batch":
            if args.get("unsafe") is not True:
                raise BridgeError("raw CAN batch send requires unsafe=true")
            frames = args.get("frames")
            if not isinstance(frames, list) or not 1 <= len(frames) <= 128:
                raise BridgeError("CAN batch must contain 1..128 frames")
            for frame_args in frames:
                if not isinstance(frame_args, dict):
                    raise BridgeError("CAN batch frame must be an object")
                self._send_request({**frame_args, "unsafe": True})
            self._emit_result(identifier, {"sent": len(frames)})
            return
        if operation in {"state.subscribe", "state.unsubscribe"}:
            self._emit_result(identifier, {"supported": False})
            return
        if operation == "cancel":
            self._emit_result(identifier, {"cancelled": False})
            return
        if operation == "shutdown":
            self._safe_disable_all()
            self._emit_result(identifier, {"stopped": True})
            self._stop.set()
            return
        raise BridgeError(f"unsupported operation: {operation}")

    def _send_request(self, args: dict[str, Any]) -> None:
        if args.get("unsafe") is not True:
            raise BridgeError("raw CAN send requires unsafe=true")
        bus = self._validate_bus(args.get("bus"))
        arbitration_id = int(args.get("id", -1))
        is_extended = bool(args.get("is_extended", False))
        limit = CAN_EFF_MASK if is_extended else CAN_SFF_MASK
        if not 0 <= arbitration_id <= limit:
            raise BridgeError("CAN arbitration ID is out of range")
        raw_data = args.get("data")
        if not isinstance(raw_data, list):
            raise BridgeError("CAN data must be an array")
        try:
            data = bytes(int(value) for value in raw_data)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BridgeError("CAN data contains an invalid byte") from exc
        is_fd = bool(args.get("is_fd", True))
        if len(data) > (64 if is_fd else 8):
            raise BridgeError("CAN payload is too long")
        can_id = arbitration_id | (CAN_EFF_FLAG if is_extended else 0)
        frame = (
            FD_FRAME.pack(can_id, len(data), CANFD_BRS if args.get("bitrate_switch", is_fd) else 0, data.ljust(64, b"\0"))
            if is_fd
            else CLASSIC_FRAME.pack(can_id, len(data), data.ljust(8, b"\0"))
        )
        sock = self._open_bus(bus)
        try:
            sock.send(frame)
        except OSError as exc:
            raise BridgeError(f"{bus} send failed: {exc}") from exc
        self._track_motor_command(bus, arbitration_id, data)

    def _open_bus(self, bus: str) -> socket.socket:
        with self._lock:
            existing = self._sockets.get(bus)
            if existing is not None:
                return existing
            try:
                socket.if_nametoindex(bus)
                sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
                sock.setsockopt(SOL_CAN_RAW, CAN_RAW_FD_FRAMES, 1)
                sock.setblocking(False)
                sock.bind((bus,))
            except (AttributeError, OSError) as exc:
                candidate = locals().get("sock")
                if candidate is not None:
                    with contextlib.suppress(OSError):
                        candidate.close()
                raise BridgeError(f"cannot open {bus}: {exc}") from exc
            self._sockets[bus] = sock
            self._socket_buses[sock] = bus
            return sock

    def _receive_loop(self) -> None:
        while not self._stop.wait(0.01):
            with self._lock:
                sockets = tuple(self._socket_buses)
            if not sockets:
                continue
            try:
                ready, _writable, _errors = select.select(sockets, (), (), 0.05)
            except (OSError, ValueError):
                continue
            for sock in ready:
                try:
                    payload = sock.recv(FD_FRAME.size)
                    frame = self._decode_frame(payload)
                    if frame is None:
                        continue
                    with self._lock:
                        bus = self._socket_buses.get(sock)
                        subscribed = bus in self._subscribed_buses
                    if bus and subscribed:
                        frame.update(v=1, type="can.frame", bus=bus, mono_ms=round(time.monotonic() * 1000))
                        self._emit(frame)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    continue

    @staticmethod
    def _decode_frame(payload: bytes) -> dict[str, Any] | None:
        if len(payload) == FD_FRAME.size:
            can_id, length, flags, data = FD_FRAME.unpack(payload)
            is_fd = True
            bitrate_switch = bool(flags & CANFD_BRS)
        elif len(payload) == CLASSIC_FRAME.size:
            can_id, length, data = CLASSIC_FRAME.unpack(payload)
            is_fd = False
            bitrate_switch = False
        else:
            return None
        is_extended = bool(can_id & CAN_EFF_FLAG)
        arbitration_id = can_id & (CAN_EFF_MASK if is_extended else CAN_SFF_MASK)
        return {
            "id": arbitration_id,
            "is_extended": is_extended,
            "is_fd": is_fd,
            "bitrate_switch": bitrate_switch,
            "data": list(data[:length]),
        }

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(0.1):
            if time.monotonic() - self._last_heartbeat <= self._deadman_timeout_s:
                continue
            if self._deadman_triggered:
                continue
            self._safe_disable_all()
            self._emit({"v": 1, "type": "safety", "reason": "desktop heartbeat timeout"})
            # A transient desktop stall must leave motors disabled, but it must
            # not permanently destroy the CAN bridge. The next heartbeat clears
            # the latch; stdin EOF or an explicit shutdown still exits normally.
            self._deadman_triggered = True

    def _safe_disable_all(self) -> None:
        with self._lock:
            targets = tuple(self._touched_motors)
            self._touched_motors.clear()
        for _attempt in range(2):
            for bus, device_id in targets:
                try:
                    data = bytearray.fromhex("40 40 00 19 24 01 00 01 10 01 10 20 00 00 00 00")
                    data[5] = device_id
                    frame = FD_FRAME.pack(0x300, len(data), CANFD_BRS, bytes(data).ljust(64, b"\0"))
                    self._open_bus(bus).send(frame)
                except BaseException:
                    continue

    def _track_motor_command(self, bus: str, arbitration_id: int, data: bytes) -> None:
        if arbitration_id != 0x300:
            return
        touched: set[int] = set()
        if len(data) >= 16 and data[:5] == bytes.fromhex("40 40 00 19 24"):
            parameter = data[9:12]
            if parameter in {bytes.fromhex("01 02 20"), bytes.fromhex("01 10 20")}:
                touched.add(data[5])
        elif len(data) >= 12 and data[:7] == bytes.fromhex("40 00 40 28 1C 01 06"):
            touched.add(data[7])
        elif len(data) >= 5 and data[:3] == bytes.fromhex("40 00 A1"):
            count = data[4]
            for index in range(min(count, 7)):
                offset = 5 + index * 7
                if offset + 7 <= len(data) and data[offset] == 0x07:
                    touched.add(data[offset + 1])
        if touched:
            known = set(self._configured_motors)
            with self._lock:
                for device_id in touched:
                    target = (bus, device_id)
                    if not known or target in known:
                        self._touched_motors.add(target)

    def _close_sockets(self) -> None:
        with self._lock:
            sockets = tuple(self._sockets.values())
            self._sockets.clear()
            self._socket_buses.clear()
            self._subscribed_buses.clear()
            self._touched_motors.clear()
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.close()

    @staticmethod
    def _read_motor_inventory(config_path: Path) -> tuple[tuple[str, int], ...]:
        try:
            lines = config_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ()
        motors: list[tuple[str, int]] = []
        device_id: int | None = None
        bus: str | None = None
        for line in [*lines, "- logic_id: end"]:
            stripped = line.strip()
            if stripped.startswith("- logic_id:"):
                if device_id is not None and bus is not None:
                    motors.append((bus, device_id))
                device_id = None
                bus = None
            elif stripped.startswith("device_id:"):
                try:
                    device_id = int(stripped.split(":", 1)[1].strip(), 0)
                except ValueError:
                    device_id = None
            elif stripped.startswith("bus:"):
                candidate = stripped.split(":", 1)[1].strip()
                bus = candidate if BUS_PATTERN.fullmatch(candidate) else None
        return tuple(dict.fromkeys(motors))

    @staticmethod
    def _validate_bus(value: Any) -> str:
        bus = str(value or "").strip().lower()
        if not BUS_PATTERN.fullmatch(bus):
            raise BridgeError("bus must use canN format")
        return bus

    def _emit_result(self, identifier: str, data: dict[str, Any]) -> None:
        self._emit({"v": 1, "type": "result", "id": identifier, "ok": True, "data": data})

    def _emit_error(self, identifier: str, code: str, message: str) -> None:
        self._emit(
            {
                "v": 1,
                "type": "result",
                "id": identifier,
                "ok": False,
                "error": {"code": code, "message": message},
            }
        )

    def _emit(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    return SocketCanBridge(args.config).run()


if __name__ == "__main__":
    raise SystemExit(main())
