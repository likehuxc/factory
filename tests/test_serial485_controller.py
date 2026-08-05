from __future__ import annotations

import pytest

from d7_factory_studio.core.ports import CancellationToken, OperationCancelled
from d7_factory_studio.features.serial485.controller import CycleOptions, Serial485Controller
from d7_factory_studio.features.serial485.service import Serial485Config, Serial485Service
from d7_factory_studio.protocols.serial485 import (
    CONTROLWORD_STOP_POSITION,
    build_0e_write_multi,
    build_echo,
    control_authority_item,
    controlword_item,
    eeprom_save_item,
    speed_mode_items,
    system_reset_item,
)


class FakeSerialService:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.echo_station = 0
        self.token_to_cancel: CancellationToken | None = None
        self.cancel_at_write: int | None = None

    def echo(self, request: bytes, *, timeout_s: float = 0.5) -> bool:
        return request[3] == self.echo_station

    def write(self, frame: bytes, *, clear_input: bool = True) -> None:
        self.writes.append(frame)
        if self.cancel_at_write == len(self.writes) and self.token_to_cancel is not None:
            self.token_to_cancel.cancel()


def test_scan_maps_wire_station_to_communication_id() -> None:
    service = FakeSerialService()
    service.echo_station = 0x10
    result = Serial485Controller(service, sleeper=lambda _seconds: None).scan(probe_timeout_s=0)
    assert result is not None
    assert result.station == 0x10 and result.comm_id == 0x11


def test_cycle_finally_sends_both_stop_commands_after_cancellation() -> None:
    service = FakeSerialService()
    token = CancellationToken()
    service.token_to_cancel = token
    service.cancel_at_write = 3  # enable, brake release, forward speed
    controller = Serial485Controller(service, sleeper=lambda _seconds: None)

    with pytest.raises(OperationCancelled):
        controller.run_cycle(CycleOptions(cycles=1, run_seconds=0), token)

    expected_speed_stop = build_0e_write_multi(station=0, items=speed_mode_items(0))
    expected_position_stop = build_0e_write_multi(
        station=0, items=[controlword_item(CONTROLWORD_STOP_POSITION)]
    )
    assert service.writes[-2:] == [expected_speed_stop, expected_position_stop]


def test_485_control_authority_can_be_taken_and_released() -> None:
    service = FakeSerialService()
    controller = Serial485Controller(service, sleeper=lambda _seconds: None)
    controller.take_control_authority(1)
    controller.release_control_authority(1)
    expected = [
        build_0e_write_multi(station=0, items=[control_authority_item(1)]),
        build_0e_write_multi(station=0, items=[eeprom_save_item()]),
        build_0e_write_multi(station=0, items=[system_reset_item()]),
        build_0e_write_multi(station=0, items=[control_authority_item(0)]),
        build_0e_write_multi(station=0, items=[eeprom_save_item()]),
        build_0e_write_multi(station=0, items=[system_reset_item()]),
    ]
    assert service.writes == expected


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
