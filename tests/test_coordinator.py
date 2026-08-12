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
from d7_factory_studio.protocols.pace_bms_upgrade import PaceBmsUpgradeProtocol
from d7_factory_studio.transports.orin_agent import AgentCanTransport


class FakeSettings:
    def __init__(self, tmp_path: Path) -> None:
        self.values: dict[str, object] = {}
        self.report_directory = tmp_path / "reports"
        self.known_hosts_path = tmp_path / "known_hosts"

    def value(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set_value(self, key: str, value: object) -> None:
        self.values[key] = value

    def ssh_password(self, _host: str, _username: str) -> str | None:
        return None


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
    def __init__(self) -> None:
        self.requests: list[object] = []

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def execute(self, _request, _token, _on_output=None) -> RemoteCommandResult:  # type: ignore[no-untyped-def]
        self.requests.append(_request)
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
    serial_close_calls: list[bool] = []

    class Tasks:
        def cancel_all(self, *, exclude: set[str] | None = None) -> None:
            calls.append(exclude)

    target.tasks = Tasks()  # type: ignore[assignment]
    target._close_serial = lambda: serial_close_calls.append(True)  # type: ignore[method-assign]
    target._disconnect_operation(CancellationToken(), lambda _value, _message: None)
    assert calls == [{"connection.disconnect"}]
    assert serial_close_calls == []


def test_orin_connection_defaults_to_pudu_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = coordinator(tmp_path)
    target.settings.values.update(
        {
            "ssh/host": "192.168.140.87",
            "ssh/username": "",
            "ssh/fingerprint": "SHA256:fingerprint-from-another-host",
        }
    )
    captured: dict[str, object] = {}

    class Agent:
        def __init__(self, on_event=None) -> None:  # type: ignore[no-untyped-def]
            del on_event

        def connect(
            self,
            host: str,
            port: int,
            username: str,
            password: str,
            fingerprint: str,
            known_hosts_path: Path,
        ) -> None:
            captured.update(
                host=host,
                port=port,
                username=username,
                password=password,
                fingerprint=fingerprint,
                known_hosts_path=known_hosts_path,
            )

        def close(self) -> None:
            pass

        def execute(self, command: str):  # type: ignore[no-untyped-def]
            assert "$HOME" in command
            return 0, "/home/pudu", ""

        def deploy_file(
            self, local_path: Path, remote_path: str, executable: bool = False
        ) -> str:
            captured["agent_deploy"] = (local_path, remote_path, executable)
            return "agent-hash"

        def deploy_bytes(self, content: bytes, remote_path: str) -> str:
            captured["config_deploy"] = (content, remote_path)
            return "config-hash"

        def start(
            self,
            remote_binary: str,
            remote_config: str,
            remote_library_path: str | None = None,
        ) -> dict[str, object]:
            captured["agent_start"] = (
                remote_binary,
                remote_config,
                remote_library_path,
            )
            return {"protocol": "d7-factory-can-agent-jsonl"}

    class Remote:
        def __init__(self, _connection) -> None:  # type: ignore[no-untyped-def]
            pass

        def connect(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("d7_factory_studio.coordinator.OrinAgentClient", Agent)
    monkeypatch.setattr("d7_factory_studio.coordinator.ParamikoRemoteSession", Remote)

    def run_sync(action: str, operation, task_id: str | None = None) -> str:  # type: ignore[no-untyped-def]
        del task_id
        captured["result"] = operation(CancellationToken(), lambda _value, _message: None)
        return action

    target._start = run_sync  # type: ignore[method-assign]
    target._connect({"mode": ConnectionMode.ORIN_REMOTE.value})

    assert captured["username"] == "pudu"
    assert captured["password"] == "pudu"
    assert captured["fingerprint"] == ""
    assert captured["agent_deploy"][2] is True  # type: ignore[index]
    assert captured["agent_start"][0].endswith("d7-factory-can-agent")  # type: ignore[index,union-attr]


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


def test_broadcast_uses_page_selected_interface(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    remote = FakeRemote()
    target._resources = ConnectionResources(
        ConnectionMode.ORIN_REMOTE,
        remote_session=remote,  # type: ignore[arg-type]
    )
    captured: dict[str, object] = {}

    def run_sync(action: str, operation, task_id: str | None = None) -> str:  # type: ignore[no-untyped-def]
        del task_id
        captured[action] = operation(CancellationToken(), lambda _value, _message: None)
        return action

    target._start = run_sync  # type: ignore[method-assign]
    target._handle_diagnostics(
        "diagnostics.broadcast",
        {"interface": "can2", "duration_s": 10},
    )
    result = captured["diagnostics.broadcast"]
    assert isinstance(result, dict)
    assert result["interface"] == "can2"
    assert [request.evidence_label for request in remote.requests] == [
        "setup-can",
        "broadcast-can2",
    ]


def test_node_parameters_prepare_can_and_use_page_selected_interface(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    remote = FakeRemote()
    target._resources = ConnectionResources(
        ConnectionMode.ORIN_REMOTE,
        remote_session=remote,  # type: ignore[arg-type]
    )
    captured: dict[str, object] = {}

    def run_sync(action: str, operation, task_id: str | None = None) -> str:  # type: ignore[no-untyped-def]
        del task_id
        captured[action] = operation(CancellationToken(), lambda _value, _message: None)
        return action

    target._start = run_sync  # type: ignore[method-assign]
    target._handle_diagnostics(
        "diagnostics.node_param_read",
        {"interface": "can0", "logic_ids": [7]},
    )

    result = captured["diagnostics.node_param_read"]
    assert isinstance(result, dict)
    assert [node["logic_id"] for node in result["nodes"]] == [7]
    assert [request.evidence_label for request in remote.requests] == [
        "setup-can",
        "node-read-7",
    ]


def test_serial_open_failure_is_transactional_and_same_port_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = coordinator(tmp_path)
    instances: list[object] = []

    class Service:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            self.config = config
            self.is_open = False
            self.closed = False
            instances.append(self)

        def open(self) -> None:
            if len(instances) == 1:
                raise OSError("port unavailable")
            self.is_open = True

        def close(self) -> None:
            self.closed = True
            self.is_open = False

    monkeypatch.setattr("d7_factory_studio.coordinator.Serial485Service", Service)

    with pytest.raises(OSError, match="port unavailable"):
        target._open_serial({"port": "COM13", "baud": 115200})
    assert target._serial_service is None
    assert target._serial_controller is None
    assert instances[0].closed is True  # type: ignore[attr-defined]

    controller = target._open_serial({"port": "COM13", "baud": 115200})
    assert controller.service is instances[1]
    assert instances[1].is_open is True  # type: ignore[attr-defined]


def test_diagnostic_loop_summary_uses_worst_round_verdict(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    summary = target._diagnostic_loop_summary(
        ("can0",),
        [
            {
                "round": 1,
                "verdict": "PASS",
                "result": {
                    "stages": [],
                    "execution": {
                        "status": "complete",
                        "planned_stage_count": 3,
                        "executed_stage_count": 3,
                    },
                },
                "bundle": {},
            },
            {
                "round": 2,
                "verdict": "FAIL",
                "result": {
                    "stages": [],
                    "execution": {
                        "status": "not_started",
                        "planned_stage_count": 3,
                        "executed_stage_count": 0,
                    },
                },
                "bundle": {},
            },
        ],
    )

    assert summary["round_count"] == 2
    assert summary["evaluation"]["verdict"] == "FAIL"
    assert summary["execution"] == {
        "status": "partial",
        "planned_stage_count": 6,
        "executed_stage_count": 3,
    }


def test_diagnostic_loop_raw_artifacts_are_namespaced_by_round(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    artifacts = target._diagnostic_loop_raw_artifacts(
        [
            {
                "round": 2,
                "result": {
                    "stages": [],
                    "commands": [
                        {"label": "required-tools", "stdout": "ok", "stderr": ""}
                    ],
                },
            }
        ]
    )

    assert artifacts["rounds/002/commands/01-required-tools.stdout.log"] == "ok"


def test_motor_write_is_rejected_while_safety_locked(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    target._resources.motor_service = object()  # type: ignore[assignment]
    with pytest.raises(PermissionError, match="安全锁"):
        target._handle_motor("motor.set_velocity", {"target": {"motor": 1}})


def test_local_can_background_fault_relocks_and_notifies_ui(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    target.state.safety_locked = False
    events: list[tuple[str, str, object]] = []
    target.state.task_event.connect(
        lambda action, event, payload: events.append((action, event, payload))
    )

    target._handle_local_can_fault("位置心跳发送失败，已双发失能")

    assert target.state.safety_locked is True
    assert events == [
        (
            "motor.local_can_safety",
            "failed",
            {"error": "位置心跳发送失败，已双发失能"},
        )
    ]


def test_require_can_restarts_dead_orin_agent_and_resubscribes_bus(tmp_path: Path) -> None:
    target = coordinator(tmp_path)

    class Agent:
        def __init__(self) -> None:
            self.is_running = False
            self.restart_calls = 0
            self.requests: list[tuple[str, object]] = []
            self.listeners: list[object] = []

        def restart(self) -> dict[str, object]:
            self.restart_calls += 1
            self.is_running = True
            return {"protocol": "d7-factory-can-agent-jsonl"}

        def request(self, operation: str, *, args=None, **_kwargs):  # type: ignore[no-untyped-def]
            self.requests.append((operation, args))
            return {}

        def add_event_listener(self, listener) -> None:  # type: ignore[no-untyped-def]
            self.listeners.append(listener)

        def remove_event_listener(self, listener) -> None:  # type: ignore[no-untyped-def]
            if listener in self.listeners:
                self.listeners.remove(listener)

    agent = Agent()
    stale = AgentCanTransport(agent, "can1")  # type: ignore[arg-type]
    stale._open = True
    target._resources = ConnectionResources(
        ConnectionMode.ORIN_REMOTE,
        agent_client=agent,  # type: ignore[arg-type]
        can_transport=stale,
    )

    recovered = target._require_can("can1")

    assert agent.restart_calls == 1
    assert recovered is not stale
    assert recovered.is_open
    assert agent.requests[-1] == ("can.subscribe", {"bus": "can1"})


def test_require_can_reopens_closed_orin_transport_on_same_bus(tmp_path: Path) -> None:
    target = coordinator(tmp_path)

    class Agent:
        is_running = True

        def __init__(self) -> None:
            self.requests: list[tuple[str, object]] = []
            self.listeners: list[object] = []

        def request(self, operation: str, *, args=None, **_kwargs):  # type: ignore[no-untyped-def]
            self.requests.append((operation, args))
            return {}

        def add_event_listener(self, listener) -> None:  # type: ignore[no-untyped-def]
            self.listeners.append(listener)

        def remove_event_listener(self, listener) -> None:  # type: ignore[no-untyped-def]
            if listener in self.listeners:
                self.listeners.remove(listener)

    agent = Agent()
    transport = AgentCanTransport(agent, "can1")  # type: ignore[arg-type]
    target._resources = ConnectionResources(
        ConnectionMode.ORIN_REMOTE,
        agent_client=agent,  # type: ignore[arg-type]
        can_transport=transport,
    )

    reopened = target._require_can("can1")

    assert reopened is transport
    assert reopened.is_open
    assert agent.requests == [("can.subscribe", {"bus": "can1"})]


def test_local_can_velocity_routes_through_safe_controller(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    transport = FakeCanTransport()
    target._resources = ConnectionResources(ConnectionMode.PC_DIRECT, can_transport=transport)
    captured: dict[str, object] = {}

    class Controller:
        def __init__(self) -> None:
            self.transport = transport

        def run_velocity(
            self,
            device_ids: list[int],
            rad_s: float,
            duration_s: float,
            accel_time_ms: int,
            decel_time_ms: int,
            token: CancellationToken,
        ) -> dict[str, object]:
            captured["velocity"] = (
                device_ids,
                rad_s,
                duration_s,
                accel_time_ms,
                decel_time_ms,
                token,
            )
            return {
                "positions": {device_ids[0]: 0.25},
                "statuses": {device_ids[0]: 0x42},
                "safe_state": "position_trajectory_then_disabled",
            }

    target._local_can_motor_controller = Controller()  # type: ignore[assignment]

    def run_sync(action: str, operation, task_id: str | None = None) -> str:  # type: ignore[no-untyped-def]
        del task_id
        captured["action"] = action
        captured["result"] = operation(CancellationToken(), lambda _value, _message: None)
        return action

    target._start = run_sync  # type: ignore[method-assign]
    target._handle_local_can_motor(
        "motor.set_velocity",
        "set_velocity",
        {"motor": 24},
        {
            "rad_s": 0.5,
            "duration_s": 2.0,
            "accel_rad_s2": 1.0,
            "decel_rad_s2": 0.5,
        },
    )

    assert captured["action"] == "motor.set_velocity"
    assert captured["result"] == {
        "motors": [24],
        "rad_s": 0.5,
        "duration_s": 2.0,
        "positions": {"24": 0.25},
        "statuses": {"24": 0x42},
        "safe_state": "position_trajectory_then_disabled",
    }
    assert captured["velocity"][:5] == ([0x52], 0.5, 2.0, 500, 1000)  # type: ignore[index]


def test_pace_battery_upgrade_routes_to_address_protocol(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    target = coordinator(tmp_path)
    transport = FakeCanTransport()
    target._resources = ConnectionResources(ConnectionMode.PC_DIRECT, can_transport=transport)
    captured: dict[str, object] = {}

    class FakePaceController:
        def __init__(self, actual_transport, protocol, **_callbacks) -> None:  # type: ignore[no-untyped-def]
            captured["transport"] = actual_transport
            captured["protocol"] = protocol

        def upgrade_file(self, path: str, token: CancellationToken) -> dict[str, object]:
            captured["path"] = path
            captured["token"] = token
            return {"identifier": "50194V110", "size": 99_534, "blocks": 778}

    monkeypatch.setattr("d7_factory_studio.coordinator.PaceBatteryUpgradeController", FakePaceController)

    def run_sync(action: str, operation, task_id: str | None = None) -> str:  # type: ignore[no-untyped-def]
        captured["action"] = action
        captured["task_id"] = task_id
        captured["result"] = operation(CancellationToken(), lambda _value, _message: None)
        return task_id or action

    target._start = run_sync  # type: ignore[method-assign]
    target._start_firmware(
        {
            "target": "battery",
            "protocol": "pace_bin",
            "battery_address": 0,
            "firmware": "C50194V110-50195-1.19-001.bin",
        }
    )
    assert captured["transport"] is transport
    assert isinstance(captured["protocol"], PaceBmsUpgradeProtocol)
    assert captured["protocol"].address == 0
    assert captured["action"] == "firmware.start.battery"
    assert captured["task_id"] == "firmware.start:battery"
    assert captured["result"] == {
        "target": "battery",
        "protocol": "pace_bin",
        "identifier": "50194V110",
        "size": 99_534,
        "blocks": 778,
    }


def test_pace_battery_upgrade_rejects_remote_before_agent_lookup(tmp_path: Path) -> None:
    target = coordinator(tmp_path)
    target._resources = ConnectionResources(ConnectionMode.ORIN_REMOTE)
    with pytest.raises(RuntimeError, match="仅支持 PC 直连"):
        target._start_firmware(
            {
                "target": "battery",
                "protocol": "pace_bin",
                "battery_address": 0,
                "firmware": "C50194V110.bin",
            }
        )


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("firmware.query_role", {"target": "pmu", "role": "BOOT"}),
        ("firmware.query_version", {"target": "pmu", "version": "1.12.3"}),
    ],
)
def test_firmware_device_info_queries_route_to_iap_controller(
    tmp_path: Path, monkeypatch, action: str, expected: dict[str, str]
) -> None:  # type: ignore[no-untyped-def]
    target = coordinator(tmp_path)
    target._resources = ConnectionResources(
        ConnectionMode.PC_DIRECT,
        can_transport=FakeCanTransport(),
    )
    captured: dict[str, object] = {}

    class FakeController:
        def __init__(self, _transport, protocol, **_callbacks) -> None:  # type: ignore[no-untyped-def]
            captured["protocol"] = protocol

        def query_role(self, token: CancellationToken) -> str:
            captured["token"] = token
            return "BOOT"

        def query_software_version(self, token: CancellationToken) -> str:
            captured["token"] = token
            return "1.12.3"

    monkeypatch.setattr("d7_factory_studio.coordinator.FirmwareUpgradeController", FakeController)

    def run_sync(event_action: str, operation, task_id: str | None = None) -> str:  # type: ignore[no-untyped-def]
        captured["action"] = event_action
        captured["task_id"] = task_id
        captured["result"] = operation(CancellationToken(), lambda _value, _message: None)
        return task_id or event_action

    target._start = run_sync  # type: ignore[method-assign]
    target._query_firmware(
        action,
        {"target": "pmu", "target_id": 0x18, "iap_id": 0x7FF},
    )
    protocol = captured["protocol"]
    assert protocol.target_id == 0x18
    assert protocol.can_id == 0x7FF
    assert captured["action"] == f"{action}.pmu"
    assert captured["task_id"] == "firmware.query:pmu"
    assert captured["result"] == expected
