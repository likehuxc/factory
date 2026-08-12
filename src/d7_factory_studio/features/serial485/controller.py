from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from d7_factory_studio.core.ports import CancellationToken, OperationCancelled
from d7_factory_studio.features.serial485.service import Serial485Error, Serial485Service
from d7_factory_studio.protocols.serial485 import (
    ABS_ENCODER_OFFSET_PARAMETER,
    ACTUAL_POSITION_PARAMETER,
    BROADCAST_STATION,
    COMM_ID_PARAMETER,
    CONTROL_AUTHORITY_PARAMETER,
    CONTROLWORD_BRAKE_ON,
    CONTROLWORD_BRAKE_RELEASE,
    CONTROLWORD_ENABLE,
    CONTROLWORD_START_ABSOLUTE,
    CONTROLWORD_START_RELATIVE,
    CONTROLWORD_STOP_POSITION,
    D7_COUNTS_PER_REVOLUTION,
    DEFAULT_STATION_AFTER_RESET,
    DEVICE_ID_PARAMETER,
    DEVICE_NAME_PARAMETER,
    HARDWARE_VERSION_PARAMETER,
    MOTOR_IDENTIFICATION_STATE_PARAMETER,
    MOTOR_PHASE_SEQUENCE_PARAMETER,
    SERVO_STATUS_PARAMETER,
    SOFTWARE_VERSION_PARAMETER,
    ParameterReadItem,
    absolute_position_mode_items,
    build_0e_write_multi,
    build_echo,
    comm_id_broadcast_reset_item,
    comm_id_from_station,
    comm_id_register_value,
    comm_id_write_item,
    control_authority_item,
    controlword_item,
    eeprom_save_item,
    motor_identification_control_item,
    relative_position_mode_items,
    speed_mode_items,
    station_from_comm_id,
    system_reset_item,
)

_T = TypeVar("_T")

DEFAULT_SPEED_RAD_S = 0.5
DEFAULT_ACCEL_RAD_S2 = 1.0
SPEED_FORWARD = round(DEFAULT_SPEED_RAD_S * D7_COUNTS_PER_REVOLUTION / math.tau)
SPEED_REVERSE = -SPEED_FORWARD
SPEED_STOP = 0


class Serial485ControllerError(RuntimeError):
    pass


class Serial485StageError(Serial485ControllerError):
    def __init__(self, stage: str, cause: BaseException) -> None:
        super().__init__(f"[{stage}] {cause}")
        self.stage = stage


class Serial485SafetyStopError(Serial485ControllerError):
    pass


@dataclass(frozen=True, slots=True)
class Serial485Identity:
    requested_comm_id: int
    comm_id: int
    register_value: int


@dataclass(frozen=True, slots=True)
class Serial485Connection:
    requested_comm_id: int
    comm_id: int
    station: int
    attempts: int
    echo_frame: bytes
    echo_ok: bool
    register_value: int | None = None
    device_id: int | None = None
    device_name: str = ""
    hardware_version: str = ""
    software_version: str = ""
    parameter_values: tuple[tuple[int, int], ...] = ()
    parameter_error: str = ""


@dataclass(frozen=True, slots=True)
class Serial485Position:
    counts: int
    radians: float


@dataclass(frozen=True, slots=True)
class PhaseIdentificationResult:
    phase_sequence: int
    encoder_offset: int


@dataclass(frozen=True, slots=True)
class SerialScanResult:
    station: int
    comm_id: int


@dataclass(frozen=True, slots=True)
class CycleOptions:
    comm_id: int = 1
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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.service = service
        self.on_log = on_log or (lambda _message: None)
        self.on_progress = on_progress or (lambda _percent, _message: None)
        self._sleep_impl = sleeper
        self._clock = clock
        self._internal_token = CancellationToken()

    def cancel(self) -> None:
        self._internal_token.cancel()

    def read_identity(self, comm_id: int, *, timeout_s: float = 0.5) -> Serial485Identity:
        item = self._read_one(comm_id, COMM_ID_PARAMETER, timeout_s=timeout_s)
        assert item.value is not None
        register_value = item.value
        actual_comm_id = self._comm_id_from_read_value(register_value)
        if actual_comm_id != comm_id:
            raise Serial485ControllerError(
                f"communication ID mismatch: station for 0x{comm_id:02X} returned 0x{actual_comm_id:02X}"
            )
        return Serial485Identity(comm_id, actual_comm_id, register_value)

    def connect(
        self,
        comm_id: int,
        *,
        attempts: int = 5,
        timeout_s: float = 0.5,
        read_parameter: bool = True,
        allow_station_fallback: bool = False,
    ) -> Serial485Connection:
        """Connect using ServoStudio echo and parameter-read behavior."""
        if attempts < 1:
            raise ValueError("attempts must be positive")
        primary_station = station_from_comm_id(comm_id)
        stations = [primary_station]
        if allow_station_fallback:
            for candidate in (comm_id, 0):
                if candidate not in stations:
                    stations.append(candidate)

        errors: list[str] = []
        for station_index, station in enumerate(stations):
            request = build_echo(station=station)
            station_attempts = attempts if station_index == 0 else 1
            matched_attempt = 0
            for attempt in range(1, station_attempts + 1):
                self.on_progress(
                    min(65, 10 + station_index * 18 + round(attempt * 30 / station_attempts)),
                    f"正在尝试线站地址 0x{station:02X}（回送 {attempt}/{station_attempts}）",
                )
                try:
                    if self.service.echo(request, timeout_s=timeout_s):
                        matched_attempt = attempt
                        break
                except Exception as exc:
                    errors.append(f"站 0x{station:02X} 回送：{exc}")

            parameter_values: list[tuple[int, int]] = []
            parameter_errors: list[str] = []
            if read_parameter:
                self.on_progress(70 + station_index * 5, f"正在读取站 0x{station:02X} 的电机参数")
                for parameter in (
                    COMM_ID_PARAMETER,
                    DEVICE_ID_PARAMETER,
                    CONTROL_AUTHORITY_PARAMETER,
                    SERVO_STATUS_PARAMETER,
                    ACTUAL_POSITION_PARAMETER,
                ):
                    try:
                        items = self.service.read_parameters(
                            station,
                            [parameter],
                            timeout_s=timeout_s,
                        )
                        if len(items) == 1 and items[0].value is not None:
                            parameter_values.append((parameter.address, int(items[0].value)))
                    except Exception as exc:
                        parameter_errors.append(f"0x{parameter.address:06X}: {exc}")

            version_values: dict[int, str] = {}
            if read_parameter and (matched_attempt or parameter_values):
                for parameter in (
                    DEVICE_NAME_PARAMETER,
                    HARDWARE_VERSION_PARAMETER,
                    SOFTWARE_VERSION_PARAMETER,
                ):
                    try:
                        items = self.service.read_parameters(
                            station,
                            [parameter],
                            timeout_s=timeout_s,
                        )
                        if len(items) == 1 and isinstance(items[0].value, str):
                            version_values[parameter.address] = items[0].value
                    except Exception as exc:
                        parameter_errors.append(f"0x{parameter.address:06X}: {exc}")

            if matched_attempt or parameter_values:
                register_value = next(
                    (
                        value
                        for address, value in parameter_values
                        if address == COMM_ID_PARAMETER.address
                    ),
                    None,
                )
                try:
                    reported_comm_id = self._comm_id_from_read_value(register_value)
                except Serial485ControllerError:
                    actual_comm_id = comm_id_from_station(station)
                else:
                    # A successful explicit connection is identified by the
                    # addressed wire station.  0x200711 is still shown as a raw
                    # diagnostic value because JiHua firmware variants return
                    # different read representations for the same written ID.
                    actual_comm_id = comm_id_from_station(station) if matched_attempt else reported_comm_id
                device_id = next(
                    (
                        value
                        for address, value in parameter_values
                        if address == DEVICE_ID_PARAMETER.address
                    ),
                    None,
                )
                self.on_progress(100, f"线站地址 0x{station:02X} 已连接")
                return Serial485Connection(
                    requested_comm_id=comm_id,
                    comm_id=actual_comm_id,
                    station=station,
                    attempts=matched_attempt or station_attempts,
                    echo_frame=request,
                    echo_ok=bool(matched_attempt),
                    register_value=register_value,
                    device_id=device_id,
                    device_name=version_values.get(DEVICE_NAME_PARAMETER.address, ""),
                    hardware_version=version_values.get(HARDWARE_VERSION_PARAMETER.address, ""),
                    software_version=version_values.get(SOFTWARE_VERSION_PARAMETER.address, ""),
                    parameter_values=tuple(parameter_values),
                    parameter_error="；".join(parameter_errors),
                )
            errors.extend(f"站 0x{station:02X} 参数 {error}" for error in parameter_errors)

        attempted = "、".join(f"0x{station:02X}" for station in stations)
        detail = errors[-1] if errors else "未收到回送或参数响应"
        raise Serial485ControllerError(
            f"通信 ID 0x{comm_id:02X} 连接失败；已尝试线站地址 {attempted}；{detail}"
        )

    def probe(
        self,
        comm_id: int,
        *,
        attempts: int = 5,
        timeout_s: float = 0.5,
    ) -> Serial485Identity | None:
        if attempts < 1:
            raise ValueError("attempts must be positive")
        for _attempt in range(attempts):
            try:
                return self.read_identity(comm_id, timeout_s=timeout_s)
            except (Serial485Error, Serial485ControllerError, ValueError):
                continue
        return None

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
                self.on_progress(100, f"发现通讯 ID 0x{result.comm_id:02X}")
                return result
            self._sleep(0.002, active_token)
        self.on_progress(100, "扫描完成，未发现设备")
        return None

    def broadcast_reset_comm_id(self, token: CancellationToken | None = None) -> None:
        active_token = self._begin_operation(token)
        self._write_items(BROADCAST_STATION, [comm_id_broadcast_reset_item()], active_token)
        self._sleep(0.08, active_token)
        self._write_items(DEFAULT_STATION_AFTER_RESET, [system_reset_item()], active_token)

    def write_comm_id(
        self,
        comm_id: int,
        token: CancellationToken | None = None,
        *,
        current_comm_id: int = 1,
    ) -> Serial485Connection:
        if not 0 <= comm_id <= 0xFF:
            raise ValueError("communication ID must be in 0..255")
        active_token = self._begin_operation(token)
        current_station = station_from_comm_id(current_comm_id)

        self._check_cancelled(active_token)
        self._run_stage(
            "initial_connect",
            lambda: self.connect(
                current_comm_id,
                attempts=5,
                timeout_s=0.5,
                read_parameter=False,
                allow_station_fallback=False,
            ),
        )
        self._run_stage(
            "write_id",
            lambda: self._write_items(
                current_station,
                [comm_id_write_item(comm_id)],
                active_token,
            ),
        )
        self.on_log(
            f"写入通讯 ID 0x{comm_id:02X}，寄存器值 0x{comm_id_register_value(comm_id):04X}"
        )
        self._run_stage(
            "save_eeprom",
            lambda: self._write_items(current_station, [eeprom_save_item()], active_token),
        )
        self._run_stage("wait_before_reset", lambda: self._sleep(2.0, active_token))
        self._run_stage(
            "reset",
            lambda: self._write_items(current_station, [system_reset_item()], active_token),
        )
        self._run_stage("wait_after_reset", lambda: self._sleep(2.0, active_token))
        return self._run_stage(
            "verify_connect",
            lambda: self.connect(
                comm_id,
                attempts=5,
                timeout_s=0.5,
                read_parameter=True,
                allow_station_fallback=False,
            ),
        )

    def identify_wheel_phases(
        self,
        comm_id: int,
        token: CancellationToken | None = None,
        *,
        stage_timeout_s: float = 180.0,
        poll_interval_s: float = 1.0,
    ) -> PhaseIdentificationResult:
        if stage_timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError("identification timeout and poll interval must be positive")
        active_token = self._begin_operation(token)
        servo_status = self._read_one(comm_id, SERVO_STATUS_PARAMETER, timeout_s=0.8).value
        if servo_status in {520, 536}:
            raise Serial485ControllerError(f"伺服处于故障状态 {servo_status}，请先清除故障")

        primary_failure: BaseException | None = None
        try:
            phase_sequence = self._run_identification_stage(
                comm_id,
                mode=2,
                success_state=34,
                failure_state=47,
                result_parameter=MOTOR_PHASE_SEQUENCE_PARAMETER,
                label="动力线相序辨识",
                progress_range=(5, 48),
                timeout_s=stage_timeout_s,
                poll_interval_s=poll_interval_s,
                token=active_token,
            )
            encoder_offset = self._run_identification_stage(
                comm_id,
                mode=4,
                success_state=66,
                failure_state=79,
                result_parameter=ABS_ENCODER_OFFSET_PARAMETER,
                label="绝对值编码器偏置辨识",
                progress_range=(52, 98),
                timeout_s=stage_timeout_s,
                poll_interval_s=poll_interval_s,
                token=active_token,
            )
            self.on_progress(100, "轮毂电机两阶段寻相完成")
            return PhaseIdentificationResult(int(phase_sequence), int(encoder_offset))
        except BaseException as exc:
            primary_failure = exc
            raise
        finally:
            try:
                self.service.write(
                    build_0e_write_multi(
                        station=station_from_comm_id(comm_id),
                        items=[motor_identification_control_item(0)],
                    )
                )
            except Exception as stop_error:
                if primary_failure is None:
                    raise Serial485ControllerError(f"停止电机寻相失败: {stop_error}") from stop_error
                self.on_log(f"停止电机寻相失败: {stop_error}")

    def _run_identification_stage(
        self,
        comm_id: int,
        *,
        mode: int,
        success_state: int,
        failure_state: int,
        result_parameter,
        label: str,
        progress_range: tuple[int, int],
        timeout_s: float,
        poll_interval_s: float,
        token: CancellationToken,
    ) -> int:
        station = station_from_comm_id(comm_id)
        self.on_progress(progress_range[0], f"开始{label}")
        self._write_items(station, [motor_identification_control_item(mode)], token)
        deadline = self._clock() + timeout_s
        polls = 0
        while self._clock() < deadline:
            self._check_cancelled(token)
            state_item = self._read_one(
                comm_id,
                MOTOR_IDENTIFICATION_STATE_PARAMETER,
                timeout_s=min(1.0, poll_interval_s),
            )
            state = int(state_item.value)
            if state == success_state:
                result = self._read_one(comm_id, result_parameter, timeout_s=1.0)
                self.on_progress(progress_range[1], f"{label}成功")
                return int(result.value)
            if state == failure_state:
                raise Serial485ControllerError(f"{label}失败，状态 {state}")
            polls += 1
            progress = min(progress_range[1] - 1, progress_range[0] + polls)
            self.on_progress(progress, f"{label}中 · 状态 {state}")
            self._sleep(poll_interval_s, token)
        raise Serial485ControllerError(f"{label}超时（{timeout_s:g} s）")

    def set_control_authority(
        self,
        comm_id: int,
        owned: bool,
        token: CancellationToken | None = None,
        *,
        reboot_wait_s: float = 2.0,
        verify_attempts: int = 3,
        verify_interval_s: float = 0.25,
    ) -> None:
        if reboot_wait_s < 0 or verify_attempts < 1 or verify_interval_s < 0:
            raise ValueError("invalid control-authority timing")
        active_token = self._begin_operation(token)
        station = station_from_comm_id(comm_id)
        expected = 0 if owned else 1
        # 0 selects the local serial/PC controller; 1 hands control back to CAN.
        self._write_items(station, [control_authority_item(expected)], active_token)
        self._sleep(0.15, active_token)
        self._write_items(station, [eeprom_save_item()], active_token)
        self._sleep(0.65, active_token)
        self._write_items(station, [system_reset_item()], active_token)
        self._sleep(reboot_wait_s, active_token)

        last_value: int | None = None
        last_error: Serial485ControllerError | None = None
        for attempt in range(1, verify_attempts + 1):
            self._check_cancelled(active_token)
            try:
                value = self._read_one(
                    comm_id,
                    CONTROL_AUTHORITY_PARAMETER,
                    timeout_s=0.5,
                ).value
            except Serial485ControllerError as exc:
                last_error = exc
            else:
                assert isinstance(value, int)
                last_value = value
                if value == expected:
                    return
            if attempt < verify_attempts:
                self._sleep(verify_interval_s, active_token)

        if last_value is not None:
            raise Serial485ControllerError(
                f"控制权校验失败：0x200201=0x{last_value:04X}，期望 0x{expected:04X}"
            )
        # The JiHua quick-config tool treats the write/save/reset sequence as
        # complete without requiring an immediate response from the rebooting
        # drive.  Keep the readback when available, but do not turn a valid
        # control-source switch into a UI failure solely because the drive has
        # not finished booting yet.
        assert last_error is not None
        self.on_log(f"驱动复位后暂未返回控制权状态，将由连接状态回读确认：{last_error}")

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

    def enable_servo(
        self,
        comm_id: int,
        token: CancellationToken | None = None,
        *,
        step_interval_s: float = 0.3,
        verify_attempts: int = 3,
        verify_interval_s: float = 0.2,
        sequence_attempts: int = 2,
    ) -> int | None:
        """Enable using ServoStudio first, then its quick-tool compatibility path."""
        if (
            step_interval_s < 0
            or verify_interval_s < 0
            or verify_attempts < 1
            or sequence_attempts < 1
        ):
            raise ValueError("invalid servo enable timing")
        active_token = self._begin_operation(token)
        full_steps = (
            (CONTROLWORD_ENABLE, "0x0006 准备使能"),
            (CONTROLWORD_BRAKE_ON, "0x0007 接通使能"),
            (CONTROLWORD_BRAKE_RELEASE, "0x000F 进入使能"),
        )
        quick_steps = (
            (CONTROLWORD_ENABLE, "0x0006 准备使能"),
            (CONTROLWORD_BRAKE_RELEASE, "0x000F 松开抱闸"),
        )
        last_statusword: int | None = None
        last_read_error: Serial485ControllerError | None = None
        for sequence_attempt in range(1, sequence_attempts + 1):
            # ServoStudio uses 6 -> 7 -> 15.  The working JiHua quick-config
            # auto-test uses 6 -> 15, so use that as the firmware fallback.
            steps = full_steps if sequence_attempt == 1 else quick_steps
            for index, (controlword, label) in enumerate(steps, start=1):
                self._check_cancelled(active_token)
                retry = f"（重试 {sequence_attempt}/{sequence_attempts}）" if sequence_attempt > 1 else ""
                self.on_progress(
                    round(index * 60 / len(steps)),
                    f"使能步骤 {index}/{len(steps)}：{label}{retry}",
                )
                self._set_controlword(comm_id, controlword, active_token)
                if index < len(steps):
                    self._sleep(step_interval_s, active_token)

            for attempt in range(1, verify_attempts + 1):
                self._sleep(verify_interval_s, active_token)
                self.on_progress(
                    65 + round(attempt * 30 / verify_attempts),
                    "正在确认伺服使能状态",
                )
                try:
                    statusword = self._read_one(
                        comm_id,
                        SERVO_STATUS_PARAMETER,
                        timeout_s=0.5,
                    ).value
                except Serial485ControllerError as exc:
                    last_read_error = exc
                    continue
                assert isinstance(statusword, int)
                last_statusword = statusword
                if statusword & 0x7F == 0x37:
                    self.on_progress(100, f"伺服使能成功（0x{statusword:04X}）")
                    return statusword

            if last_statusword is None and last_read_error is not None:
                # The quick tool does not read 0x6041 at all.  Some firmware
                # accepts the controlwords but does not expose this object over
                # 0x0D, so defer confirmation to the normal connection refresh.
                self.on_log(f"使能控制字已发送，状态字暂不可读：{last_read_error}")
                return None

            if sequence_attempt < sequence_attempts:
                detail = (
                    f"状态字 0x{last_statusword:04X}"
                    if last_statusword is not None
                    else "状态回读失败"
                )
                self.on_log(f"使能未生效（{detail}），正在重发完整使能序列")

        if last_statusword is None:
            return None
        raise Serial485ControllerError(
            f"使能状态校验失败：状态字 0x{last_statusword:04X} 仍为禁能，期望低 7 位为 0x37"
        )

    def set_speed(
        self,
        comm_id: int,
        rad_s: float | str,
        token: CancellationToken | None = None,
        *,
        motion_time_s: float | None = None,
        duration_s: float | None = None,
        accel_rad_s2: float = DEFAULT_ACCEL_RAD_S2,
        decel_rad_s2: float = DEFAULT_ACCEL_RAD_S2,
        counts_per_revolution: int = D7_COUNTS_PER_REVOLUTION,
    ) -> None:
        if isinstance(rad_s, str):
            try:
                rad_s = {
                    "forward": DEFAULT_SPEED_RAD_S,
                    "reverse": -DEFAULT_SPEED_RAD_S,
                    "stop": 0.0,
                }[rad_s]
            except KeyError as exc:
                raise ValueError("direction must be forward, reverse or stop") from exc
        if motion_time_s is not None and duration_s is not None:
            raise ValueError("specify only one of motion_time_s and duration_s")
        run_seconds = motion_time_s if motion_time_s is not None else duration_s
        if run_seconds is not None and (not math.isfinite(run_seconds) or run_seconds < 0):
            raise ValueError("motion time must be finite and non-negative")

        velocity, acceleration, deceleration = self._motion_counts(
            rad_s=float(rad_s),
            accel_rad_s2=accel_rad_s2,
            decel_rad_s2=decel_rad_s2,
            counts_per_revolution=counts_per_revolution,
        )
        if velocity == 0 and run_seconds is not None:
            raise ValueError("目标速度为 0；定时速度运动需要非零速度，请使用停止按钮停止电机")
        active_token = self._begin_operation(token)
        raw_velocity = velocity & 0xFFFFFFFF
        self.on_progress(
            5,
            f"速度参数：{float(rad_s):.6f} rad/s · raw 0x{raw_velocity:08X} "
            f"· 加速 0x{acceleration:08X} · 减速 0x{deceleration:08X}",
        )
        if run_seconds is None:
            self._set_speed_value(
                comm_id,
                velocity,
                acceleration,
                deceleration,
                active_token,
            )
            return

        primary_failure: BaseException | None = None
        try:
            self._set_speed_value(
                comm_id,
                velocity,
                acceleration,
                deceleration,
                active_token,
            )
            self._sleep(run_seconds, active_token)
        except BaseException as exc:
            primary_failure = exc
            raise
        finally:
            try:
                self._speed_stop(comm_id, acceleration, deceleration)
            except Exception as stop_error:
                self.on_log(f"定时速度运动停止失败: {stop_error}")
                if primary_failure is None:
                    raise Serial485SafetyStopError(
                        f"定时速度运动停止失败: {stop_error}"
                    ) from stop_error

    def read_absolute_position(
        self,
        comm_id: int,
        *,
        counts_per_revolution: int = D7_COUNTS_PER_REVOLUTION,
        timeout_s: float = 0.5,
    ) -> Serial485Position:
        self._validate_counts_per_revolution(counts_per_revolution)
        item = self._read_one(comm_id, ACTUAL_POSITION_PARAMETER, timeout_s=timeout_s)
        assert item.value is not None
        return Serial485Position(
            counts=item.value,
            radians=item.value * math.tau / counts_per_revolution,
        )

    def move_absolute(
        self,
        comm_id: int,
        *,
        position_rad: float,
        speed_rad_s: float = DEFAULT_SPEED_RAD_S,
        accel_rad_s2: float = DEFAULT_ACCEL_RAD_S2,
        decel_rad_s2: float = DEFAULT_ACCEL_RAD_S2,
        counts_per_revolution: int = D7_COUNTS_PER_REVOLUTION,
        token: CancellationToken | None = None,
    ) -> None:
        target = self._position_counts(position_rad, counts_per_revolution)
        velocity, acceleration, deceleration = self._motion_counts(
            rad_s=abs(speed_rad_s),
            accel_rad_s2=accel_rad_s2,
            decel_rad_s2=decel_rad_s2,
            counts_per_revolution=counts_per_revolution,
        )
        if velocity == 0:
            raise ValueError("absolute-position speed must be non-zero")
        active_token = self._begin_operation(token)
        self._start_position_motion(
            comm_id,
            absolute_position_mode_items(
                target_counts=target,
                profile_velocity_counts_s=velocity,
                acceleration_counts_s2=acceleration,
                deceleration_counts_s2=deceleration,
            ),
            CONTROLWORD_START_ABSOLUTE,
            active_token,
        )

    def move_relative_angle(
        self,
        comm_id: int,
        *,
        angle_deg: float,
        speed_rad_s: float = DEFAULT_SPEED_RAD_S,
        accel_rad_s2: float = DEFAULT_ACCEL_RAD_S2,
        decel_rad_s2: float = DEFAULT_ACCEL_RAD_S2,
        counts_per_revolution: int = D7_COUNTS_PER_REVOLUTION,
        token: CancellationToken | None = None,
    ) -> None:
        if not math.isfinite(angle_deg) or not -360 <= angle_deg <= 360 or angle_deg == 0:
            raise ValueError("relative angle must be finite, non-zero and in -360..360 degrees")
        delta = self._position_counts(math.radians(angle_deg), counts_per_revolution)
        velocity, acceleration, deceleration = self._motion_counts(
            rad_s=abs(speed_rad_s),
            accel_rad_s2=accel_rad_s2,
            decel_rad_s2=decel_rad_s2,
            counts_per_revolution=counts_per_revolution,
        )
        if velocity == 0:
            raise ValueError("relative-position speed must be non-zero")
        active_token = self._begin_operation(token)
        self._start_position_motion(
            comm_id,
            relative_position_mode_items(
                delta_counts=delta,
                profile_velocity_counts_s=velocity,
                acceleration_counts_s2=acceleration,
                deceleration_counts_s2=deceleration,
            ),
            CONTROLWORD_START_RELATIVE,
            active_token,
        )

    def move_relative(
        self,
        comm_id: int,
        *,
        angle_degrees: float,
        counts_per_revolution: int = D7_COUNTS_PER_REVOLUTION,
        speed_rpm: float = 5.0,
        token: CancellationToken | None = None,
    ) -> None:
        if not math.isfinite(speed_rpm) or not 0 < speed_rpm <= 60:
            raise ValueError("speed_rpm must be finite and in 0..60")
        self.move_relative_angle(
            comm_id,
            angle_deg=angle_degrees,
            speed_rad_s=speed_rpm * math.tau / 60,
            counts_per_revolution=counts_per_revolution,
            token=token,
        )

    def run_cycle(
        self,
        options: CycleOptions | None = None,
        token: CancellationToken | None = None,
    ) -> None:
        opts = options or CycleOptions()
        active_token = self._begin_operation(token)
        completed = 0
        primary_failure: BaseException | None = None
        acceleration = self._rad_to_counts(DEFAULT_ACCEL_RAD_S2, D7_COUNTS_PER_REVOLUTION)
        try:
            while opts.cycles is None or completed < opts.cycles:
                self._wait_until_present(opts, active_token)
                self._set_controlword(opts.comm_id, CONTROLWORD_ENABLE, active_token)
                self._sleep(0.2, active_token)
                self._set_controlword(opts.comm_id, CONTROLWORD_BRAKE_RELEASE, active_token)
                self._sleep(0.2, active_token)
                self._set_speed_value(
                    opts.comm_id,
                    SPEED_FORWARD,
                    acceleration,
                    acceleration,
                    active_token,
                )
                self._sleep(opts.run_seconds, active_token)
                self._set_speed_value(
                    opts.comm_id,
                    SPEED_REVERSE,
                    acceleration,
                    acceleration,
                    active_token,
                )
                self._sleep(opts.run_seconds, active_token)
                self._set_speed_value(
                    opts.comm_id,
                    SPEED_STOP,
                    acceleration,
                    acceleration,
                    active_token,
                )
                completed += 1
                percent = 0 if opts.cycles is None else round(completed * 100 / opts.cycles)
                self.on_progress(percent, f"循环测试完成 {completed} 轮")
                if opts.cycles is None or completed < opts.cycles:
                    self._sleep(opts.round_wait_seconds, active_token)
        except BaseException as exc:
            primary_failure = exc
            raise
        finally:
            self._finish_with_safety_stop(opts.comm_id, primary_failure, "循环任务")

    def _wait_until_present(self, options: CycleOptions, token: CancellationToken) -> None:
        request = build_echo(station=station_from_comm_id(options.comm_id))
        while True:
            self._check_cancelled(token)
            # JiHua's working auto-test detects the drive with function 0x08.
            # Requiring a 0x0D parameter response here rejects firmware that
            # can already accept the controlword and speed write frames.
            if self.service.echo(request, timeout_s=0.08):
                return
            self._sleep(options.probe_interval_seconds, token)

    def _start_position_motion(
        self,
        comm_id: int,
        items: list[bytes],
        start_controlword: int,
        token: CancellationToken,
    ) -> None:
        station = station_from_comm_id(comm_id)
        self._write_items(station, items, token)
        self._sleep(0.1, token)
        self._write_items(station, [controlword_item(CONTROLWORD_BRAKE_RELEASE)], token)
        self._sleep(0.1, token)
        self._write_items(station, [controlword_item(start_controlword)], token)

    def _read_one(self, comm_id: int, parameter, *, timeout_s: float) -> ParameterReadItem:
        try:
            items = self.service.read_parameters(
                station_from_comm_id(comm_id),
                [parameter],
                timeout_s=timeout_s,
            )
        except Exception as exc:
            raise Serial485ControllerError(str(exc)) from exc
        if len(items) != 1 or items[0].address != parameter.address or items[0].value is None:
            raise Serial485ControllerError(
                f"invalid parameter-read result for 0x{parameter.address:06X}"
            )
        return items[0]

    @staticmethod
    def _comm_id_from_read_value(register_value: int | None) -> int:
        if register_value is None:
            raise Serial485ControllerError("communication ID parameter was not returned")
        # 0x200711 is written as 0x01xx. Production JiHua drives have been
        # observed returning either that encoded value or the plain 0x00xx ID.
        if 0 <= register_value <= 0xFF:
            return register_value
        if register_value & 0xFF00 == 0x0100:
            return register_value & 0xFF
        raise Serial485ControllerError(
            f"communication ID register has unsupported value 0x{register_value:04X}"
        )

    @staticmethod
    def _validate_counts_per_revolution(counts_per_revolution: int) -> None:
        if not 1 <= counts_per_revolution <= 0x7FFFFFFF:
            raise ValueError("counts_per_revolution must be in 1..0x7FFFFFFF")

    @classmethod
    def _rad_to_counts(cls, value: float, counts_per_revolution: int) -> int:
        cls._validate_counts_per_revolution(counts_per_revolution)
        if not math.isfinite(value):
            raise ValueError("motion values must be finite")
        return round(value * counts_per_revolution / math.tau)

    @classmethod
    def _position_counts(cls, radians: float, counts_per_revolution: int) -> int:
        counts = cls._rad_to_counts(radians, counts_per_revolution)
        if not -(1 << 31) <= counts < (1 << 31):
            raise ValueError("position exceeds the protocol INT32 range")
        return counts

    @classmethod
    def _motion_counts(
        cls,
        *,
        rad_s: float,
        accel_rad_s2: float,
        decel_rad_s2: float,
        counts_per_revolution: int,
    ) -> tuple[int, int, int]:
        if accel_rad_s2 <= 0 or decel_rad_s2 <= 0:
            raise ValueError("acceleration and deceleration must be positive")
        velocity = cls._rad_to_counts(rad_s, counts_per_revolution)
        acceleration = cls._rad_to_counts(accel_rad_s2, counts_per_revolution)
        deceleration = cls._rad_to_counts(decel_rad_s2, counts_per_revolution)
        if not -(1 << 31) <= velocity < (1 << 31):
            raise ValueError("velocity exceeds the protocol INT32 range")
        if not 1 <= acceleration <= 0xFFFFFFFF:
            raise ValueError("acceleration exceeds the protocol UINT32 range")
        if not 1 <= deceleration <= 0xFFFFFFFF:
            raise ValueError("deceleration exceeds the protocol UINT32 range")
        return velocity, acceleration, deceleration

    def _set_controlword(self, comm_id: int, value: int, token: CancellationToken) -> None:
        self._write_items(station_from_comm_id(comm_id), [controlword_item(value)], token)

    def _set_speed_value(
        self,
        comm_id: int,
        velocity: int,
        acceleration: int,
        deceleration: int,
        token: CancellationToken,
    ) -> None:
        self._write_items(
            station_from_comm_id(comm_id),
            speed_mode_items(
                velocity,
                acceleration_counts_s2=acceleration,
                deceleration_counts_s2=deceleration,
            ),
            token,
        )

    def _speed_stop(self, comm_id: int, acceleration: int, deceleration: int) -> None:
        station = station_from_comm_id(comm_id)
        self.service.write(
            build_0e_write_multi(
                station=station,
                items=speed_mode_items(
                    SPEED_STOP,
                    acceleration_counts_s2=acceleration,
                    deceleration_counts_s2=deceleration,
                ),
            )
        )

    def _finish_with_safety_stop(
        self,
        comm_id: int,
        primary_failure: BaseException | None,
        operation: str,
    ) -> None:
        try:
            self._safety_stop(comm_id)
        except Exception as stop_error:
            self.on_log(f"{operation}安全停止失败: {stop_error}")
            if primary_failure is None:
                raise Serial485SafetyStopError(f"{operation}安全停止失败: {stop_error}") from stop_error

    def _safety_stop(self, comm_id: int) -> None:
        """Best-effort stop that deliberately ignores cancellation state."""
        station = station_from_comm_id(comm_id)
        errors: list[Exception] = []
        acceleration = self._rad_to_counts(DEFAULT_ACCEL_RAD_S2, D7_COUNTS_PER_REVOLUTION)
        for items in (
            speed_mode_items(
                SPEED_STOP,
                acceleration_counts_s2=acceleration,
                deceleration_counts_s2=acceleration,
            ),
            [controlword_item(CONTROLWORD_STOP_POSITION)],
        ):
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

    @staticmethod
    def _run_stage(stage: str, action: Callable[[], _T]) -> _T:
        try:
            return action()
        except OperationCancelled:
            raise
        except BaseException as exc:
            raise Serial485StageError(stage, exc) from exc

    def _begin_operation(self, token: CancellationToken | None) -> CancellationToken:
        self._internal_token = CancellationToken()
        return token or self._internal_token

    def _check_cancelled(self, token: CancellationToken) -> None:
        token.raise_if_cancelled()
        if token is not self._internal_token:
            self._internal_token.raise_if_cancelled()

    def _sleep(self, seconds: float, token: CancellationToken) -> None:
        deadline = self._clock() + max(seconds, 0)
        while self._clock() < deadline:
            self._check_cancelled(token)
            self._sleep_impl(min(0.05, max(0.0, deadline - self._clock())))
        self._check_cancelled(token)
