from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from d7_factory_studio.app import create_application
from d7_factory_studio.application import ApplicationState
from d7_factory_studio.coordinator import ApplicationCoordinator, ConnectionResources
from d7_factory_studio.core.models import CanFrame, ConnectionMode
from d7_factory_studio.core.ports import CancellationToken, OperationCancelled, RemoteCommandResult


class FakeSettings:
    def __init__(self, tmp_path: Path) -> None:
        self.values: dict[str, object] = {}
        self.report_directory = tmp_path / "reports"
        self.known_hosts_path = tmp_path / "known_hosts"

    def value(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set_value(self, key: str, value: object) -> None:
        self.values[key] = value


class FakeCanTransport:
    def __init__(self) -> None:
        self.frames: list[CanFrame] = []
        self.is_open = True

    def open(self, _channel: int, _mode: object) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def send(self, frame: CanFrame) -> None:
        self.frames.append(frame)

    def receive(self, _timeout_ms: int = 50) -> list[CanFrame]:
        return []


class FakeRemote:
    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def execute(self, _request, _token, _on_output=None) -> RemoteCommandResult:  # type: ignore[no-untyped-def]
        return RemoteCommandResult(0, "", "", 0.0)

    def list_files(self, _remote_root: str) -> list[object]:
        return []

    def download_files(self, _entries, _destination, _token) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
        return []


def coordinator(tmp_path: Path) -> ApplicationCoordinator:
    create_application([])
    return ApplicationCoordinator(ApplicationState(), FakeSettings(tmp_path))  # type: ignore[arg-type]


def test_disconnect_does_not_cancel_its_own_worker(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    calls: list[set[str] | None] = []

    class Tasks:
        def cancel_all(self, *, exclude: set[str] | None = None) -> None:
            calls.append(exclude)

    target.tasks = Tasks()  # type: ignore[assignment]
    target._disconnect_operation(CancellationToken(), lambda _value, _message: None)
    assert calls == [{"connection.disconnect"}]


def test_battery_simulation_sends_original_three_frame_cycle(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    transport = FakeCanTransport()
    target._resources = ConnectionResources(ConnectionMode.PC_DIRECT, can_transport=transport)
    result: dict[str, object] = {}

    def run_once(_action: str, operation, task_id: str | None = None) -> str:  # type: ignore[no-untyped-def]
        del task_id
        token = CancellationToken()

        def report(_progress: int, _message: str) -> None:
            token.cancel()

        with pytest.raises(OperationCancelled):
            operation(token, report)
        result["done"] = True
        return "battery"

    target._start = run_once  # type: ignore[method-assign]
    target._handle_machine(
        "machine.battery_simulation_start",
        {
            "target_ids": [0x41],
            "interval_ms": 1000,
            "voltage_v": 48.5,
            "current_a": -2.25,
            "soc": 65,
        },
    )
    assert result["done"] is True
    assert [frame.data[1] for frame in transport.frames] == [0x02, 0x0E, 0x81]
    assert int.from_bytes(transport.frames[0].data[2:6], "big") == 48_500
    assert int.from_bytes(transport.frames[1].data[2:6], "big", signed=True) == -2_250
    assert int.from_bytes(transport.frames[2].data[2:6], "big") == 65


def test_timing_route_uses_keyword_only_api(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    target._resources = ConnectionResources(
        ConnectionMode.ORIN_REMOTE,
        remote_session=FakeRemote(),  # type: ignore[arg-type]
    )
    captured: dict[str, object] = {}

    def run_sync(action: str, operation, task_id: str | None = None) -> str:  # type: ignore[no-untyped-def]
        del task_id
        captured[action] = operation(CancellationToken(), lambda _value, _message: None)
        return action

    target._start = run_sync  # type: ignore[method-assign]
    target._handle_diagnostics(
        "diagnostics.timing_calculate",
        {"clock_mhz": 40.0, "sample_point": 80.0},
    )
    result = captured["diagnostics.timing_calculate"]
    assert isinstance(result, dict)
    assert result["candidate"].target_bitrate == 1_000_000


def test_motor_write_is_rejected_while_safety_locked(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    target._resources.motor_service = object()  # type: ignore[assignment]
    with pytest.raises(PermissionError, match="安全锁"):
        target._handle_motor("motor.set_velocity", {"target": {"motor": 1}})
