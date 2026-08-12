from __future__ import annotations

import math
from collections import deque

import pytest

from d7_factory_studio.core.ports import CancellationToken, OperationCancelled
from d7_factory_studio.features.serial485.controller import (
    CycleOptions,
    Serial485Controller,
    Serial485ControllerError,
    Serial485StageError,
)
from d7_factory_studio.features.serial485.service import (
    Serial485Config,
    Serial485Error,
    Serial485Service,
)
from d7_factory_studio.protocols.serial485 import (
    ABS_ENCODER_OFFSET_ADDRESS,
    ACTUAL_POSITION_ADDRESS,
    COMM_ID_ADDRESS,
    COMM_ID_PARAMETER,
    CONTROL_AUTHORITY_ADDRESS,
    CONTROL_AUTHORITY_PARAMETER,
    CONTROLWORD_BRAKE_ON,
    CONTROLWORD_BRAKE_RELEASE,
    CONTROLWORD_ENABLE,
    CONTROLWORD_STOP_POSITION,
    D7_COUNTS_PER_REVOLUTION,
    DEVICE_ID_ADDRESS,
    DEVICE_NAME_ADDRESS,
    HARDWARE_VERSION_ADDRESS,
    MOTOR_IDENTIFICATION_STATE_ADDRESS,
    MOTOR_PHASE_SEQUENCE_ADDRESS,
    SERVO_STATUS_ADDRESS,
    SOFTWARE_VERSION_ADDRESS,
    ParameterReadItem,
    absolute_position_mode_items,
    build_0d_read_multi,
    build_0e_write_multi,
    build_aa55_frame,
    build_echo,
    comm_id_write_item,
    control_authority_item,
    controlword_item,
    eeprom_save_item,
    motor_identification_control_item,
    speed_mode_items,
    system_reset_item,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeSerialService:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.write_times: list[float] = []
        self.reads: list[tuple[float, int, tuple[int, ...]]] = []
        self.echo_station = 0
        self.echo_stations: deque[int] = deque()
        self.echo_calls = 0
        self.token_to_cancel: CancellationToken | None = None
        self.cancel_at_write: int | None = None
        self.fail_at_write: int | None = None
        self.clock = FakeClock()
        self.parameter_results: deque[dict[int, int | str]] = deque()

    def echo(self, request: bytes, *, timeout_s: float = 0.5) -> bool:
        self.echo_calls += 1
        expected = self.echo_stations.popleft() if self.echo_stations else self.echo_station
        return request[3] == expected

    def write(self, frame: bytes, *, clear_input: bool = True) -> None:
        if self.fail_at_write == len(self.writes) + 1:
            raise RuntimeError("injected write failure")
        self.writes.append(frame)
        self.write_times.append(self.clock())
        if self.cancel_at_write == len(self.writes) and self.token_to_cancel is not None:
            self.token_to_cancel.cancel()

    def read_parameters(self, station, parameters, *, timeout_s=0.5):
        specs = tuple(parameters)
        self.reads.append((self.clock(), station, tuple(spec.address for spec in specs)))
        values = self.parameter_results.popleft() if self.parameter_results else {}
        items = []
        for spec in specs:
            if spec.length_flag == 0:
                value = str(values.get(spec.address, f"V{spec.address:06X}"))
                raw = value.encode("ascii")
                items.append(ParameterReadItem(spec.address, len(raw), raw, value))
                continue
            default = 0x0100 | ((station + 1) & 0xFF) if spec.address == COMM_ID_ADDRESS else 0
            value = values.get(spec.address, default)
            width = {0x08: 1, 0x10: 2, 0x20: 4}[spec.length_flag]
            raw = int(value).to_bytes(width, "little", signed=spec.signed)
            items.append(ParameterReadItem(spec.address, spec.length_flag, raw, value))
        return tuple(items)


def make_controller(service: FakeSerialService) -> Serial485Controller:
    return Serial485Controller(service, sleeper=service.clock.sleep, clock=service.clock)


def test_enable_servo_runs_full_jihua_sequence_and_verifies_status() -> None:
    service = FakeSerialService()
    service.parameter_results.extend(
        [
            {SERVO_STATUS_ADDRESS: 0x1231},
            {SERVO_STATUS_ADDRESS: 0x1237},
        ]
    )

    statusword = make_controller(service).enable_servo(0x21)

    assert statusword == 0x1237
    assert service.writes == [
        build_0e_write_multi(station=0x20, items=[controlword_item(CONTROLWORD_ENABLE)]),
        build_0e_write_multi(station=0x20, items=[controlword_item(CONTROLWORD_BRAKE_ON)]),
        build_0e_write_multi(station=0x20, items=[controlword_item(CONTROLWORD_BRAKE_RELEASE)]),
    ]
    assert service.write_times == pytest.approx([0.0, 0.3, 0.6])


def test_enable_servo_fails_when_status_remains_disabled() -> None:
    service = FakeSerialService()
    service.parameter_results.extend(
        [{SERVO_STATUS_ADDRESS: 0x1250} for _attempt in range(9)]
    )

    with pytest.raises(Serial485ControllerError, match="0x1250.*低 7 位为 0x37"):
        make_controller(service).enable_servo(0x21)

    assert service.writes == [
        build_0e_write_multi(station=0x20, items=[controlword_item(value)])
        for value in (
            CONTROLWORD_ENABLE,
            CONTROLWORD_BRAKE_ON,
            CONTROLWORD_BRAKE_RELEASE,
            CONTROLWORD_ENABLE,
            CONTROLWORD_BRAKE_RELEASE,
        )
    ]


def test_enable_servo_falls_back_to_working_quick_tool_sequence() -> None:
    service = FakeSerialService()
    service.parameter_results.extend(
        [
            *({SERVO_STATUS_ADDRESS: 0x1240} for _attempt in range(3)),
            {SERVO_STATUS_ADDRESS: 0x1237},
        ]
    )

    statusword = make_controller(service).enable_servo(0x21)

    assert statusword == 0x1237
    assert service.writes == [
        *[
            build_0e_write_multi(station=0x20, items=[controlword_item(value)])
            for value in (CONTROLWORD_ENABLE, CONTROLWORD_BRAKE_ON, CONTROLWORD_BRAKE_RELEASE)
        ],
        *[
            build_0e_write_multi(station=0x20, items=[controlword_item(value)])
            for value in (CONTROLWORD_ENABLE, CONTROLWORD_BRAKE_RELEASE)
        ],
    ]


def test_enable_servo_accepts_unavailable_status_readback_like_quick_tool() -> None:
    class WriteOnlyService(FakeSerialService):
        def read_parameters(self, station, parameters, *, timeout_s=0.5):
            raise Serial485Error("status read is unsupported")

    service = WriteOnlyService()

    assert make_controller(service).enable_servo(0x21) is None
    assert service.writes == [
        build_0e_write_multi(station=0x20, items=[controlword_item(value)])
        for value in (CONTROLWORD_ENABLE, CONTROLWORD_BRAKE_ON, CONTROLWORD_BRAKE_RELEASE)
    ]


def test_scan_maps_wire_station_to_communication_id() -> None:
    service = FakeSerialService()
    service.echo_station = 0x10
    result = make_controller(service).scan(probe_timeout_s=0)
    assert result is not None
    assert result.station == 0x10 and result.comm_id == 0x11


def test_cycle_finally_sends_both_stop_commands_after_cancellation() -> None:
    service = FakeSerialService()
    token = CancellationToken()
    service.token_to_cancel = token
    service.cancel_at_write = 3  # enable, brake release, forward speed
    controller = make_controller(service)

    with pytest.raises(OperationCancelled):
        controller.run_cycle(CycleOptions(cycles=1, run_seconds=0), token)

    acceleration = round(D7_COUNTS_PER_REVOLUTION / math.tau)
    expected_speed_stop = build_0e_write_multi(
        station=0,
        items=speed_mode_items(
            0,
            acceleration_counts_s2=acceleration,
            deceleration_counts_s2=acceleration,
        ),
    )
    expected_position_stop = build_0e_write_multi(
        station=0, items=[controlword_item(CONTROLWORD_STOP_POSITION)]
    )
    assert service.writes[-2:] == [expected_speed_stop, expected_position_stop]


def test_cycle_presence_detection_uses_jihua_echo_instead_of_parameter_read() -> None:
    service = FakeSerialService()
    token = CancellationToken()
    service.token_to_cancel = token
    service.cancel_at_write = 1

    with pytest.raises(OperationCancelled):
        make_controller(service).run_cycle(CycleOptions(cycles=1, run_seconds=0), token)

    assert service.echo_calls == 1
    assert service.reads == []


def test_485_control_authority_can_be_taken_and_released() -> None:
    service = FakeSerialService()
    service.parameter_results.extend(
        [
            {CONTROL_AUTHORITY_ADDRESS: 0},
            {CONTROL_AUTHORITY_ADDRESS: 1},
        ]
    )
    controller = make_controller(service)
    controller.take_control_authority(1)
    controller.release_control_authority(1)
    expected = [
        build_0e_write_multi(station=0, items=[control_authority_item(0)]),
        build_0e_write_multi(station=0, items=[eeprom_save_item()]),
        build_0e_write_multi(station=0, items=[system_reset_item()]),
        build_0e_write_multi(station=0, items=[control_authority_item(1)]),
        build_0e_write_multi(station=0, items=[eeprom_save_item()]),
        build_0e_write_multi(station=0, items=[system_reset_item()]),
    ]
    assert service.writes == expected
    assert service.write_times == pytest.approx([0.0, 0.15, 0.8, 2.8, 2.95, 3.6])
    assert service.reads == [
        (2.8, 0, (CONTROL_AUTHORITY_ADDRESS,)),
        (5.6, 0, (CONTROL_AUTHORITY_ADDRESS,)),
    ]


def test_control_authority_does_not_fail_only_because_reboot_readback_is_late() -> None:
    class RebootingService(FakeSerialService):
        def read_parameters(self, station, parameters, *, timeout_s=0.5):
            raise Serial485Error("drive is rebooting")

    service = RebootingService()

    make_controller(service).take_control_authority(1)

    assert service.writes == [
        build_0e_write_multi(station=0, items=[control_authority_item(0)]),
        build_0e_write_multi(station=0, items=[eeprom_save_item()]),
        build_0e_write_multi(station=0, items=[system_reset_item()]),
    ]


def test_probe_and_identity_use_real_0d_parameter_reads() -> None:
    service = FakeSerialService()
    identity = make_controller(service).probe(0x11, attempts=1)
    assert identity is not None
    assert identity.comm_id == 0x11 and identity.register_value == 0x0111
    assert service.reads == [(0.0, 0x10, (COMM_ID_ADDRESS,))]
    assert service.echo_calls == 0


def test_identity_accepts_plain_comm_id_returned_by_production_drive() -> None:
    service = FakeSerialService()
    service.parameter_results.append({COMM_ID_ADDRESS: 0x0011})

    identity = make_controller(service).read_identity(0x11)

    assert identity.comm_id == 0x11
    assert identity.register_value == 0x0011


def test_connect_uses_jihua_echo_then_reads_identity_when_supported() -> None:
    service = FakeSerialService()
    service.echo_station = 0x01
    connection = make_controller(service).connect(0x02)

    assert connection.comm_id == 0x02
    assert connection.station == 0x01
    assert connection.attempts == 1
    assert connection.echo_ok is True
    assert connection.register_value == 0x0102
    assert dict(connection.parameter_values)[COMM_ID_ADDRESS] == 0x0102
    assert dict(connection.parameter_values)[CONTROL_AUTHORITY_ADDRESS] == 0
    assert connection.parameter_error == ""
    assert service.echo_calls == 1
    assert service.reads == [
        (0.0, 0x01, (COMM_ID_ADDRESS,)),
        (0.0, 0x01, (DEVICE_ID_ADDRESS,)),
        (0.0, 0x01, (CONTROL_AUTHORITY_ADDRESS,)),
        (0.0, 0x01, (SERVO_STATUS_ADDRESS,)),
        (0.0, 0x01, (ACTUAL_POSITION_ADDRESS,)),
        (0.0, 0x01, (DEVICE_NAME_ADDRESS,)),
        (0.0, 0x01, (HARDWARE_VERSION_ADDRESS,)),
        (0.0, 0x01, (SOFTWARE_VERSION_ADDRESS,)),
    ]


def test_write_comm_id_uses_required_read_write_wait_reset_wait_read_sequence() -> None:
    service = FakeSerialService()
    service.echo_stations.extend([0, 0x10])
    service.parameter_results.append({COMM_ID_ADDRESS: 0x0111})
    identity = make_controller(service).write_comm_id(0x11)

    assert identity.comm_id == 0x11
    assert service.writes == [
        build_0e_write_multi(station=0, items=[comm_id_write_item(0x11)]),
        build_0e_write_multi(station=0, items=[eeprom_save_item()]),
        build_0e_write_multi(station=0, items=[system_reset_item()]),
    ]
    assert service.write_times == pytest.approx([0.0, 0.0, 2.0])
    assert service.echo_calls == 2
    assert service.reads[0][0] == pytest.approx(4.0)
    assert service.reads[0][1:] == (0x10, (COMM_ID_ADDRESS,))


def test_write_comm_id_reports_failing_stage_and_preserves_raw_config_value() -> None:
    service = FakeSerialService()
    service.fail_at_write = 2
    with pytest.raises(Serial485StageError, match=r"\[save_eeprom\]") as write_error:
        make_controller(service).write_comm_id(0x11)
    assert write_error.value.stage == "save_eeprom"

    service = FakeSerialService()
    service.echo_stations.extend([0, 0x10])
    service.parameter_results.append({COMM_ID_ADDRESS: 0x0112})
    result = make_controller(service).write_comm_id(0x11)
    assert result.comm_id == 0x11
    assert result.register_value == 0x0112


def test_wheel_phase_identification_runs_both_original_stages_and_stops() -> None:
    service = FakeSerialService()
    service.parameter_results.extend(
        [
            {SERVO_STATUS_ADDRESS: 0},
            {MOTOR_IDENTIFICATION_STATE_ADDRESS: 32},
            {MOTOR_IDENTIFICATION_STATE_ADDRESS: 33},
            {MOTOR_IDENTIFICATION_STATE_ADDRESS: 34},
            {MOTOR_PHASE_SEQUENCE_ADDRESS: 1},
            {MOTOR_IDENTIFICATION_STATE_ADDRESS: 64},
            {MOTOR_IDENTIFICATION_STATE_ADDRESS: 65},
            {MOTOR_IDENTIFICATION_STATE_ADDRESS: 66},
            {ABS_ENCODER_OFFSET_ADDRESS: 0x12345678},
        ]
    )
    result = make_controller(service).identify_wheel_phases(0x52)

    assert result.phase_sequence == 1
    assert result.encoder_offset == 0x12345678
    assert service.writes == [
        build_0e_write_multi(station=0x51, items=[motor_identification_control_item(2)]),
        build_0e_write_multi(station=0x51, items=[motor_identification_control_item(4)]),
        build_0e_write_multi(station=0x51, items=[motor_identification_control_item(0)]),
    ]
    assert service.clock.now == pytest.approx(4.0)


def test_wheel_phase_identification_failure_still_sends_stop() -> None:
    service = FakeSerialService()
    service.parameter_results.extend(
        [
            {SERVO_STATUS_ADDRESS: 0},
            {MOTOR_IDENTIFICATION_STATE_ADDRESS: 47},
        ]
    )
    with pytest.raises(Exception, match="动力线相序辨识失败"):
        make_controller(service).identify_wheel_phases(0x52)
    assert service.writes[-1] == build_0e_write_multi(
        station=0x51,
        items=[motor_identification_control_item(0)],
    )


def test_timed_velocity_converts_rad_units_and_finally_stops_on_cancellation() -> None:
    service = FakeSerialService()
    token = CancellationToken()
    service.token_to_cancel = token
    service.cancel_at_write = 1
    controller = make_controller(service)

    with pytest.raises(OperationCancelled):
        controller.set_speed(
            1,
            0.5,
            token,
            motion_time_s=5,
            accel_rad_s2=1,
            decel_rad_s2=2,
        )

    velocity = round(0.5 * D7_COUNTS_PER_REVOLUTION / math.tau)
    acceleration = round(D7_COUNTS_PER_REVOLUTION / math.tau)
    deceleration = round(2 * D7_COUNTS_PER_REVOLUTION / math.tau)
    expected_run = build_0e_write_multi(
        station=0,
        items=speed_mode_items(
            velocity,
            acceleration_counts_s2=acceleration,
            deceleration_counts_s2=deceleration,
        ),
    )
    expected_stop = build_0e_write_multi(
        station=0,
        items=speed_mode_items(
            0,
            acceleration_counts_s2=acceleration,
            deceleration_counts_s2=deceleration,
        ),
    )
    assert service.writes == [expected_run, expected_stop]


def test_velocity_uses_same_24_bit_motion_units_as_working_quick_config_tool() -> None:
    service = FakeSerialService()
    controller = make_controller(service)
    five_rpm_rad_s = 5 * math.tau / 60

    controller.set_speed(
        0x21,
        five_rpm_rad_s,
        accel_rad_s2=five_rpm_rad_s,
        decel_rad_s2=five_rpm_rad_s,
    )

    assert D7_COUNTS_PER_REVOLUTION == 16_777_216
    assert service.writes == [
        build_0e_write_multi(
            station=0x20,
            items=speed_mode_items(
                0x00155555,
                acceleration_counts_s2=0x00155555,
                deceleration_counts_s2=0x00155555,
            ),
        ),
    ]


def test_timed_velocity_rejects_zero_instead_of_reporting_successful_motion() -> None:
    with pytest.raises(ValueError, match="目标速度为 0"):
        make_controller(FakeSerialService()).set_speed(0x21, 0.0, duration_s=2.0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"rad_s": float("nan")}, "finite"),
        ({"rad_s": 1.0, "accel_rad_s2": 0}, "positive"),
        ({"rad_s": 1.0, "motion_time_s": -1}, "motion time"),
    ],
)
def test_velocity_rejects_values_outside_explicit_conversion_range(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        make_controller(FakeSerialService()).set_speed(1, **kwargs)


def test_absolute_position_read_and_absolute_relative_motion_interfaces() -> None:
    service = FakeSerialService()
    service.parameter_results.append({ACTUAL_POSITION_ADDRESS: D7_COUNTS_PER_REVOLUTION // 2})
    controller = make_controller(service)
    position = controller.read_absolute_position(1)
    assert position.counts == D7_COUNTS_PER_REVOLUTION // 2
    assert position.radians == pytest.approx(math.pi)

    controller.move_absolute(1, position_rad=math.pi / 2, speed_rad_s=0.5)
    target = D7_COUNTS_PER_REVOLUTION // 4
    velocity = round(0.5 * D7_COUNTS_PER_REVOLUTION / math.tau)
    acceleration = round(D7_COUNTS_PER_REVOLUTION / math.tau)
    assert service.writes[-3] == build_0e_write_multi(
        station=0,
        items=absolute_position_mode_items(
            target_counts=target,
            profile_velocity_counts_s=velocity,
            acceleration_counts_s2=acceleration,
            deceleration_counts_s2=acceleration,
        ),
    )
    assert service.writes[-1] == build_0e_write_multi(
        station=0,
        items=[controlword_item(0x003F)],
    )

    controller.move_relative_angle(1, angle_deg=-90, speed_rad_s=0.5)
    assert bytes.fromhex("20 00 7A 60 00 00 C0 FF") in service.writes[-3]
    assert service.writes[-1] == build_0e_write_multi(
        station=0,
        items=[controlword_item(0x007F)],
    )


class FakePort:
    def __init__(self, payload: bytes, **_kwargs: object) -> None:
        self.payload = payload
        self.is_open = True
        self.timeout = 0.1

    @property
    def in_waiting(self) -> int:
        return len(self.payload)

    def read(self, _size: int) -> bytes:
        payload, self.payload = self.payload, b""
        return payload

    def close(self) -> None:
        self.is_open = False


def test_serial_service_preserves_multiple_verified_frames_from_one_read() -> None:
    first = build_echo(station=1)
    second = build_echo(station=2)
    service = Serial485Service(
        Serial485Config("COM_TEST"),
        serial_factory=lambda **kwargs: FakePort(first + second, **kwargs),
    )
    service.open()
    assert service.read_frame(0.1).raw == first
    assert service.read_frame(0.1).raw == second


def test_serial_service_closes_partial_port_when_open_setup_fails() -> None:
    class ResetFailurePort:
        is_open = True

        def __init__(self) -> None:
            self.closed = False

        def reset_input_buffer(self) -> None:
            raise OSError("reset failed")

        def close(self) -> None:
            self.closed = True
            self.is_open = False

    port = ResetFailurePort()
    service = Serial485Service(
        Serial485Config("COM_TEST"), serial_factory=lambda **_kwargs: port
    )

    with pytest.raises(Serial485Error, match="reset failed"):
        service.open()

    assert port.closed is True
    assert service.is_open is False


class FakeExchangePort(FakePort):
    def __init__(self, response: bytes, **kwargs: object) -> None:
        super().__init__(b"", **kwargs)
        self.response = response
        self.written = b""

    def reset_input_buffer(self) -> None:
        self.payload = b""

    def write(self, frame: bytes) -> int:
        self.written = bytes(frame)
        self.payload = self.response
        return len(frame)

    def flush(self) -> None:
        pass


class TimeoutSensitiveExchangePort(FakeExchangePort):
    def __init__(self, response: bytes, **kwargs: object) -> None:
        self._timeout = 0.1
        self._tx_started = False
        super().__init__(response, **kwargs)

    @property
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        if self._tx_started:
            raise RuntimeError("COM timeout changed after transmission")
        self._timeout = value

    def write(self, frame: bytes) -> int:
        result = super().write(frame)
        self._tx_started = True
        return result


def test_service_does_not_reconfigure_windows_com_timeout_after_transmit() -> None:
    response = build_aa55_frame(
        station=0,
        function=0x0D,
        data=bytes.fromhex("01 00 10 01 02 20 00 00"),
    )
    port = TimeoutSensitiveExchangePort(response)
    service = Serial485Service(
        Serial485Config("COM_TEST"),
        serial_factory=lambda **_kwargs: port,
    )
    service.open()

    items = service.read_parameters(0, [CONTROL_AUTHORITY_PARAMETER], timeout_s=0.1)

    assert items[0].value == 0


def test_service_0d_read_keeps_unrelated_frame_from_sticky_packet() -> None:
    unrelated = build_echo(station=2)
    response = build_aa55_frame(
        station=0,
        function=0x0D,
        data=bytes.fromhex("01 00 10 11 07 20 01 01"),
    )
    port = FakeExchangePort(unrelated + response)
    service = Serial485Service(
        Serial485Config("COM_TEST"),
        serial_factory=lambda **_kwargs: port,
    )
    service.open()

    items = service.read_parameters(0, [COMM_ID_PARAMETER], timeout_s=0.1)
    assert items[0].value == 0x0101
    assert port.written == build_0d_read_multi(station=0, parameters=[COMM_ID_PARAMETER])
    assert service.read_frame(0.1).raw == unrelated
