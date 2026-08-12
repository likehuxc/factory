from __future__ import annotations

import threading
import time
from collections import deque

import pytest

from d7_factory_studio.core.models import CanFrame
from d7_factory_studio.core.ports import CancellationToken, OperationCancelled
from d7_factory_studio.features.motor.local_can_controller import LocalCanMotorController
from d7_factory_studio.protocols.can_motor import (
    DISABLE_DATA,
    ENABLE_DATA,
    READ_MOTOR_STATE_DATA,
    SET_CONTROL_SOURCE_DATA,
)


def feedback_frame(device_id: int, status: int, position_raw: int) -> CanFrame:
    data = bytearray(32)
    data[7] = device_id
    data[9] = status
    data[15:17] = (0x3000).to_bytes(2, "little")
    data[19:21] = position_raw.to_bytes(2, "little")
    return CanFrame(0x100 + device_id, bytes(data), is_fd=True, bitrate_switch=True)


class ScriptedTransport:
    def __init__(self, positions: dict[int, int], *, confirm_enable: bool = True) -> None:
        self.is_open = True
        self.positions = positions
        self.statuses = {device_id: 0x40 for device_id in positions}
        self.confirm_enable = confirm_enable
        self.frames: list[CanFrame] = []
        self._received: deque[CanFrame] = deque()
        self._lock = threading.Lock()
        self.fail_position = False

    def open(self, _channel: int, _mode: object) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def send(self, frame: CanFrame) -> None:
        with self._lock:
            if self.fail_position and self.is_position(frame):
                raise RuntimeError("injected heartbeat failure")
            self.frames.append(frame)
            if frame.data == READ_MOTOR_STATE_DATA:
                self._received.extend(
                    feedback_frame(device_id, self.statuses[device_id], raw)
                    for device_id, raw in self.positions.items()
                )
            elif self.confirm_enable and self.is_enable(frame):
                self.statuses[frame.data[5]] = 0x42
            elif self.is_position(frame):
                self.positions[frame.data[7]] = int.from_bytes(frame.data[10:12], "little")

    def receive(self, timeout_ms: int = 50) -> list[CanFrame]:
        del timeout_ms
        with self._lock:
            result = list(self._received)
            self._received.clear()
            return result

    @staticmethod
    def is_position(frame: CanFrame) -> bool:
        return len(frame.data) == 12 and frame.data[:7] == bytes.fromhex("40 00 40 28 1C 01 06")

    @staticmethod
    def is_enable(frame: CanFrame) -> bool:
        return len(frame.data) == len(ENABLE_DATA) and frame.data[9:13] == ENABLE_DATA[9:13]

    @staticmethod
    def is_disable(frame: CanFrame) -> bool:
        return len(frame.data) == len(DISABLE_DATA) and frame.data[9:13] == DISABLE_DATA[9:13]


class BlockingTransport(ScriptedTransport):
    def __init__(self, positions: dict[int, int]) -> None:
        super().__init__(positions)
        self.entered_receive = threading.Event()
        self.release_receive = threading.Event()

    def receive(self, timeout_ms: int = 50) -> list[CanFrame]:
        if timeout_ms > 0 and not self.release_receive.is_set():
            self.entered_receive.set()
            self.release_receive.wait(1.0)
        return super().receive(timeout_ms)


def controller(transport: ScriptedTransport, **kwargs: object) -> LocalCanMotorController:
    options: dict[str, object] = {
        "heartbeat_hz": 200.0,
        "startup_delay_s": 0.01,
        "enable_timeout_s": 0.1,
        "safe_stop_hold_s": 0.04,
        "read_timeout_s": 0.1,
    }
    options.update(kwargs)
    return LocalCanMotorController(transport, **options)  # type: ignore[arg-type]


def test_group_enable_uses_fresh_positions_command_id_and_confirms_each_motor() -> None:
    transport = ScriptedTransport({0x21: 0x7000, 0x22: 0x8000})
    target = controller(transport)

    result = target.enable_position_hold([0x21, 0x22], CancellationToken())

    assert result["statuses"] == {0x21: 0x42, 0x22: 0x42}
    assert target.active_device_ids == (0x21, 0x22)
    assert all(frame.arbitration_id == 0x300 for frame in transport.frames)
    first_position = next(
        index for index, frame in enumerate(transport.frames) if transport.is_position(frame)
    )
    first_enable = next(
        index for index, frame in enumerate(transport.frames) if transport.is_enable(frame)
    )
    assert first_position < first_enable
    control_targets = {
        frame.data[5]
        for frame in transport.frames
        if len(frame.data) == len(SET_CONTROL_SOURCE_DATA)
        and frame.data[9:13] == SET_CONTROL_SOURCE_DATA[9:13]
    }
    assert control_targets == {0x21, 0x22}

    target.emergency_stop()


def test_probe_reports_online_and_missing_motors_without_failing_group() -> None:
    transport = ScriptedTransport({0x21: 0x7000, 0x22: 0x8000})
    target = controller(transport)

    result = target.probe([0x21, 0x23], CancellationToken())

    assert set(result["feedback"]) == {0x21}
    assert result["feedback"][0x21].position_raw == 0x7000
    assert result["missing"] == (0x23,)
    assert result["response_ids"] == (0x21, 0x22)
    assert [frame.data for frame in transport.frames] == [READ_MOTOR_STATE_DATA]


def test_probe_lists_broadcast_response_id_even_when_payload_is_too_short_to_decode() -> None:
    class ShortResponseTransport(ScriptedTransport):
        def send(self, frame: CanFrame) -> None:
            super().send(frame)
            if frame.data == READ_MOTOR_STATE_DATA:
                self._received.append(CanFrame(0x121, b"\x01\x02", is_fd=True))

    transport = ShortResponseTransport({})
    target = controller(transport)

    result = target.probe([0x21], CancellationToken())

    assert result["feedback"] == {}
    assert result["missing"] == (0x21,)
    assert result["response_ids"] == (0x21,)


def test_safe_stop_sends_disable_then_keeps_position_heartbeat_for_80ms_window() -> None:
    transport = ScriptedTransport({0x21: 0x7000})
    target = controller(transport, safe_stop_hold_s=0.08)
    target.enable_position_hold([0x21], CancellationToken())
    start = len(transport.frames)

    stopped_at = time.monotonic()
    target.safe_stop([0x21])
    elapsed = time.monotonic() - stopped_at

    stop_frames = transport.frames[start:]
    disable_index = next(
        index for index, frame in enumerate(stop_frames) if transport.is_disable(frame)
    )
    assert elapsed >= 0.075
    assert any(transport.is_position(frame) for frame in stop_frames[disable_index + 1 :])
    assert not target.is_holding
    settled = len(transport.frames)
    time.sleep(0.02)
    assert len(transport.frames) == settled


def test_enable_timeout_stops_heartbeat_and_double_sends_disable() -> None:
    transport = ScriptedTransport({0x21: 0x7000}, confirm_enable=False)
    target = controller(transport)

    with pytest.raises(RuntimeError, match="未确认运行状态 0x42"):
        target.enable_position_hold([0x21], CancellationToken())

    assert not target.is_holding
    assert all(frame.arbitration_id == 0x300 for frame in transport.frames)
    assert all(transport.is_disable(frame) for frame in transport.frames[-2:])


def test_heartbeat_send_failure_immediately_double_disables_and_reports_fault() -> None:
    transport = ScriptedTransport({0x21: 0x7000})
    faults: list[str] = []
    target = controller(transport, on_fault=faults.append)
    target.enable_position_hold([0x21], CancellationToken())

    transport.fail_position = True
    deadline = time.monotonic() + 0.2
    while target.is_holding and time.monotonic() < deadline:
        time.sleep(0.005)

    assert not target.is_holding
    assert faults and "双发失能" in faults[-1]
    assert all(transport.is_disable(frame) for frame in transport.frames[-2:])


def test_velocity_uses_position_trajectory_confirms_feedback_then_disables() -> None:
    transport = ScriptedTransport({0x21: 0x7000})
    target = controller(transport)
    target.enable_position_hold([0x21], CancellationToken())
    start = len(transport.frames)

    result = target.run_velocity(
        [0x21],
        0.5,
        0.08,
        20,
        20,
        CancellationToken(),
    )

    frames = transport.frames[start:]
    position_frames = [frame for frame in frames if transport.is_position(frame)]
    assert len(position_frames) >= 4
    assert not any(frame.data[2] == 0xA1 for frame in frames)
    assert int.from_bytes(position_frames[-1].data[10:12], "little") > 0x7000
    assert any(transport.is_disable(frame) for frame in frames)
    assert result["statuses"] == {0x21: 0x42}
    assert result["safe_state"] == "position_trajectory_then_disabled"
    assert not target.is_holding


def test_velocity_rejects_position_limit_before_publishing_trajectory() -> None:
    transport = ScriptedTransport({0x21: 0xFF00})
    target = controller(transport)
    target.enable_position_hold([0x21], CancellationToken())
    start = len(transport.frames)

    with pytest.raises(ValueError, match="-5..5"):
        target.run_velocity(
            [0x21],
            0.5,
            10.0,
            500,
            500,
            CancellationToken(),
        )

    assert not any(frame.data[2] == 0xA1 for frame in transport.frames[start:])
    assert not target.is_holding


def test_operations_reject_concurrency_but_safe_stop_preempts_blocked_operation() -> None:
    transport = BlockingTransport({0x21: 0x7000})
    target = controller(transport)
    token = CancellationToken()
    errors: list[BaseException] = []

    def read_position() -> None:
        try:
            target.read_positions([0x21], token)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=read_position)
    worker.start()
    assert transport.entered_receive.wait(0.2)

    with pytest.raises(RuntimeError, match="正在执行其他电机操作"):
        target.read_positions([0x21], CancellationToken())
    target.safe_stop([0x21])
    token.cancel()
    transport.release_receive.set()
    worker.join(0.5)

    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], OperationCancelled)
    assert any(transport.is_disable(frame) for frame in transport.frames)
