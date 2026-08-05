from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from d7_factory_studio.core.ports import CancellationToken
from d7_factory_studio.features.serial485.service import Serial485Error, Serial485Service
from d7_factory_studio.protocols.serial485 import (
    BROADCAST_STATION,
    CONTROLWORD_BRAKE_RELEASE,
    CONTROLWORD_ENABLE,
    CONTROLWORD_STOP_POSITION,
    DEFAULT_STATION_AFTER_RESET,
    build_0e_write_multi,
    build_echo,
    comm_id_broadcast_reset_item,
    comm_id_from_station,
    comm_id_register_value,
    comm_id_write_item,
    control_authority_item,
    controlword_item,
    eeprom_save_item,
    relative_position_mode_items,
    speed_mode_items,
    station_from_comm_id,
    system_reset_item,
)

SPEED_FORWARD = 0x00155555
SPEED_REVERSE = 0xFFEAAAAB
SPEED_STOP = 0x00000000


class Serial485ControllerError(RuntimeError):
    pass


class Serial485SafetyStopError(Serial485ControllerError):
    pass


@dataclass(frozen=True, slots=True)
class SerialScanResult:
    station: int
    comm_id: int


@dataclass(frozen=True, slots=True)
class CycleOptions:
    comm_id: int = 0
    cycles: int | None = None
    run_seconds: float = 5.0
    round_wait_seconds: float = 10.0
    probe_interval_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not 0 <= self.comm_id <= 0xFF:
            raise ValueError("comm_id must be in 0..255")
        if self.cycles is not None and self.cycles < 1:
            raise ValueError("cycles must be positive or None")
        if self.run_seconds < 0 or self.round_wait_seconds < 0:
            raise ValueError("cycle durations cannot be negative")


class Serial485Controller:
    def __init__(
        self,
        service: Serial485Service,
        *,
        on_log: Callable[[str], None] | None = None,
        on_progress: Callable[[int, str], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.service = service
        self.on_log = on_log or (lambda _message: None)
        self.on_progress = on_progress or (lambda _percent, _message: None)
        self._sleep_impl = sleeper
        self._internal_token = CancellationToken()

    def cancel(self) -> None:
        self._internal_token.cancel()

    def probe(self, comm_id: int, *, attempts: int = 5, timeout_s: float = 0.5) -> bool:
        request = build_echo(station=station_from_comm_id(comm_id))
        return any(self.service.echo(request, timeout_s=timeout_s) for _ in range(attempts))

    def scan(
        self,
        token: CancellationToken | None = None,
        *,
        probe_timeout_s: float = 0.08,
    ) -> SerialScanResult | None:
        active_token = self._begin_operation(token)
        for station in range(256):
            self._check_cancelled(active_token)
            self.on_progress(round(station * 100 / 255), f"扫描站号 0x{station:02X}")
            if self.service.echo(build_echo(station=station), timeout_s=probe_timeout_s):
                result = SerialScanResult(station, comm_id_from_station(station))
                self.on_progress(100, f"发现通信 ID 0x{result.comm_id:02X}")
                return result
            self._sleep(0.002, active_token)
        self.on_progress(100, "扫描完成，未发现设备")
        return None

    def broadcast_reset_comm_id(self, token: CancellationToken | None = None) -> None:
        active_token = self._begin_operation(token)
        self._write_items(BROADCAST_STATION, [comm_id_broadcast_reset_item()], active_token)
        self._sleep(0.08, active_token)
        self._write_items(DEFAULT_STATION_AFTER_RESET, [system_reset_item()], active_token)

    def write_comm_id(self, comm_id: int, token: CancellationToken | None = None) -> None:
        active_token = self._begin_operation(token)
        station = DEFAULT_STATION_AFTER_RESET
        self._write_items(station, [comm_id_write_item(comm_id)], active_token)
        self.on_log(f"写入通信 ID 0x{comm_id:02X}，寄存器值 0x{comm_id_register_value(comm_id):04X}")
        self._sleep(0.08, active_token)
        self._write_items(station, [eeprom_save_item()], active_token)
        self._sleep(0.08, active_token)
        self._write_items(station, [system_reset_item()], active_token)
        self._sleep(2.0, active_token)
        if not self.service.echo(build_echo(station=station_from_comm_id(comm_id)), timeout_s=2.5):
            raise Serial485ControllerError(f"ID 0x{comm_id:02X} 写入后回读验证失败")

    def set_control_authority(
        self,
        comm_id: int,
        owned: bool,
        token: CancellationToken | None = None,
    ) -> None:
        active_token = self._begin_operation(token)
        station = station_from_comm_id(comm_id)
        self._write_items(station, [control_authority_item(1 if owned else 0)], active_token)
        self._sleep(0.15, active_token)
        self._write_items(station, [eeprom_save_item()], active_token)
        self._sleep(0.65, active_token)
        self._write_items(station, [system_reset_item()], active_token)

    def take_control_authority(self, comm_id: int, token: CancellationToken | None = None) -> None:
        self.set_control_authority(comm_id, True, token)

    def release_control_authority(self, comm_id: int, token: CancellationToken | None = None) -> None:
        self.set_control_authority(comm_id, False, token)

    def set_controlword(
        self,
        comm_id: int,
        value: int,
        token: CancellationToken | None = None,
    ) -> None:
        self._set_controlword(comm_id, value, self._begin_operation(token))

    def set_speed(
        self,
        comm_id: int,
        direction: str,
        token: CancellationToken | None = None,
    ) -> None:
        values = {"forward": SPEED_FORWARD, "reverse": SPEED_REVERSE, "stop": SPEED_STOP}
        try:
            velocity = values[direction]
        except KeyError as exc:
            raise ValueError("direction must be forward, reverse or stop") from exc
        self._set_speed_value(comm_id, velocity, self._begin_operation(token))

    def move_relative(
        self,
        comm_id: int,
        *,
        angle_degrees: float,
        counts_per_revolution: int = 16_777_216,
        speed_rpm: float = 5.0,
        token: CancellationToken | None = None,
    ) -> None:
        if not -360 <= angle_degrees <= 360 or angle_degrees == 0:
            raise ValueError("relative angle must be in -360..360 and non-zero")
        if not 1 <= counts_per_revolution <= 0x7FFFFFFF:
            raise ValueError("counts_per_revolution out of range")
        if not 0 < speed_rpm <= 60:
            raise ValueError("speed_rpm must be in 0..60")
        active_token = self._begin_operation(token)
        station = station_from_comm_id(comm_id)
        delta = round(counts_per_revolution * angle_degrees / 360.0)
        velocity = max(1, round(counts_per_revolution * speed_rpm / 60.0))
        items = relative_position_mode_items(
            delta_counts=delta,
            profile_velocity_counts_s=velocity,
            acceleration_counts_s2=velocity,
            deceleration_counts_s2=velocity,
        )
        self._write_items(station, items, active_token)
        self._sleep(0.1, active_token)
        self._write_items(station, [controlword_item(CONTROLWORD_BRAKE_RELEASE)], active_token)
        self._sleep(0.1, active_token)
        self._write_items(station, [controlword_item(0x007F)], active_token)

    def run_cycle(
        self,
        options: CycleOptions | None = None,
        token: CancellationToken | None = None,
    ) -> None:
        opts = options or CycleOptions()
        active_token = self._begin_operation(token)
        completed = 0
        primary_failure: BaseException | None = None
        try:
            while opts.cycles is None or completed < opts.cycles:
                self._wait_until_present(opts, active_token)
                self._set_controlword(opts.comm_id, CONTROLWORD_ENABLE, active_token)
                self._sleep(0.2, active_token)
                self._set_controlword(opts.comm_id, CONTROLWORD_BRAKE_RELEASE, active_token)
                self._sleep(0.2, active_token)
                self._set_speed_value(opts.comm_id, SPEED_FORWARD, active_token)
                self._sleep(opts.run_seconds, active_token)
                self._set_speed_value(opts.comm_id, SPEED_REVERSE, active_token)
                self._sleep(opts.run_seconds, active_token)
                self._set_speed_value(opts.comm_id, SPEED_STOP, active_token)
                completed += 1
                percent = 0 if opts.cycles is None else round(completed * 100 / opts.cycles)
                self.on_progress(percent, f"循环测试完成 {completed} 轮")
                if opts.cycles is None or completed < opts.cycles:
                    self._sleep(opts.round_wait_seconds, active_token)
        except BaseException as exc:
            primary_failure = exc
            raise
        finally:
            try:
                self._safety_stop(opts.comm_id)
            except Exception as stop_error:
                self.on_log(f"循环任务安全停止失败: {stop_error}")
                if primary_failure is None:
                    raise Serial485SafetyStopError(f"循环任务安全停止失败: {stop_error}") from stop_error

    def _wait_until_present(self, options: CycleOptions, token: CancellationToken) -> None:
        request = build_echo(station=station_from_comm_id(options.comm_id))
        while True:
            self._check_cancelled(token)
            if self.service.echo(request, timeout_s=0.08):
                return
            self._sleep(options.probe_interval_seconds, token)

    def _set_controlword(self, comm_id: int, value: int, token: CancellationToken) -> None:
        self._write_items(station_from_comm_id(comm_id), [controlword_item(value)], token)

    def _set_speed_value(self, comm_id: int, velocity: int, token: CancellationToken) -> None:
        self._write_items(station_from_comm_id(comm_id), speed_mode_items(velocity), token)

    def _safety_stop(self, comm_id: int) -> None:
        """Best-effort stop that deliberately ignores cancellation state."""
        station = station_from_comm_id(comm_id)
        errors: list[Exception] = []
        for items in (speed_mode_items(SPEED_STOP), [controlword_item(CONTROLWORD_STOP_POSITION)]):
            try:
                self.service.write(build_0e_write_multi(station=station, items=items))
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 2:
            raise Serial485Error("speed stop and position stop both failed") from errors[-1]

    def _write_items(self, station: int, items: list[bytes], token: CancellationToken) -> None:
        self._check_cancelled(token)
        try:
            self.service.write(build_0e_write_multi(station=station, items=items))
        except Exception as exc:
            raise Serial485ControllerError(str(exc)) from exc

    def _begin_operation(self, token: CancellationToken | None) -> CancellationToken:
        self._internal_token = CancellationToken()
        return token or self._internal_token

    def _check_cancelled(self, token: CancellationToken) -> None:
        token.raise_if_cancelled()
        if token is not self._internal_token:
            self._internal_token.raise_if_cancelled()

    def _sleep(self, seconds: float, token: CancellationToken) -> None:
        deadline = time.monotonic() + max(seconds, 0)
        while time.monotonic() < deadline:
            self._check_cancelled(token)
            self._sleep_impl(min(0.05, max(0.0, deadline - time.monotonic())))
