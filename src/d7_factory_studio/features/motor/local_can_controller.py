from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager, suppress
from typing import Any

from d7_factory_studio.core.ports import CancellationToken, CanTransport
from d7_factory_studio.protocols.can_motor import (
    DISABLE_DATA,
    ENABLE_DATA,
    READ_MOTOR_STATE_DATA,
    broadcast_response_device_id,
    decode_motor_feedback,
    disable_frame,
    enable_frame,
    fault_reset_frame,
    position_command_frame,
    read_motor_state_frame,
    set_control_source_frame,
)


class LocalCanMotorController:
    """Safe state machine for direct CAN-box motor control."""

    COMMAND_ID = 0x300

    def __init__(
        self,
        transport: CanTransport,
        *,
        heartbeat_hz: float = 50.0,
        startup_delay_s: float = 0.1,
        enable_timeout_s: float = 2.0,
        safe_stop_hold_s: float = 0.08,
        read_timeout_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        on_fault: Callable[[str], None] | None = None,
    ) -> None:
        if heartbeat_hz <= 0:
            raise ValueError("位置心跳频率必须大于 0")
        self.transport = transport
        self._heartbeat_interval_s = 1.0 / heartbeat_hz
        self._startup_delay_s = startup_delay_s
        self._enable_timeout_s = enable_timeout_s
        self._safe_stop_hold_s = safe_stop_hold_s
        self._read_timeout_s = read_timeout_s
        self._clock = clock
        self._on_fault = on_fault
        self._operation_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_started: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_positions: dict[int, float] = {}
        self._heartbeat_error: BaseException | None = None
        self._generation = 0
        self._stopping = False

    @property
    def is_holding(self) -> bool:
        with self._state_lock:
            return bool(self._heartbeat_positions) and self._heartbeat_alive_locked()

    @property
    def active_device_ids(self) -> tuple[int, ...]:
        with self._state_lock:
            return tuple(sorted(self._heartbeat_positions))

    def enable_position_hold(
        self,
        device_ids: Iterable[int],
        token: CancellationToken,
        report: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        ids = self._normalize_ids(device_ids)
        with self._exclusive_operation():
            with self._state_lock:
                self._raise_heartbeat_error_locked()
                if self._stopping:
                    raise RuntimeError("CAN 电机正在安全停止")
                if self._heartbeat_alive_locked() or self._heartbeat_positions:
                    raise RuntimeError("位置心跳已在运行，请先失能后再重新使能")
                self._generation += 1
                generation = self._generation
            try:
                self._emit(report, 5, "正在读取最新电机位置")
                feedback = self._read_feedback(ids, token, require_running=False)
                positions = {device_id: item.position_rad for device_id, item in feedback.items()}
                self._emit(
                    report,
                    15,
                    "最新反馈："
                    + "; ".join(self._format_feedback(item) for item in feedback.values()),
                )
                token.raise_if_cancelled()
                self._check_generation(generation)

                self._send_many(
                    frame
                    for device_id in ids
                    for frame in (
                        set_control_source_frame(device_id, self.COMMAND_ID),
                        disable_frame(device_id, self.COMMAND_ID),
                        fault_reset_frame(device_id, self.COMMAND_ID),
                    )
                )
                self._emit(report, 30, "控制源、失能和故障复位已发送")

                self._start_heartbeat(positions, generation)
                self._wait_for_heartbeat_start(generation, token)
                self._wait_with_checks(self._startup_delay_s, generation, token)
                self._send_many(
                    enable_frame(device_id, self.COMMAND_ID) for device_id in ids
                )
                self._emit(report, 55, "已使能，正在确认运行状态 0x42")

                statuses = self._confirm_running(ids, generation, token, report)
                self._emit(report, 100, "全部电机已确认运行状态 0x42")
                return {
                    "device_ids": list(ids),
                    "positions": positions,
                    "statuses": statuses,
                    "heartbeat_hz": round(1.0 / self._heartbeat_interval_s, 3),
                }
            except BaseException:
                self._emergency_stop_noexcept(ids)
                raise

    def read_positions(
        self,
        device_ids: Iterable[int],
        token: CancellationToken,
    ) -> dict[int, float]:
        ids = self._normalize_ids(device_ids)
        with self._exclusive_operation():
            feedback = self._read_feedback(ids, token, require_running=False)
            return {device_id: item.position_rad for device_id, item in feedback.items()}

    def probe(
        self,
        device_ids: Iterable[int],
        token: CancellationToken,
    ) -> dict[str, Any]:
        """Return partial online results instead of failing the whole selected group."""
        ids = self._normalize_ids(device_ids)
        with self._exclusive_operation():
            self.transport.receive(0)
            pending = set(ids)
            feedback: dict[int, Any] = {}
            response_ids: set[int] = set()
            deadline = self._clock() + self._read_timeout_s
            next_request = 0.0
            while pending and self._clock() < deadline:
                token.raise_if_cancelled()
                now = self._clock()
                if now >= next_request:
                    # This payload is a bus broadcast. Repeating it inside the
                    # scan window makes discovery resilient to a dropped frame.
                    self.transport.send(read_motor_state_frame(0, self.COMMAND_ID))
                    next_request = now + 0.2
                for frame in self.transport.receive(20):
                    response_id = broadcast_response_device_id(frame)
                    if response_id is not None:
                        response_ids.add(response_id)
                    for device_id in tuple(pending):
                        item = decode_motor_feedback(frame, device_id)
                        if item is None:
                            continue
                        feedback[device_id] = item
                        pending.remove(device_id)
            return {
                "feedback": feedback,
                "missing": tuple(sorted(pending)),
                "response_ids": tuple(sorted(response_ids)),
            }

    def set_positions(self, positions: dict[int, float], token: CancellationToken) -> None:
        ids = self._normalize_ids(positions)
        with self._exclusive_operation():
            token.raise_if_cancelled()
            with self._state_lock:
                self._raise_heartbeat_error_locked()
                if not self._heartbeat_alive_locked():
                    raise RuntimeError("位置心跳未运行，请先执行使能")
                active = set(self._heartbeat_positions)
                if not set(ids).issubset(active):
                    raise RuntimeError("目标电机与当前位置心跳目标不一致")
                for device_id in ids:
                    # Validate before publishing the new target to the heartbeat thread.
                    position_command_frame(device_id, float(positions[device_id]), self.COMMAND_ID)
                self._heartbeat_positions.update(
                    {device_id: float(positions[device_id]) for device_id in ids}
                )

    def move_relative(
        self,
        device_ids: Iterable[int],
        delta_rad: float,
        token: CancellationToken,
    ) -> dict[int, float]:
        ids = self._normalize_ids(device_ids)
        with self._exclusive_operation():
            feedback = self._read_feedback(ids, token, require_running=False)
            positions = {
                device_id: item.position_rad + float(delta_rad)
                for device_id, item in feedback.items()
            }
            with self._state_lock:
                self._raise_heartbeat_error_locked()
                if not self._heartbeat_alive_locked() or not set(ids).issubset(
                    self._heartbeat_positions
                ):
                    raise RuntimeError("位置心跳未覆盖目标电机，请先执行使能")
                for device_id, position in positions.items():
                    position_command_frame(device_id, position, self.COMMAND_ID)
                self._heartbeat_positions.update(positions)
            return positions

    def take_control(self, device_ids: Iterable[int], token: CancellationToken) -> None:
        self._send_service_frames(device_ids, token, set_control_source_frame)

    def clear_errors(self, device_ids: Iterable[int], token: CancellationToken) -> None:
        self._send_service_frames(device_ids, token, fault_reset_frame)

    def run_velocity(
        self,
        device_ids: Iterable[int],
        velocity_rad_s: float,
        duration_s: float,
        accel_time_ms: int,
        decel_time_ms: int,
        token: CancellationToken,
    ) -> dict[str, Any]:
        ids = self._normalize_ids(device_ids)
        if duration_s <= 0:
            raise ValueError("CAN 盒速度运动必须设置大于 0 的运动时间")
        with self._exclusive_operation():
            with self._state_lock:
                self._raise_heartbeat_error_locked()
                if not self._heartbeat_alive_locked() or not set(ids).issubset(
                    self._heartbeat_positions
                ):
                    raise RuntimeError("速度运动前必须先完成 CAN 盒使能")
                generation = self._generation
            try:
                # actuator_sdk leaves ServoMotor::set_velocity() as a no-op. Keep
                # the proven position heartbeat alive and integrate a bounded
                # trapezoidal velocity profile into successive position targets.
                feedback = self._read_feedback(ids, token, require_running=True)
                start_positions = {
                    device_id: item.position_rad for device_id, item in feedback.items()
                }
                accel_s, cruise_s, decel_s = self._velocity_segments(
                    duration_s,
                    accel_time_ms / 1000.0,
                    decel_time_ms / 1000.0,
                )
                displacement = self._velocity_displacement(
                    duration_s,
                    float(velocity_rad_s),
                    accel_s,
                    cruise_s,
                    decel_s,
                )
                final_targets = {
                    device_id: position + displacement
                    for device_id, position in start_positions.items()
                }
                for device_id, position in final_targets.items():
                    position_command_frame(device_id, position, self.COMMAND_ID)

                with self._state_lock:
                    self._heartbeat_positions.update(start_positions)

                started = self._clock()
                while True:
                    token.raise_if_cancelled()
                    self._check_generation(generation)
                    self._raise_heartbeat_error()
                    elapsed = min(duration_s, self._clock() - started)
                    offset = self._velocity_displacement(
                        elapsed,
                        float(velocity_rad_s),
                        accel_s,
                        cruise_s,
                        decel_s,
                    )
                    with self._state_lock:
                        self._heartbeat_positions.update(
                            {
                                device_id: position + offset
                                for device_id, position in start_positions.items()
                            }
                        )
                    if elapsed >= duration_s:
                        break
                    time.sleep(
                        min(
                            self._heartbeat_interval_s,
                            max(0.0, duration_s - elapsed),
                        )
                    )

                self._wait_with_checks(
                    max(self._heartbeat_interval_s * 2, 0.05), generation, token
                )
                final_feedback = self._read_feedback(ids, token, require_running=True)
                self.safe_stop(ids)
                return {
                    "device_ids": list(ids),
                    "rad_s": float(velocity_rad_s),
                    "duration_s": float(duration_s),
                    "target_positions": final_targets,
                    "positions": {
                        device_id: item.position_rad
                        for device_id, item in final_feedback.items()
                    },
                    "statuses": {
                        device_id: item.status for device_id, item in final_feedback.items()
                    },
                    "safe_state": "position_trajectory_then_disabled",
                }
            except BaseException:
                self._emergency_stop_noexcept(ids)
                raise

    @staticmethod
    def _velocity_segments(
        duration_s: float,
        accel_s: float,
        decel_s: float,
    ) -> tuple[float, float, float]:
        accel = max(0.0, float(accel_s))
        decel = max(0.0, float(decel_s))
        ramp_total = accel + decel
        if ramp_total > duration_s and ramp_total > 0:
            scale = duration_s / ramp_total
            accel *= scale
            decel *= scale
        return accel, max(0.0, duration_s - accel - decel), decel

    @staticmethod
    def _velocity_displacement(
        elapsed_s: float,
        velocity_rad_s: float,
        accel_s: float,
        cruise_s: float,
        decel_s: float,
    ) -> float:
        elapsed = max(0.0, float(elapsed_s))
        velocity = float(velocity_rad_s)
        if accel_s > 0 and elapsed < accel_s:
            return 0.5 * velocity * elapsed * elapsed / accel_s

        distance = 0.5 * velocity * accel_s
        elapsed -= accel_s
        cruise_elapsed = min(elapsed, cruise_s)
        distance += velocity * cruise_elapsed
        elapsed -= cruise_elapsed
        if elapsed <= 0 or decel_s <= 0:
            return distance
        decel_elapsed = min(elapsed, decel_s)
        return distance + velocity * (
            decel_elapsed - 0.5 * decel_elapsed * decel_elapsed / decel_s
        )

    def safe_stop(self, device_ids: Iterable[int]) -> None:
        ids = self._stop_targets(device_ids)
        with self._state_lock:
            if self._stopping:
                raise RuntimeError("CAN 电机已经在安全停止")
            self._stopping = True
            generation = self._generation
        first_error: BaseException | None = None
        try:
            for device_id in ids:
                try:
                    self.transport.send(disable_frame(device_id, self.COMMAND_ID))
                except BaseException as exc:
                    first_error = first_error or exc
            deadline = self._clock() + self._safe_stop_hold_s
            while self._clock() < deadline:
                with self._state_lock:
                    if self._heartbeat_error is not None:
                        first_error = first_error or self._heartbeat_error
                        break
                time.sleep(min(0.01, max(0.0, deadline - self._clock())))
        finally:
            with self._state_lock:
                if self._generation == generation:
                    self._generation += 1
            self._stop_heartbeat(clear_positions=True)
            with self._state_lock:
                if self._generation in {generation, generation + 1}:
                    self._stopping = False
        if first_error is not None:
            self._emergency_stop_noexcept(ids)
            raise RuntimeError(f"CAN 电机安全停止失败: {first_error}") from first_error

    def emergency_stop(self, device_ids: Iterable[int] = ()) -> None:
        ids = self._stop_targets(device_ids)
        with self._state_lock:
            self._generation += 1
            generation = self._generation
            self._stopping = True
        self._stop_heartbeat(clear_positions=True)
        first_error: BaseException | None = None
        try:
            for _attempt in range(2):
                for device_id in ids:
                    try:
                        self.transport.send(disable_frame(device_id, self.COMMAND_ID))
                    except BaseException as exc:
                        first_error = first_error or exc
        finally:
            with self._state_lock:
                if self._generation == generation:
                    self._stopping = False
        if first_error is not None:
            raise RuntimeError(f"CAN 电机紧急失能发送失败: {first_error}") from first_error

    def shutdown(self) -> None:
        ids = self.active_device_ids
        if ids:
            self._emergency_stop_noexcept(ids)
        else:
            with self._state_lock:
                self._generation += 1
            self._stop_heartbeat(clear_positions=True)

    def _read_feedback(
        self,
        ids: tuple[int, ...],
        token: CancellationToken,
        *,
        require_running: bool,
    ) -> dict[int, Any]:
        # Drop queued samples before requesting a new position so startup cannot use stale data.
        self.transport.receive(0)
        for device_id in ids:
            self.transport.send(read_motor_state_frame(device_id, self.COMMAND_ID))
        pending = set(ids)
        feedback: dict[int, Any] = {}
        deadline = self._clock() + self._read_timeout_s
        while pending and self._clock() < deadline:
            token.raise_if_cancelled()
            frames = self.transport.receive(20)
            token.raise_if_cancelled()
            for frame in frames:
                for device_id in tuple(pending):
                    item = decode_motor_feedback(frame, device_id)
                    if item is None or (require_running and item.status != 0x42):
                        continue
                    feedback[device_id] = item
                    pending.remove(device_id)
        if pending:
            missing = ", ".join(f"0x{device_id:02X}" for device_id in sorted(pending))
            condition = "运行状态 0x42" if require_running else "最新位置反馈"
            raise RuntimeError(f"等待{condition}超时: {missing}")
        return feedback

    def _confirm_running(
        self,
        ids: tuple[int, ...],
        generation: int,
        token: CancellationToken,
        report: Callable[[int, str], None] | None = None,
    ) -> dict[int, int]:
        pending = set(ids)
        statuses: dict[int, int] = {}
        latest: dict[int, Any] = {}
        deadline = self._clock() + self._enable_timeout_s
        next_request = 0.0
        while pending and self._clock() < deadline:
            token.raise_if_cancelled()
            self._check_generation(generation)
            self._raise_heartbeat_error()
            now = self._clock()
            if now >= next_request:
                for device_id in pending:
                    self.transport.send(read_motor_state_frame(device_id, self.COMMAND_ID))
                next_request = now + 0.2
            frames = self.transport.receive(20)
            token.raise_if_cancelled()
            self._check_generation(generation)
            for frame in frames:
                for device_id in tuple(pending):
                    item = decode_motor_feedback(frame, device_id)
                    if item is None:
                        continue
                    latest[device_id] = item
                    statuses[device_id] = item.status
                    if item.status == 0x42:
                        pending.remove(device_id)
        if pending:
            missing = ", ".join(f"0x{device_id:02X}" for device_id in sorted(pending))
            detail = "; ".join(
                self._format_feedback(latest[device_id])
                for device_id in sorted(pending)
                if device_id in latest
            )
            if detail:
                self._emit(report, 90, f"未进入 0x42 的最新反馈：{detail}")
            suffix = f"；最新反馈：{detail}" if detail else "；未收到有效状态回包"
            raise RuntimeError(f"使能后 2 秒内未确认运行状态 0x42: {missing}{suffix}")
        return statuses

    @staticmethod
    def _format_feedback(item: Any) -> str:
        alarms = bytes(item.alarm_bytes).hex(" ").upper() or "无"
        return (
            f"0x{item.device_id:02X} status=0x{item.status:02X} "
            f"pos_raw=0x{item.position_raw:04X} pos={item.position_rad:.5f}rad "
            f"current=0x{item.current_raw:04X} torque=0x{item.torque_raw:04X} "
            f"temp={item.motor_temperature_c}/{item.driver_temperature_c}C alarms={alarms}"
        )

    def _send_service_frames(
        self,
        device_ids: Iterable[int],
        token: CancellationToken,
        factory: Callable[..., Any],
    ) -> None:
        ids = self._normalize_ids(device_ids)
        with self._exclusive_operation():
            token.raise_if_cancelled()
            self._send_many(factory(device_id, self.COMMAND_ID) for device_id in ids)

    def _start_heartbeat(self, positions: dict[int, float], generation: int) -> None:
        stop_event = threading.Event()
        started_event = threading.Event()
        with self._state_lock:
            self._heartbeat_positions = dict(positions)
            self._heartbeat_error = None
            self._heartbeat_stop = stop_event
            self._heartbeat_started = started_event

        def heartbeat() -> None:
            next_send = self._clock()
            try:
                while not stop_event.is_set():
                    self._check_generation(generation, reject_stopping=False)
                    with self._state_lock:
                        snapshot = tuple(self._heartbeat_positions.items())
                    self._send_many(
                        position_command_frame(device_id, position, self.COMMAND_ID)
                        for device_id, position in snapshot
                    )
                    started_event.set()
                    next_send += self._heartbeat_interval_s
                    delay = next_send - self._clock()
                    if delay > 0:
                        stop_event.wait(delay)
                    else:
                        next_send = self._clock()
            except BaseException as exc:
                with self._state_lock:
                    if not stop_event.is_set():
                        self._heartbeat_error = exc
                stop_event.set()
                message = f"位置心跳发送失败，已双发失能: {exc}"
                self._emergency_stop_noexcept(snapshot_device_ids)
                if self._on_fault is not None:
                    self._on_fault(message)

        snapshot_device_ids = tuple(positions)
        thread = threading.Thread(target=heartbeat, name="d7-local-can-position", daemon=True)
        with self._state_lock:
            self._heartbeat_thread = thread
        thread.start()

    def _wait_for_heartbeat_start(
        self,
        generation: int,
        token: CancellationToken,
    ) -> None:
        with self._state_lock:
            started = self._heartbeat_started
        if started is None:
            raise RuntimeError("位置心跳未创建")
        deadline = self._clock() + 0.5
        while not started.wait(0.01):
            token.raise_if_cancelled()
            self._check_generation(generation)
            self._raise_heartbeat_error()
            if self._clock() >= deadline:
                raise RuntimeError("位置心跳启动超时")

    def _send_many(self, frames: Iterable[Any]) -> None:
        pending = tuple(frames)
        batch_sender = getattr(self.transport, "send_many", None)
        if callable(batch_sender):
            batch_sender(pending)
            return
        for frame in pending:
            self.transport.send(frame)

    def _wait_with_checks(
        self,
        seconds: float,
        generation: int,
        token: CancellationToken,
    ) -> None:
        deadline = self._clock() + max(0.0, seconds)
        while self._clock() < deadline:
            token.raise_if_cancelled()
            self._check_generation(generation)
            self._raise_heartbeat_error()
            time.sleep(min(0.01, max(0.0, deadline - self._clock())))

    def _stop_heartbeat(self, *, clear_positions: bool) -> None:
        with self._state_lock:
            stop_event = self._heartbeat_stop
            thread = self._heartbeat_thread
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._state_lock:
            if self._heartbeat_thread is thread:
                self._heartbeat_thread = None
                self._heartbeat_stop = None
                self._heartbeat_started = None
            if clear_positions:
                self._heartbeat_positions.clear()
                self._heartbeat_error = None

    def _stop_targets(self, device_ids: Iterable[int]) -> tuple[int, ...]:
        requested = set(self._normalize_ids(device_ids, allow_empty=True))
        with self._state_lock:
            requested.update(self._heartbeat_positions)
        return tuple(sorted(requested))

    def _emergency_stop_noexcept(self, device_ids: Iterable[int]) -> None:
        with suppress(BaseException):
            self.emergency_stop(device_ids)

    def _check_generation(self, expected: int, *, reject_stopping: bool = True) -> None:
        with self._state_lock:
            if self._generation != expected:
                raise RuntimeError("CAN 电机操作已被安全停止抢占")
            if reject_stopping and self._stopping:
                raise RuntimeError("CAN 电机操作已被安全停止抢占")

    def _raise_heartbeat_error(self) -> None:
        with self._state_lock:
            self._raise_heartbeat_error_locked()

    def _raise_heartbeat_error_locked(self) -> None:
        if self._heartbeat_error is not None:
            raise RuntimeError(f"位置心跳发送失败: {self._heartbeat_error}") from self._heartbeat_error

    def _heartbeat_alive_locked(self) -> bool:
        return self._heartbeat_thread is not None and self._heartbeat_thread.is_alive()

    @contextmanager
    def _exclusive_operation(self):  # type: ignore[no-untyped-def]
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("CAN 盒正在执行其他电机操作")
        try:
            with self._state_lock:
                if self._stopping:
                    raise RuntimeError("CAN 电机正在安全停止")
            yield
        finally:
            self._operation_lock.release()

    @staticmethod
    def _normalize_ids(
        device_ids: Iterable[int],
        *,
        allow_empty: bool = False,
    ) -> tuple[int, ...]:
        ids = tuple(dict.fromkeys(int(value) for value in device_ids))
        if not ids and not allow_empty:
            raise ValueError("目标电机不能为空")
        for device_id in ids:
            if not 0 <= device_id <= 0xFF:
                raise ValueError("D7 电机设备 CAN ID 必须在 0x00..0xFF")
        return ids

    @staticmethod
    def _emit(
        report: Callable[[int, str], None] | None,
        value: int,
        message: str,
    ) -> None:
        if report is not None:
            report(value, message)


__all__ = ["LocalCanMotorController", "DISABLE_DATA", "ENABLE_DATA", "READ_MOTOR_STATE_DATA"]
