from __future__ import annotations

import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.evt import agent_config_yaml
from d7_factory_studio.core.models import CanFrame, ConnectionMode, LinkState
from d7_factory_studio.core.ports import (
    CancellationToken,
    CanTransport,
    RemoteCommandRequest,
    RemoteFileInfo,
    RemoteSession,
)
from d7_factory_studio.features.device_logs import DeviceLogService, LogTimeFilter
from d7_factory_studio.features.diagnostics import (
    DiagnosticOptions,
    DiagnosticProfile,
    DiagnosticService,
    ParamikoRemoteSession,
    SshConnection,
    diagnostic_raw_artifacts,
)
from d7_factory_studio.features.diagnostics.broadcast import LIVE_RESPONSE_PREFIX
from d7_factory_studio.features.diagnostics.forwarding import (
    AdbNetworkForwarder,
    ForwardConfig,
)
from d7_factory_studio.features.firmware import (
    FirmwareImage,
    FirmwareUpgradeController,
    PaceBatteryUpgradeController,
    UpgradeOptions,
)
from d7_factory_studio.features.firmware.profiles import IapDeviceProfile, battery_profile
from d7_factory_studio.features.motor import LocalCanMotorController, OrinMotorService
from d7_factory_studio.features.serial485 import (
    CycleOptions,
    Serial485Config,
    Serial485Controller,
    Serial485Service,
)
from d7_factory_studio.protocols.can_console import build_battery_frame, build_light_frame
from d7_factory_studio.protocols.iap import IapProtocol
from d7_factory_studio.protocols.machine_info import (
    MACHINE_INFO_FIELDS,
    MACHINE_INFO_SUCCESS,
    build_machine_info_request,
    format_machine_info_value,
    parse_machine_info_response,
)
from d7_factory_studio.protocols.pace_bms_upgrade import PaceBmsUpgradeProtocol
from d7_factory_studio.protocols.serial485 import (
    CONTROLWORD_BRAKE_RELEASE,
    CONTROLWORD_STOP_POSITION,
)
from d7_factory_studio.reports import ReportBundleWriter
from d7_factory_studio.settings_store import SettingsStore, ssh_fingerprint_key
from d7_factory_studio.transports.orin_agent import (
    AgentCanTransport,
    OrinAgentClient,
    UnknownHostKeyError,
)
from d7_factory_studio.transports.zlg import ZlgCanTransport, ZlgTransportConfig, discover_controlcanfd
from d7_factory_studio.ui.tasks import TaskManager


@dataclass(slots=True)
class ConnectionResources:
    mode: ConnectionMode
    can_transport: CanTransport | None = None
    remote_session: RemoteSession | None = None
    agent_client: OrinAgentClient | None = None
    motor_service: OrinMotorService | None = None
    warning: str = ""


class ApplicationCoordinator(QObject):
    agent_event_received = Signal(object)
    local_can_fault_received = Signal(str)

    def __init__(
        self,
        state: ApplicationState,
        settings: SettingsStore,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.settings = settings
        self.tasks = TaskManager(self)
        self._task_actions: dict[str, str] = {}
        self._resources = ConnectionResources(ConnectionMode.PC_DIRECT)
        self._serial_service: Serial485Service | None = None
        self._serial_controller: Serial485Controller | None = None
        self._serial_operation_lock = threading.Lock()
        self._local_can_motor_controller: LocalCanMotorController | None = None
        self._device_log_catalog: dict[str, RemoteFileInfo] = {}
        self._diagnostic_loop_stop = threading.Event()
        self._diagnostic_loop_running = False

        state.action_requested.connect(self.handle)
        self.tasks.progress.connect(self._on_progress)
        self.tasks.succeeded.connect(self._on_succeeded)
        self.tasks.failed.connect(self._on_failed)
        self.tasks.cancelled.connect(self._on_cancelled)
        self.tasks.finished.connect(self._on_finished)
        self.agent_event_received.connect(self._handle_agent_event)
        self.local_can_fault_received.connect(self._handle_local_can_fault)

    @Slot(str, object)
    def handle(self, action: str, payload: object) -> None:
        values = dict(payload) if isinstance(payload, dict) else {}
        try:
            if action == "connection.connect":
                self._connect(values)
            elif action == "connection.disconnect":
                if not self.tasks.is_running("connection.disconnect"):
                    self._start(action, self._disconnect_operation, task_id="connection.disconnect")
            elif action == "application.shutdown":
                self.shutdown()
            elif action == "firmware.start":
                self._start_firmware(values)
            elif action == "firmware.cancel":
                self._cancel_action("firmware.start")
            elif action in {"firmware.query_role", "firmware.query_version"}:
                self._query_firmware(action, values)
            elif action.startswith("serial485."):
                self._handle_serial(action, values)
            elif action.startswith("motor."):
                self._handle_motor(action, values)
            elif action.startswith("diagnostics."):
                self._handle_diagnostics(action, values)
            elif action.startswith("device_logs."):
                self._handle_device_logs(action, values)
            elif action.startswith("machine."):
                self._handle_machine(action, values)
            elif action == "settings.zlg_scan":
                self._start(action, lambda _token, _report: str(discover_controlcanfd()))
            elif action == "settings.ssh_test":
                self._start(action, lambda _token, _report: self._test_ssh(values))
            else:
                raise NotImplementedError(f"尚未注册操作: {action}")
        except Exception as exc:
            self.state.log("任务", f"{action}: {exc}", "error")
            notify_action = action
            if action in {
                "firmware.start",
                "firmware.query_role",
                "firmware.query_version",
            } and values.get("target"):
                notify_action = f"{action}.{values['target']}"
            self.state.notify_task(notify_action, "failed", {"error": str(exc)})

    def _connect(self, payload: dict[str, Any]) -> None:
        if self.tasks.is_running("connection.connect"):
            return
        mode = ConnectionMode(str(payload.get("mode", self.state.connection_mode.value)))
        self.state.set_link_state(LinkState.CONNECTING)

        def operation(token: CancellationToken, report) -> ConnectionResources:
            report(10, "正在检查连接设置")
            token.raise_if_cancelled()
            self._close_resources()
            if mode is ConnectionMode.PC_DIRECT:
                configured_dll = str(self.settings.value("zlg/dll_path", "")).strip()
                config = ZlgTransportConfig(
                    dll_path=configured_dll or None,
                    device_index=int(self.settings.value("zlg/device_index", 0)),
                    arbitration_baudrate=1_000_000,
                    data_baudrate=5_000_000,
                )
                transport = ZlgCanTransport(
                    config,
                    on_trace=lambda message: self.state.log("CAN 帧", message),
                )
                interface = self.state.evt.interfaces[self.state.active_interface]
                report(45, "正在打开 USBCANFD-200U")
                try:
                    channel = int(payload.get("channel", self.settings.value("zlg/channel", 0)))
                    transport.open(channel, interface.mode)
                    token.raise_if_cancelled()
                    report(100, "PC CAN 已连接")
                    return ConnectionResources(mode, can_transport=transport)
                except BaseException:
                    transport.close()
                    raise

            host = str(self.settings.value("ssh/host", "")).strip()
            username = str(self.settings.value("ssh/username", "pudu")).strip() or "pudu"
            port = int(self.settings.value("ssh/port", 22))
            fingerprint = str(
                self.settings.value(ssh_fingerprint_key(host), "")
            ).strip()
            password = self.settings.ssh_password(host, username) or "pudu"
            if not host or not username or not password:
                raise ValueError("请先在设置中填写 Orin 主机、用户名并保存密码")
            client = OrinAgentClient(on_event=self.agent_event_received.emit)
            remote: ParamikoRemoteSession | None = None
            try:
                report(30, "正在验证 Orin SSH 主机指纹")
                client.connect(
                    host,
                    port,
                    username,
                    password,
                    fingerprint,
                    known_hosts_path=self.settings.known_hosts_path,
                )
                token.raise_if_cancelled()
                remote = ParamikoRemoteSession(
                    SshConnection(
                        host=host,
                        username=username,
                        password=password,
                        port=port,
                        known_hosts=self.settings.known_hosts_path,
                    )
                )
                report(55, "正在建立诊断会话")
                remote.connect()
                token.raise_if_cancelled()
                warning = ""
                motor_service: OrinMotorService | None = None
                binary = self._agent_binary_path()
                configured_config = str(self.settings.value("ssh/agent_config", "")).strip()
                config_path = Path(configured_config).expanduser() if configured_config else None
                if binary.is_file():
                    status, home, error = client.execute('printf "%s" "$HOME"')
                    if status != 0 or not home.strip().startswith("/"):
                        raise RuntimeError(error.strip() or "无法确定 Orin 用户目录")
                    remote_root = f"{home.strip()}/.local/share/d7-factory-studio"
                    remote_binary = f"{remote_root}/bin/d7-factory-can-agent"
                    remote_config = f"{remote_root}/config/d7-agent.yaml"
                    report(70, "正在部署独立 SocketCAN Agent")
                    client.deploy_file(binary, remote_binary, executable=True)
                    if config_path is not None:
                        if not config_path.is_file():
                            raise FileNotFoundError(f"自定义 D7 agent YAML 不存在: {config_path}")
                        client.deploy_file(config_path, remote_config)
                    else:
                        client.deploy_bytes(
                            agent_config_yaml(self.state.evt).encode("utf-8"), remote_config
                        )
                    token.raise_if_cancelled()
                    hello = client.start(remote_binary, remote_config)
                    if hello.get("protocol") != "d7-factory-can-agent-jsonl":
                        raise RuntimeError("Orin SocketCAN Agent 协议握手失败")
                else:
                    warning = (
                        "未找到内置或自定义 SocketCAN Agent；"
                        "远程诊断可用，CAN 电机保持禁用"
                    )
                token.raise_if_cancelled()
                report(100, "Orin SSH 已连接")
                return ConnectionResources(
                    mode,
                    remote_session=remote,
                    agent_client=client,
                    motor_service=motor_service,
                    warning=warning,
                )
            except BaseException:
                if remote is not None:
                    remote.close()
                client.close()
                raise

        self._start("connection.connect", operation, task_id="connection.connect")

    def _disconnect_operation(self, _token: CancellationToken, report) -> None:
        report(20, "正在停止任务并关闭连接")
        self.tasks.cancel_all(exclude={"connection.disconnect"})
        self._close_resources()
        report(100, "已断开")

    def _close_resources(self) -> None:
        errors: list[Exception] = []
        if self._local_can_motor_controller is not None:
            try:
                self._local_can_motor_controller.shutdown()
            except Exception as exc:
                errors.append(exc)
            finally:
                self._local_can_motor_controller = None
        for resource in (
            self._resources.can_transport,
            self._resources.agent_client,
            self._resources.remote_session,
        ):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    errors.append(exc)
        self._resources = ConnectionResources(self.state.connection_mode)
        if errors:
            self.state.log("连接", f"关闭连接时出现 {len(errors)} 个错误: {errors[0]}", "error")

    def _start_firmware(self, payload: dict[str, Any]) -> None:
        target_name = str(payload["target"])
        protocol_name = str(payload.get("protocol", "d7_iap"))
        if self.tasks.is_running(f"firmware.query:{target_name}"):
            raise RuntimeError("设备信息查询尚未结束，请稍后开始升级")

        if target_name == "battery" and protocol_name == "pace_bin":
            if self._resources.mode is ConnectionMode.ORIN_REMOTE:
                raise RuntimeError(
                    "Pace BIN 升级当前仅支持 PC 直连（CAN 盒）；Orin Agent 尚未支持 29 位扩展帧"
                )
            transport = self._firmware_transport()
            address = int(payload.get("battery_address", 0))

            def pace_operation(token: CancellationToken, report) -> dict[str, Any]:
                controller = PaceBatteryUpgradeController(
                    transport,
                    PaceBmsUpgradeProtocol(address),
                    on_log=lambda message: self.state.log("电池升级", message),
                    on_progress=report,
                )
                result = controller.upgrade_file(str(payload["firmware"]), token=token)
                return {"target": target_name, "protocol": protocol_name, **result}

            event_action = f"firmware.start.{target_name}"
            self._start(event_action, pace_operation, task_id=f"firmware.start:{target_name}")
            return

        transport = self._firmware_transport()
        target_id = int(payload["target_id"])
        iap_id = int(payload["iap_id"])

        def operation(token: CancellationToken, report) -> dict[str, Any]:
            image = FirmwareImage.from_file(str(payload["firmware"]))
            profile = (
                battery_profile(target_id, can_id=iap_id)
                if target_name == "battery"
                else IapDeviceProfile("PMU", target_id, iap_id)
            )
            controller = FirmwareUpgradeController(
                transport,
                IapProtocol(profile.target_id, profile.can_id),
                default_options=UpgradeOptions.for_profile(profile),
                on_log=lambda message: self.state.log("升级", message),
                on_progress=report,
            )
            controller.upgrade(image, token=token)
            return {"target": target_name, "size": image.size, "warnings": list(image.warnings)}

        event_action = f"firmware.start.{target_name}"
        self._start(event_action, operation, task_id=f"firmware.start:{target_name}")

    def _query_firmware(self, action: str, payload: dict[str, Any]) -> None:
        target_name = str(payload["target"])
        if self.tasks.is_running(f"firmware.query:{target_name}"):
            raise RuntimeError("设备信息查询正在运行")
        if self.tasks.is_running(f"firmware.start:{target_name}"):
            raise RuntimeError("固件升级正在运行，不能同时查询设备信息")
        transport = self._firmware_transport()
        target_id = int(payload["target_id"])
        iap_id = int(payload["iap_id"])

        def operation(token: CancellationToken, report) -> dict[str, Any]:
            report(10, "正在发送 D7 IAP 查询")
            controller = FirmwareUpgradeController(
                transport,
                IapProtocol(target_id, iap_id),
                on_log=lambda message: self.state.log("升级", message),
            )
            if action == "firmware.query_role":
                role = controller.query_role(token=token)
                self.state.log("升级", f"当前设备角色：{role}")
                report(100, f"当前设备角色：{role}")
                return {"target": target_name, "role": role}
            version = controller.query_software_version(token=token)
            self.state.log("升级", f"当前软件版本：{version}")
            report(100, f"当前软件版本：{version}")
            return {"target": target_name, "version": version}

        event_action = f"{action}.{target_name}"
        self._start(event_action, operation, task_id=f"firmware.query:{target_name}")

    def _firmware_transport(self) -> CanTransport:
        if self._resources.mode is ConnectionMode.ORIN_REMOTE:
            client = self._require_agent()
            ota_bus = next(
                (name for name, item in self.state.evt.interfaces.items() if item.role == "ota"),
                self.state.active_interface,
            )
            return AgentCanTransport(client, ota_bus)
        if self._resources.can_transport is None:
            raise RuntimeError("PC CAN 尚未连接")
        return self._resources.can_transport

    def _handle_serial(self, action: str, payload: dict[str, Any]) -> None:
        if action in {"serial485.cancel", "serial485.cycle_cancel"}:
            self._cancel_action("serial485.cycle_start")
            return
        operation_name = action.removeprefix("serial485.")

        if operation_name == "open":
            def open_operation(token: CancellationToken, report) -> object:
                report(20, "正在打开 485 串口")
                token.raise_if_cancelled()
                controller = self._open_serial(payload)
                report(100, "485 通信已打开")
                return {
                    "port": controller.service.config.port,
                    "baud": controller.service.config.baudrate,
                }

            self._start(action, open_operation, task_id="serial485.connection")
            return

        if operation_name == "close":
            self._cancel_action("serial485.")

            def close_operation(_token: CancellationToken, report) -> object:
                report(30, "正在关闭 485 串口")
                self._close_serial()
                report(100, "485 通信已关闭")
                return {"closed": True}

            self._start(action, close_operation, task_id="serial485.connection")
            return

        controller = self._require_serial(payload)
        if operation_name == "stop":
            self._cancel_action("serial485.")

        def run_serial_operation(token: CancellationToken, report) -> object:
            controller.on_progress = report
            comm_id = int(payload.get("station_id", 0))
            if operation_name == "scan":
                return controller.scan(token)
            if operation_name == "connect":
                connection = controller.connect(comm_id, allow_station_fallback=False)
                return {
                    "present": True,
                    "comm_id": connection.comm_id,
                    "station": connection.station,
                    "attempts": connection.attempts,
                    "echo_frame": connection.echo_frame.hex(" ").upper(),
                    "echo_ok": connection.echo_ok,
                    "register_value": connection.register_value,
                    "device_id": connection.device_id,
                    "device_name": connection.device_name,
                    "hardware_version": connection.hardware_version,
                    "software_version": connection.software_version,
                    "parameters": [
                        {"address": address, "value": value}
                        for address, value in connection.parameter_values
                    ],
                    "parameter_error": connection.parameter_error,
                }
            if operation_name == "read_identity":
                identity = controller.read_identity(comm_id)
                return {
                    "present": True,
                    "comm_id": identity.comm_id,
                    "register_value": identity.register_value,
                    "parameter": f"通讯 ID 寄存器 0x{identity.register_value:04X}",
                }
            if operation_name == "write_identity":
                identity = controller.write_comm_id(
                    int(payload["new_id"]),
                    token,
                    current_comm_id=comm_id,
                )
                return {
                    "comm_id": identity.comm_id,
                    "register_value": identity.register_value,
                    "verified": True,
                    "verification": "0x08_echo",
                    "parameter_error": identity.parameter_error,
                }
            if operation_name == "reset_identity":
                controller.broadcast_reset_comm_id(token)
            elif operation_name == "phase_identify":
                result = controller.identify_wheel_phases(comm_id, token)
                return {
                    "phase_sequence": result.phase_sequence,
                    "phase_sequence_text": "UVW" if result.phase_sequence == 0 else "UWV",
                    "encoder_offset": result.encoder_offset,
                }
            elif operation_name == "take_control":
                controller.take_control_authority(comm_id, token)
            elif operation_name == "release_control":
                controller.release_control_authority(comm_id, token)
            elif operation_name == "enable":
                statusword = controller.enable_servo(comm_id, token)
                return {
                    "operation": operation_name,
                    "comm_id": comm_id,
                    "statusword": statusword,
                }
            elif operation_name == "release_brake":
                controller.set_controlword(comm_id, CONTROLWORD_BRAKE_RELEASE, token)
            elif operation_name == "set_velocity":
                controller.set_speed(
                    comm_id,
                    float(payload.get("rad_s", 0)),
                    token,
                    duration_s=(
                        float(payload["duration_s"])
                        if payload.get("duration_s") is not None
                        else None
                    ),
                    accel_rad_s2=float(payload.get("accel_rad_s2", 1.0)),
                    decel_rad_s2=float(payload.get("decel_rad_s2", 1.0)),
                )
            elif operation_name == "read_position":
                position = controller.read_absolute_position(comm_id)
                return {"position_rad": position.radians, "position_counts": position.counts}
            elif operation_name == "move_absolute":
                controller.move_absolute(
                    comm_id,
                    position_rad=float(payload["position_rad"]),
                    accel_rad_s2=float(payload.get("accel_rad_s2", 1.0)),
                    decel_rad_s2=float(payload.get("decel_rad_s2", 1.0)),
                    token=token,
                )
            elif operation_name == "move_relative":
                controller.move_relative_angle(
                    comm_id,
                    angle_deg=float(payload["angle_deg"]),
                    accel_rad_s2=float(payload.get("accel_rad_s2", 1.0)),
                    decel_rad_s2=float(payload.get("decel_rad_s2", 1.0)),
                    token=token,
                )
            elif operation_name == "stop":
                controller.set_speed(comm_id, "stop", token)
                controller.set_controlword(comm_id, CONTROLWORD_STOP_POSITION, token)
            elif operation_name == "cycle_start":
                controller.run_cycle(
                    CycleOptions(
                        comm_id=comm_id,
                        cycles=int(payload["cycles"]),
                        run_seconds=float(payload["run_seconds"]),
                        round_wait_seconds=float(payload["round_wait_seconds"]),
                    ),
                    token,
                )
            else:
                raise NotImplementedError(operation_name)
            return {"operation": operation_name, "comm_id": comm_id}

        def operation(token: CancellationToken, report) -> object:
            if operation_name == "stop":
                return run_serial_operation(token, report)
            if not self._serial_operation_lock.acquire(blocking=False):
                raise RuntimeError("485 正在执行其他操作，请等待当前操作完成")
            try:
                return run_serial_operation(token, report)
            finally:
                self._serial_operation_lock.release()

        self._start(action, operation, task_id="serial485.scan" if operation_name == "scan" else None)

    def _open_serial(self, payload: dict[str, Any]) -> Serial485Controller:
        port = str(payload.get("port", ""))
        baud = int(payload.get("baud", 115200))
        config = Serial485Config(port, baud)
        if (
            self._serial_service is None
            or self._serial_controller is None
            or self._serial_service.config != config
            or not self._serial_service.is_open
        ):
            self._close_serial()
            service = Serial485Service(config)
            try:
                service.open()
                controller = Serial485Controller(
                    service,
                    on_log=lambda message: self.state.log("485", message),
                )
            except Exception:
                service.close()
                self._serial_service = None
                self._serial_controller = None
                raise
            self._serial_service = service
            self._serial_controller = controller
        assert self._serial_controller is not None
        return self._serial_controller

    def _require_serial(self, payload: dict[str, Any]) -> Serial485Controller:
        if (
            self._serial_service is None
            or self._serial_controller is None
            or not self._serial_service.is_open
        ):
            raise RuntimeError("485 通信尚未打开，请先点击“打开 485”")
        # The global status rail owns the serial connection. Subpages deliberately
        # send only operation arguments, so validate the connection settings only
        # when a legacy caller explicitly supplies them.
        if "port" in payload or "baud" in payload:
            requested = Serial485Config(
                str(payload.get("port", self._serial_service.config.port)),
                int(payload.get("baud", self._serial_service.config.baudrate)),
            )
            if self._serial_service.config != requested:
                raise RuntimeError("串口或波特率已变化，请先关闭再重新打开 485")
        return self._serial_controller

    def _close_serial(self) -> None:
        if self._serial_service is not None:
            self._serial_service.close()
        self._serial_service = None
        self._serial_controller = None

    def _handle_motor(self, action: str, payload: dict[str, Any]) -> None:
        if action in {"motor.long_test_cancel"}:
            self._cancel_action("motor.long_test_start")
            return
        target = dict(payload.get("target", {}))
        operation_name = action.removeprefix("motor.")
        unlocked_operations = {
            "enable",
            "release_brake",
            "set_mode",
            "set_position",
            "move_absolute",
            "move_relative",
            "set_velocity",
            "param_write",
            "param_save",
            "zero_prepare",
            "zero_commit",
            "take_control",
            "long_test_start",
        }
        if operation_name in unlocked_operations and self.state.safety_locked:
            raise PermissionError("本次连接会话尚未解除安全锁")

        source = ConnectionMode(str(payload.get("source", self.state.connection_mode.value)))
        if source is not self._resources.mode:
            raise RuntimeError("所选 CAN 通信来源尚未连接")
        raw_can_operations = {
            "probe",
            "enable",
            "disable",
            "clear_errors",
            "take_control",
            "release_control",
            "set_position",
            "move_absolute",
            "move_relative",
            "read_position",
            "set_velocity",
            "emergency_stop",
        }
        if operation_name in raw_can_operations:
            if source is ConnectionMode.PC_DIRECT and operation_name not in {
                "probe",
                "read_position",
                "disable",
                "emergency_stop",
            }:
                raise RuntimeError(
                    "CAN 盒直控已安全禁用：此前切换控制源会中断 EtherCAT。"
                    "在厂家提供已验证的 EtherCAT 恢复/直控协议前，仅允许扫描、读取位置和失能。"
                )
            if operation_name == "probe":
                self._handle_can_probe(action, target, source)
                return
            self._handle_can_motor(action, operation_name, target, payload)
            return
        if source is ConnectionMode.PC_DIRECT:
            raise RuntimeError("该功能尚未支持 CAN 盒直连")
        service = self._require_motor_service()

        def operation(token: CancellationToken, report) -> object:
            if operation_name in {"enable", "disable", "clear_errors", "release_brake"}:
                getattr(service, operation_name)(target)
                return {}
            if operation_name in {"take_control", "release_control"}:
                service.set_control_authority(target, operation_name == "take_control")
            elif operation_name == "set_mode":
                service.set_mode(target, str(payload["mode"]))
            elif operation_name == "set_position":
                service.set_position(target, float(payload["angle_deg"]))
            elif operation_name == "move_absolute":
                service.set_position_rad(target, float(payload["position_rad"]))
            elif operation_name == "move_relative":
                return service.move_relative(target, float(payload["angle_deg"]))
            elif operation_name == "read_position":
                return service.read_position(target)
            elif operation_name == "set_velocity":
                rad_s = float(payload["rad_s"])
                if "accel_time_ms" in payload:
                    accel_time_ms = int(payload["accel_time_ms"])
                elif "accel_rad_s2" in payload:
                    accel = float(payload["accel_rad_s2"])
                    if accel <= 0:
                        raise ValueError("加速度必须大于 0")
                    accel_time_ms = max(1, min(65535, round(abs(rad_s) / accel * 1000)))
                else:
                    accel_time_ms = 1000
                decel = float(payload.get("decel_rad_s2", 1.0))
                if decel <= 0:
                    raise ValueError("减速度必须大于 0")
                decel_time_ms = max(1, min(65535, round(abs(rad_s) / decel * 1000)))
                service.set_velocity(target, rad_s, accel_time_ms)
                duration_s = payload.get("duration_s")
                if duration_s is not None:
                    try:
                        self._sleep_with_cancel(float(duration_s), token)
                    finally:
                        service.emergency_stop(target, decel_time_ms)
            elif operation_name == "emergency_stop":
                service.emergency_stop(target)
            elif operation_name == "zero_prepare":
                return {"token": service.zero_prepare(target)}
            elif operation_name == "zero_commit":
                service.zero_commit(target)
            elif operation_name.startswith("param_"):
                return self._motor_parameters(service, operation_name, target, payload)
            elif operation_name == "long_test_start":
                return self._run_motor_long_test(service, payload, token, report)
            else:
                raise NotImplementedError(operation_name)
            return {}

        self._start(action, operation)

    def _handle_can_motor(
        self,
        action: str,
        operation_name: str,
        target: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        nodes = self._local_motor_nodes(target)
        buses = {node.bus for node in nodes}
        if len(buses) != 1:
            raise RuntimeError("一次电机控制只能选择同一路 CAN 上的电机")
        transport = self._require_can(next(iter(buses)))
        if not transport.is_open:
            raise RuntimeError("CAN 通道尚未打开")
        controller = self._local_can_controller(transport)
        device_ids = [node.dev_id for node in nodes]
        logic_by_device = {node.dev_id: node.logic_id for node in nodes}

        def operation(token: CancellationToken, report) -> object:
            token.raise_if_cancelled()
            if operation_name == "read_position":
                positions = controller.read_positions(device_ids, token)
                mapped = {
                    str(logic_by_device[device_id]): value
                    for device_id, value in positions.items()
                }
                if len(mapped) == 1:
                    return {"position_rad": next(iter(mapped.values())), "positions": mapped}
                return {"positions": mapped}
            if operation_name == "enable":
                result = controller.enable_position_hold(device_ids, token, report)
                return {
                    "motors": [node.logic_id for node in nodes],
                    "positions": {
                        str(logic_by_device[device_id]): value
                        for device_id, value in result["positions"].items()
                    },
                    "statuses": {
                        str(logic_by_device[device_id]): value
                        for device_id, value in result["statuses"].items()
                    },
                    "heartbeat_hz": result["heartbeat_hz"],
                }
            if operation_name == "move_relative":
                delta = float(payload["angle_deg"]) * 3.141592653589793 / 180.0
                positions = controller.move_relative(device_ids, delta, token)
                return {
                    "positions": {
                        str(logic_by_device[device_id]): value
                        for device_id, value in positions.items()
                    }
                }
            if operation_name == "set_velocity":
                rad_s = float(payload["rad_s"])
                duration_s = float(payload.get("duration_s", 0))
                accel = float(payload.get("accel_rad_s2", 1.0))
                decel = float(payload.get("decel_rad_s2", 1.0))
                if duration_s <= 0:
                    raise ValueError("CAN 速度运动必须设置大于 0 的运动时间")
                if accel <= 0 or decel <= 0:
                    raise ValueError("加速度和减速度必须大于 0")
                accel_time_ms = max(1, min(65535, round(abs(rad_s) / accel * 1000)))
                decel_time_ms = max(1, min(65535, round(abs(rad_s) / decel * 1000)))
                result = controller.run_velocity(
                    device_ids,
                    rad_s,
                    duration_s,
                    accel_time_ms,
                    decel_time_ms,
                    token,
                )
                return {
                    "motors": [node.logic_id for node in nodes],
                    "rad_s": rad_s,
                    "duration_s": duration_s,
                    "positions": {
                        str(logic_by_device[device_id]): value
                        for device_id, value in result["positions"].items()
                    },
                    "statuses": {
                        str(logic_by_device[device_id]): value
                        for device_id, value in result["statuses"].items()
                    },
                    "safe_state": result["safe_state"],
                }
            if operation_name in {"release_brake", "release_control"}:
                raise RuntimeError(f"暂不支持已验证的 {operation_name} 协议")
            if operation_name in {"move_absolute", "set_position"}:
                position_rad = (
                    float(payload["position_rad"])
                    if operation_name == "move_absolute"
                    else float(payload["angle_deg"]) * 3.141592653589793 / 180.0
                )
                controller.set_positions(
                    {node.dev_id: position_rad for node in nodes},
                    token,
                )
                return {"position_rad": position_rad}
            if operation_name == "take_control":
                controller.take_control(device_ids, token)
            elif operation_name == "clear_errors":
                controller.clear_errors(device_ids, token)
            elif operation_name == "disable":
                controller.safe_stop(device_ids)
            elif operation_name == "emergency_stop":
                controller.emergency_stop(device_ids)
            else:
                raise NotImplementedError(operation_name)
            return {"motors": [node.logic_id for node in nodes]}

        if operation_name in {"disable", "emergency_stop"}:
            self.tasks.cancel("motor.local_can.operation")
            self._start(action, operation)
            return
        if self.tasks.is_running("motor.local_can.operation"):
            raise RuntimeError("CAN 正在执行其他电机操作")
        self._start(action, operation, task_id="motor.local_can.operation")

    def _handle_can_probe(
        self,
        action: str,
        target: dict[str, Any],
        source: ConnectionMode,
    ) -> None:
        nodes = self._local_motor_nodes(target)
        nodes_by_bus: dict[str, list[Any]] = {}
        for node in nodes:
            nodes_by_bus.setdefault(node.bus, []).append(node)

        def operation(token: CancellationToken, report) -> object:
            online: list[int] = []
            missing: list[int] = []
            response_ids: set[int] = set()
            positions: dict[str, float] = {}
            statuses: dict[str, int] = {}
            total_buses = len(nodes_by_bus)
            for bus_index, (bus, bus_nodes) in enumerate(nodes_by_bus.items(), 1):
                token.raise_if_cancelled()
                report(
                    round((bus_index - 1) * 100 / max(1, total_buses)),
                    f"正在检测 {bus.upper()} 的 {len(bus_nodes)} 台电机",
                )
                transport = self._require_can(bus)
                controller = self._local_can_controller(transport)
                result = controller.probe(
                    [node.dev_id for node in bus_nodes], token
                )
                response_ids.update(int(value) for value in result["response_ids"])
                by_device = {node.dev_id: node for node in bus_nodes}
                for device_id, item in result["feedback"].items():
                    node = by_device[device_id]
                    online.append(node.logic_id)
                    positions[str(node.logic_id)] = item.position_rad
                    statuses[str(node.logic_id)] = item.status
                missing.extend(
                    by_device[device_id].logic_id for device_id in result["missing"]
                )
            report(100, f"目标检测完成：在线 {len(online)}/{len(nodes)}")
            return {
                "source": source.value,
                "target": dict(target),
                "total": len(nodes),
                "online": sorted(online),
                "missing": sorted(missing),
                "response_ids": sorted(response_ids),
                "positions": positions,
                "statuses": statuses,
            }

        if self.tasks.is_running("motor.local_can.operation"):
            raise RuntimeError("CAN 正在执行其他电机操作")
        self._start(action, operation, task_id="motor.local_can.operation")

    def _handle_local_can_motor(
        self,
        action: str,
        operation_name: str,
        target: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Compatibility entry point for the shared PC/Orin CAN controller."""
        self._handle_can_motor(action, operation_name, target, payload)

    def _local_can_controller(self, transport: CanTransport) -> LocalCanMotorController:
        controller = self._local_can_motor_controller
        if controller is not None and controller.transport is transport:
            return controller
        if controller is not None:
            controller.shutdown()
        controller = LocalCanMotorController(
            transport,
            on_fault=self.local_can_fault_received.emit,
        )
        self._local_can_motor_controller = controller
        return controller

    def _local_motor_nodes(self, target: dict[str, Any]) -> list[Any]:
        if set(target) == {"motor"}:
            logic_ids = [int(target["motor"])]
        elif set(target) == {"group"}:
            logic_ids = list(self.state.evt.fixed_groups.get(str(target["group"]), ()))
        elif "motors" in target:
            logic_ids = [int(value) for value in target["motors"]]
        else:
            raise ValueError("只支持单电机或固定分组目标")
        by_id = {node.logic_id: node for node in self.state.evt.nodes}
        try:
            return [by_id[logic_id] for logic_id in logic_ids]
        except KeyError as exc:
            raise ValueError(f"未知逻辑 ID: {exc.args[0]}") from exc

    def _motor_parameters(
        self,
        service: OrinMotorService,
        operation: str,
        target: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = service.normalize_target(target)
        if "motors" not in normalized or len(normalized["motors"]) != 1:
            raise ValueError("参数操作只支持单电机")
        names = (
            "communication_timeout_ms",
            "permission",
            "alarm_mask",
            "max_speed_rpm",
            "function_switch_mask",
        )
        if operation == "param_read":
            return {
                name: service.client.request("param.read", target=normalized, args={"name": name})
                for name in names
            }
        values = dict(payload.get("values", {}))
        if operation == "param_write":
            for name, value in values.items():
                if name not in names:
                    raise ValueError(f"参数不在白名单: {name}")
                service.client.request(
                    "param.write",
                    target=normalized,
                    args={"name": name, "value": str(value), "confirm": True},
                )
            return {"written": sorted(values)}
        if operation == "param_save":
            service.client.request("param.save", target=normalized, args={"confirm": True})
            return {"saved": True}
        raise NotImplementedError(operation)

    def _run_motor_long_test(
        self,
        service: OrinMotorService,
        payload: dict[str, Any],
        token: CancellationToken,
        report,
    ) -> dict[str, int]:
        target = {"group": str(payload["group"])}
        cycles = int(payload["cycles"])
        dwell = float(payload["dwell_s"])
        mode = str(payload["mode"])
        amplitude = float(payload.get("amplitude_deg", 5.0))
        completed = 0
        try:
            for index in range(cycles):
                for direction in (1, -1):
                    token.raise_if_cancelled()
                    if mode == "velocity":
                        service.set_velocity(target, direction * float(payload["max_rad_s"]))
                    else:
                        service.set_position(target, direction * amplitude)
                    self._sleep_with_cancel(dwell, token)
                completed = index + 1
                report(round(completed * 100 / cycles), f"完成 {completed}/{cycles} 循环")
        finally:
            service.emergency_stop(target)
        return {"completed_cycles": completed}

    def _handle_diagnostics(self, action: str, payload: dict[str, Any]) -> None:
        if action == "diagnostics.stress_cancel":
            if self._diagnostic_loop_running:
                self._diagnostic_loop_stop.set()
                self.state.log("诊断", "已请求停止，将在当前轮完成后生成汇总报告", "warning")
            else:
                self._cancel_action("diagnostics.stress_start")
            return
        session = self._require_remote()
        service = DiagnosticService(self.state.evt, session)
        sudo_password = (
            session.connection.password if isinstance(session, ParamikoRemoteSession) else None
        )

        def operation(token: CancellationToken, report) -> object:
            def emit(line: str, is_error: bool) -> None:
                if (
                    action == "diagnostics.broadcast"
                    and not is_error
                    and line.startswith(f"{LIVE_RESPONSE_PREFIX}:")
                ):
                    self.state.notify_task(action, "output", {"output": line})
                    return
                self.state.log("诊断", line, "error" if is_error else "info")

            if action == "diagnostics.socketcan_probe" or action == "diagnostics.network_probe":
                return {"interfaces": list(service.probe_interfaces(token))}
            if action == "diagnostics.network_forward_configure":
                configured_adb = str(self.settings.value("diagnostics/adb_path", "")).strip()
                adb_path = Path(configured_adb or shutil.which("adb") or "adb.exe")
                forwarder = AdbNetworkForwarder(
                    ForwardConfig(
                        adb_path=adb_path,
                        wlan_device=str(self.settings.value("diagnostics/rk_wlan", "wlan0")),
                        rk_ethernet_device=str(self.settings.value("diagnostics/rk_ethernet", "eth0")),
                        orin_ethernet_device=str(
                            self.settings.value("diagnostics/orin_ethernet", "eth0")
                        ),
                        orin_ip=str(self.settings.value("diagnostics/orin_ip", "10.254.254.1")),
                        forward_port=int(self.settings.value("diagnostics/forward_port", 22)),
                        adb_serial=str(self.settings.value("diagnostics/adb_serial", "")),
                    ),
                    session,
                )
                return forwarder.execute(token)
            if action == "diagnostics.socketcan_configure":
                from d7_factory_studio.features.diagnostics.commands import configure_requests

                results = []
                for index, config in enumerate(self.state.evt.interfaces.values(), 1):
                    for request in configure_requests(config, sudo_password):
                        results.append(session.execute(request, token, emit))
                    report(round(index * 100 / len(self.state.evt.interfaces)), config.name)
                return {"configured": [config.name for config in self.state.evt.interfaces.values()]}
            if action == "diagnostics.stress_start":
                profile = DiagnosticProfile(str(payload["profile"]))
                from d7_factory_studio.features.diagnostics.profiles import safe_random_ids

                clear_dmesg = bool(payload.get("clear_dmesg", False))
                options = DiagnosticOptions(
                    profile=profile,
                    fixed_can_id=safe_random_ids(self.state.evt)[0],
                    duration_s=int(payload["duration_s"]),
                    gap_ms=int(payload["gap_ms"]),
                    setup_can_script=str(payload.get("setup_can_script", "")).strip() or None,
                    setup_each_stage=bool(payload.get("setup_each_stage", False)),
                    clear_dmesg=clear_dmesg,
                    high_risk_dmesg_clear_confirmed=clear_dmesg,
                )
                interfaces = tuple(str(value) for value in payload["interfaces"])
                loop_enabled = bool(payload.get("loop", False))
                self._diagnostic_loop_running = loop_enabled
                self._diagnostic_loop_stop.clear()
                run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:6]}-diagnostics"
                run_root = self.settings.report_directory / run_id
                rounds: list[dict[str, object]] = []
                writer = ReportBundleWriter()
                try:
                    round_index = 1
                    while True:
                        token.raise_if_cancelled()

                        def round_progress(
                            value: int,
                            message: str,
                            round_number: int = round_index,
                        ) -> None:
                            prefix = f"第 {round_number} 轮 · " if loop_enabled else ""
                            report(value, prefix + message)

                        report(0, f"第 {round_index} 轮开始" if loop_enabled else "链路测试开始")
                        result = service.run(
                            interfaces,
                            options,
                            token,
                            emit,
                            round_progress,
                            sudo_password=sudo_password,
                        )
                        verdict = str(
                            result.get("evaluation", {}).get("verdict", "FAIL")
                            if isinstance(result.get("evaluation"), dict)
                            else "FAIL"
                        ).upper()
                        round_bundle = writer.write(
                            run_root / f"round-{round_index:03d}" if loop_enabled else run_root,
                            result,
                            raw_artifacts=diagnostic_raw_artifacts(result),
                            secrets=(sudo_password or "",),
                        )
                        rounds.append(
                            {
                                "round": round_index,
                                "verdict": verdict,
                                "result": result,
                                "bundle": round_bundle,
                            }
                        )
                        execution = result.get("execution", {})
                        execution_status = (
                            str(execution.get("status", "complete"))
                            if isinstance(execution, dict)
                            else "complete"
                        )
                        if execution_status == "not_started":
                            emit(
                                "诊断前置检查未通过，本轮没有执行任何链路流量阶段；"
                                "已停止循环并生成失败报告",
                                True,
                            )
                        else:
                            emit(
                                f"第 {round_index} 轮完成：{verdict}"
                                if loop_enabled
                                else f"测试完成：{verdict}",
                                verdict == "FAIL",
                            )
                        if (
                            not loop_enabled
                            or self._diagnostic_loop_stop.is_set()
                            or execution_status == "not_started"
                        ):
                            break
                        round_index += 1
                    if not loop_enabled:
                        return {"result": result, "bundle": round_bundle, "rounds": rounds}
                    summary = self._diagnostic_loop_summary(interfaces, rounds)
                    bundle = writer.write(
                        run_root,
                        summary,
                        raw_artifacts=self._diagnostic_loop_raw_artifacts(rounds),
                        secrets=(sudo_password or "",),
                    )
                    report(100, f"循环测试已停止，共完成 {len(rounds)} 轮")
                    return {"result": summary, "bundle": bundle, "rounds": rounds}
                finally:
                    self._diagnostic_loop_running = False
                    self._diagnostic_loop_stop.clear()
            if action == "diagnostics.node_param_read" or action == "diagnostics.node_param_write":
                interface = str(payload.get("interface", ""))
                motor_interfaces = {node.bus for node in self.state.evt.nodes}
                if interface not in motor_interfaces:
                    raise ValueError(f"当前 {self.state.evt.variant} 不包含电机通道 {interface}")
                report(1, "正在执行 setup_can.sh")
                service.prepare_motor_can(
                    token,
                    on_output=emit,
                    sudo_password=sudo_password,
                )
                report(5, f"{interface.upper()} 开始参数检查")
                return service.run_node_parameters(
                    str(
                        self.settings.value(
                            "diagnostics/node_binary",
                            "/opt/actuator_sdk/jihua_calib_param_factory",
                        )
                    ),
                    token,
                    buses=(interface,),
                    logic_ids=tuple(int(value) for value in payload.get("logic_ids", [])),
                    write_then_read=action.endswith("write"),
                    on_output=emit,
                )
            if action == "diagnostics.broadcast":
                interface = str(payload.get("interface", ""))
                if interface not in self.state.evt.interfaces:
                    raise ValueError(f"当前 {self.state.evt.variant} 不包含接口 {interface}")
                duration_s = int(payload.get("duration_s", 10))
                if not 1 <= duration_s <= 300:
                    raise ValueError("广播时长必须在 1..300 秒")
                report(1, "正在执行 setup_can.sh")
                service.prepare_motor_can(
                    token,
                    on_output=emit,
                    sudo_password=sudo_password,
                )
                finished = threading.Event()

                def broadcast_progress() -> None:
                    started = time.monotonic()
                    while not finished.wait(1.0):
                        elapsed = min(duration_s, int(time.monotonic() - started))
                        report(
                            min(95, round(elapsed * 95 / duration_s)),
                            f"{interface.upper()} 监听 {elapsed}/{duration_s}s",
                        )

                progress_thread = threading.Thread(
                    target=broadcast_progress,
                    name="d7-broadcast-progress",
                    daemon=True,
                )
                progress_thread.start()
                try:
                    result = service.run_broadcast(interface, duration_s, token, on_output=emit)
                    report(100, f"{interface.upper()} 广播监听完成")
                    return result
                finally:
                    finished.set()
                    progress_thread.join(timeout=0.2)
            if action in {"diagnostics.timing_calculate", "diagnostics.tdc_calculate"}:
                from d7_factory_studio.features.diagnostics.timing import best_candidate, tdcr

                candidate = best_candidate(
                    clock_hz=int(float(payload.get("clock_mhz", 40)) * 1_000_000),
                    bitrate=5_000_000 if action == "diagnostics.tdc_calculate" else 1_000_000,
                    sample_point=float(payload.get("sample_point", 80)) / 100,
                    data_phase=action == "diagnostics.tdc_calculate",
                )
                return {"candidate": candidate, "tdcr": tdcr(candidate)}
            if action == "diagnostics.tdc_apply":
                return self._apply_tdc(session, token, payload)
            raise NotImplementedError(action)

        self._start(action, operation)

    def _diagnostic_loop_summary(
        self,
        interfaces: tuple[str, ...],
        rounds: list[dict[str, object]],
    ) -> dict[str, object]:
        verdicts = [str(item.get("verdict", "FAIL")).upper() for item in rounds]
        verdict = "FAIL" if "FAIL" in verdicts else "WARN" if "WARN" in verdicts else "PASS"
        findings = [
            f"第 {item['round']} 轮：{item.get('verdict', 'FAIL')}"
            for item in rounds
        ]
        stages: list[dict[str, object]] = []
        planned_stage_count = 0
        executed_stage_count = 0
        for item in rounds:
            result = item.get("result", {})
            if not isinstance(result, dict):
                continue
            execution = result.get("execution", {})
            if isinstance(execution, dict):
                planned_stage_count += int(execution.get("planned_stage_count", 0))
                executed_stage_count += int(execution.get("executed_stage_count", 0))
            for stage in result.get("stages", []):
                if isinstance(stage, dict):
                    stages.append({**stage, "round": item["round"]})
        if executed_stage_count == planned_stage_count:
            execution_status = "complete"
        elif executed_stage_count:
            execution_status = "partial"
        else:
            execution_status = "not_started"
        return {
            "schema_version": 1,
            "kind": "can_diagnostic_loop",
            "evt": {"variant": self.state.evt.variant, "robot_model": self.state.evt.robot_model},
            "interfaces": list(interfaces),
            "round_count": len(rounds),
            "evaluation": {"verdict": verdict, "findings": findings, "root_cause_hints": []},
            "execution": {
                "status": execution_status,
                "planned_stage_count": planned_stage_count,
                "executed_stage_count": executed_stage_count,
            },
            "stages": stages,
            "rounds": [
                {
                    "round": item["round"],
                    "verdict": item.get("verdict", "FAIL"),
                    "bundle": item.get("bundle", {}),
                }
                for item in rounds
            ],
        }

    @staticmethod
    def _diagnostic_loop_raw_artifacts(
        rounds: list[dict[str, object]],
    ) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        for item in rounds:
            result = item.get("result", {})
            if not isinstance(result, dict):
                continue
            round_number = int(item.get("round", 0))
            for name, content in diagnostic_raw_artifacts(result).items():
                artifacts[f"rounds/{round_number:03d}/{name}"] = content
        return artifacts

    def _handle_device_logs(self, action: str, payload: dict[str, Any]) -> None:
        service = DeviceLogService(self._require_remote())

        def operation(token: CancellationToken, report) -> object:
            if action == "device_logs.list":
                entries = service.browse(
                    LogTimeFilter(int(payload["start_epoch"]), int(payload["end_epoch"]))
                )
                return {"entries": entries, "rows": [service.display_row(entry) for entry in entries]}
            if action == "device_logs.download":
                entries = tuple(
                    self._device_log_catalog[path]
                    for path in payload.get("remote_paths", [])
                    if path in self._device_log_catalog
                )
                return service.download_selected(entries, Path(str(payload["destination"])), token)
            raise NotImplementedError(action)

        self._start(action, operation)

    def _handle_machine(self, action: str, payload: dict[str, Any]) -> None:
        if action == "machine.battery_simulation_stop":
            self._cancel_action("machine.battery_simulation_start")
            return
        if action == "machine.can_monitor_stop":
            self._cancel_action("machine.can_monitor_start")
            return
        selected_interface = payload.get("interface")
        transport = self._require_can(str(selected_interface) if selected_interface else None)

        def operation(token: CancellationToken, report) -> object:
            if action == "machine.can_send":
                if not self._setting_enabled("safety/allow_raw_can"):
                    raise PermissionError("设置中未允许发送原始 CAN 帧")
                frame_type = str(payload["frame_type"])
                transport.send(
                    frame := CanFrame(
                        int(payload["arbitration_id"]),
                        bytes(payload["data"]),
                        is_fd=frame_type != "classic",
                        bitrate_switch=frame_type == "fd_brs",
                    )
                )
                return {"frame": frame}
            if action == "machine.can_monitor_start":
                while True:
                    token.raise_if_cancelled()
                    for frame in transport.receive(100):
                        self.state.notify_task("machine.can_frame", "event", frame)
            if action == "machine.battery_simulation_start":
                targets = tuple(int(value) for value in payload["target_ids"])
                interval_s = int(payload["interval_ms"]) / 1000
                voltage_mv = round(float(payload["voltage_v"]) * 1000)
                current_ma = round(float(payload["current_a"]) * 1000)
                soc = int(payload["soc"])
                cycles = 0
                while True:
                    token.raise_if_cancelled()
                    for target in targets:
                        for frame in (
                            build_battery_frame(target, 0x02, voltage_mv),
                            build_battery_frame(target, 0x0E, current_ma & 0xFFFFFFFF),
                            build_battery_frame(target, 0x81, soc),
                        ):
                            transport.send(frame)
                    cycles += 1
                    report(0, f"已发送 {cycles} 轮 · {len(targets) * 3} 帧/轮")
                    self._sleep_with_cancel(interval_s, token)
            if action == "machine.light_command":
                sent = 0
                for strip_id in payload["strip_ids"]:
                    transport.send(
                        build_light_frame(
                            int(payload["can_id"]),
                            int(strip_id),
                            int(payload["color_id"]),
                            int(payload["mode"]),
                            int(payload["count"]),
                            int(payload["cycle_ms"]),
                            tuple(int(value) for value in payload["rgb"]),
                        )
                    )
                    sent += 1
                return {"sent": sent, "mode": int(payload["mode"])}
            if action == "machine.light_pattern":
                pattern = str(payload["pattern"])
                modes = {"常亮": 1, "呼吸": 3, "流水": 9, "告警闪烁": 7, "全部关闭": 0}
                mode = modes[pattern]
                color_id = 2 if pattern == "告警闪烁" else 1
                rgb = (255, 0, 0) if pattern == "告警闪烁" else (255, 255, 255)
                for strip_id in range(200, 204):
                    transport.send(build_light_frame(0x08, strip_id, color_id, mode, 1, 500, rgb))
                return {"mode": mode, "sent": 4}
            if action == "machine.info_read":
                raw_slots: dict[int, bytes] = {}
                slots = sorted({field.slot for field in MACHINE_INFO_FIELDS})
                for index, slot in enumerate(slots, 1):
                    token.raise_if_cancelled()
                    transport.send(build_machine_info_request(0x08, slot))
                    for _attempt in range(3):
                        response = next(
                            (
                                parsed
                                for frame in transport.receive(100)
                                if (parsed := parse_machine_info_response(frame)) is not None
                                and parsed.slot == slot
                                and parsed.status == MACHINE_INFO_SUCCESS
                            ),
                            None,
                        )
                        if response is not None:
                            raw_slots[slot] = response.raw
                            break
                    report(round(index * 100 / len(slots)), f"MachineInfo slot {slot}")
                values = {
                    field.key: format_machine_info_value(field, raw_slots[field.slot])
                    for field in MACHINE_INFO_FIELDS
                    if field.slot in raw_slots
                }
                return {
                    "values": values,
                    "received_slots": sorted(raw_slots),
                    "missing_slots": [slot for slot in slots if slot not in raw_slots],
                }
            raise NotImplementedError(action)

        task_id = "machine.can_monitor" if action == "machine.can_monitor_start" else None
        self._start(action, operation, task_id=task_id)

    def _apply_tdc(
        self,
        session: RemoteSession,
        token: CancellationToken,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        from d7_factory_studio.features.diagnostics.timing import best_candidate, tdcr

        interface = str(payload.get("interface", ""))
        if interface not in self.state.evt.interfaces:
            raise ValueError(f"当前 {self.state.evt.variant} 不包含接口 {interface}")
        candidate = best_candidate(
            clock_hz=int(float(payload.get("clock_mhz", 40)) * 1_000_000),
            bitrate=5_000_000,
            sample_point=float(payload.get("sample_point", 80)) / 100,
            data_phase=True,
        )
        recommendation = tdcr(candidate)
        device = session.execute(
            RemoteCommandRequest(("readlink", "-f", f"/sys/class/net/{interface}/device")),
            token,
        )
        device_path = device.stdout.strip()
        if device.returncode or not device_path.startswith("/sys/devices/") or ".mttcan" not in device_path:
            raise RuntimeError(f"{interface} 不是可验证的 Orin mttcan 接口，拒绝写入 TDC")
        tdcr_path = f"{device_path}/net/{interface}/tdcr"
        probe = session.execute(RemoteCommandRequest(("test", "-f", tdcr_path)), token)
        if probe.returncode:
            raise RuntimeError(f"未找到可写 TDCR 节点: {tdcr_path}")
        value = str(recommendation["tdcr_hex"])
        written = session.execute(
            RemoteCommandRequest(("sudo", "-n", "tee", tdcr_path), stdin_secret=value),
            token,
        )
        if written.returncode:
            raise RuntimeError(written.stderr.strip() or "TDCR 写入失败")
        readback = session.execute(RemoteCommandRequest(("cat", tdcr_path)), token)
        if readback.returncode or int(readback.stdout.strip(), 0) != int(value, 0):
            raise RuntimeError("TDCR 写入后读回不一致")
        return {"interface": interface, "path": tdcr_path, "value": value}

    def _start(self, action: str, operation, task_id: str | None = None) -> str:
        identifier = task_id or f"{action}:{uuid.uuid4().hex[:8]}"
        self._task_actions[identifier] = action
        self.state.notify_task(action, "started", {"task_id": identifier})
        self.tasks.start(identifier, operation)
        return identifier

    def _cancel_action(self, action_prefix: str) -> None:
        for task_id, action in tuple(self._task_actions.items()):
            if action.startswith(action_prefix):
                self.tasks.cancel(task_id)

    @Slot(str, int, str)
    def _on_progress(self, task_id: str, progress: int, message: str) -> None:
        action = self._task_actions.get(task_id, task_id)
        self.state.notify_task(action, "progress", {"progress": progress, "message": message})

    @Slot(str, object)
    def _on_succeeded(self, task_id: str, result: object) -> None:
        action = self._task_actions.get(task_id, task_id)
        if action == "connection.connect" and isinstance(result, ConnectionResources):
            self._resources = result
            self.state.set_link_state(LinkState.CONNECTED)
            self.state.log("连接", "设备连接成功")
            if result.warning:
                self.state.log("连接", result.warning, "warning")
        elif action == "connection.disconnect":
            self.state.set_link_state(LinkState.DISCONNECTED)
            self.state.log("连接", "设备已断开")
        elif action == "device_logs.list" and isinstance(result, dict):
            entries = result.get("entries", ())
            self._device_log_catalog = {entry.remote_path: entry for entry in entries}
        elif action == "settings.zlg_scan":
            self.settings.set_value("zlg/dll_path", str(result))
        self.state.notify_task(action, "succeeded", result)
        if action not in {"connection.connect", "connection.disconnect"}:
            self.state.log("任务", f"{action} 已完成")

    @Slot(str, str, str)
    def _on_failed(self, task_id: str, error: str, traceback_text: str) -> None:
        action = self._task_actions.get(task_id, task_id)
        if action == "connection.connect":
            self._close_resources()
            self.state.report_fault(error)
        else:
            self.state.log("任务", f"{action}: {error}", "error")
        self.state.notify_task(action, "failed", {"error": error, "traceback": traceback_text})

    @Slot(str)
    def _on_cancelled(self, task_id: str) -> None:
        action = self._task_actions.get(task_id, task_id)
        if action == "connection.connect" and self.state.link_state is LinkState.CONNECTING:
            self.state.set_link_state(LinkState.DISCONNECTED)
        self.state.log("任务", f"{action} 已取消", "warning")
        self.state.notify_task(action, "cancelled", None)

    @Slot(str)
    def _on_finished(self, task_id: str) -> None:
        self._task_actions.pop(task_id, None)

    @Slot(object)
    def _handle_agent_event(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        event_type = message.get("type")
        if event_type == "state":
            data = message.get("data", message)
            motors = data.get("motors", []) if isinstance(data, dict) else []
            self.state.online_nodes = len(motors)
            self.state.changed.emit()
            self.state.notify_task("motor.state", "event", message)
        elif event_type in {"safety", "state.error"}:
            self.state.lock(
                f"Orin agent 安全事件: {message.get('reason') or message.get('message') or '未知原因'}"
            )

    @Slot(str)
    def _handle_local_can_fault(self, message: str) -> None:
        self.state.safety_locked = True
        self.state.log("CAN 电机", message, "error")
        self.state.changed.emit()
        self.state.notify_task("motor.local_can_safety", "failed", {"error": message})

    def _require_can(self, interface: str | None = None) -> CanTransport:
        transport = self._resources.can_transport
        requested = interface or (
            transport.bus if isinstance(transport, AgentCanTransport) else self.state.active_interface
        )
        if requested not in self.state.evt.interfaces:
            raise ValueError(f"当前 {self.state.evt.variant} 不包含接口 {requested}")
        if self._resources.mode is ConnectionMode.ORIN_REMOTE:
            client = self._resources.agent_client
            if client is None:
                raise RuntimeError("Orin SocketCAN Agent 未部署；请重新连接 Orin")
            if not client.is_running:
                controller = self._local_can_motor_controller
                if controller is not None:
                    controller.shutdown()
                    self._local_can_motor_controller = None
                if isinstance(transport, AgentCanTransport):
                    transport.close()
                self._resources.can_transport = None
                transport = None
                self.state.log("Orin", "SocketCAN Agent 已退出，正在自动重启", "warning")
                hello = client.restart()
                if hello.get("protocol") != "d7-factory-can-agent-jsonl":
                    raise RuntimeError("Orin SocketCAN Agent 自动恢复握手失败")
        if (
            self._resources.mode is ConnectionMode.ORIN_REMOTE
            and isinstance(transport, AgentCanTransport)
            and transport.bus != requested
        ):
            controller = self._local_can_motor_controller
            if controller is not None and controller.transport is transport:
                if controller.is_holding:
                    raise RuntimeError("当前 CAN 通道仍在保持电机位置，请先失能再切换通道")
                controller.shutdown()
                self._local_can_motor_controller = None
            transport.close()
            transport = None
            self._resources.can_transport = None
        if transport is None and self._resources.mode is ConnectionMode.ORIN_REMOTE:
            client = self._require_agent()
            config = self.state.evt.interfaces[requested]
            transport = AgentCanTransport(client, requested)
            transport.open(0, config.mode)
            self._resources.can_transport = transport
        if transport is None:
            raise RuntimeError("CAN 尚未连接")
        if not transport.is_open:
            if self._resources.mode is not ConnectionMode.ORIN_REMOTE:
                raise RuntimeError("CAN 通道已关闭，请重新打开 CAN 盒")
            config = self.state.evt.interfaces[requested]
            transport.open(0, config.mode)
        return transport

    def _require_remote(self) -> RemoteSession:
        if self._resources.remote_session is None:
            raise RuntimeError("Orin SSH 尚未连接")
        return self._resources.remote_session

    def _require_agent(self) -> OrinAgentClient:
        if self._resources.agent_client is None or not self._resources.agent_client.is_running:
            raise RuntimeError("Orin SocketCAN Agent 未部署或未运行；请重新连接 Orin")
        return self._resources.agent_client

    def _require_motor_service(self) -> OrinMotorService:
        if self._resources.motor_service is None:
            self._require_agent()
            self._resources.motor_service = OrinMotorService(self._resources.agent_client)
        return self._resources.motor_service

    def _agent_binary_path(self) -> Path:
        configured = str(self.settings.value("ssh/agent_binary", "")).strip()
        if configured:
            return Path(configured).expanduser()
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        packaged = base / "d7_factory_studio" / "agent" / "remote_can_agent.py"
        if packaged.is_file():
            return packaged
        return Path(__file__).resolve().parent / "agent" / "remote_can_agent.py"

    def _agent_library_dir(self) -> Path:
        configured = str(self.settings.value("ssh/agent_library_dir", "")).strip()
        if configured:
            return Path(configured).expanduser()
        binary = self._agent_binary_path()
        return binary.parent / "lib"

    def _test_ssh(self, payload: dict[str, Any]) -> dict[str, str]:
        host = str(payload["host"]).strip()
        username = str(payload["username"]).strip()
        fingerprint = str(payload.get("fingerprint", "")).strip()
        client = OrinAgentClient()
        try:
            client.connect(
                host,
                int(payload["port"]),
                username,
                str(payload["password"]),
                fingerprint,
                known_hosts_path=self.settings.known_hosts_path,
            )
            status, hostname, stderr = client.execute("hostname")
            if status:
                raise RuntimeError(stderr.strip() or "hostname 执行失败")
            return {"hostname": hostname.strip(), "fingerprint": fingerprint}
        except UnknownHostKeyError as exc:
            return {
                "requires_confirmation": "true",
                "host": exc.host,
                "fingerprint": exc.fingerprint,
            }
        finally:
            client.close()

    @staticmethod
    def _sleep_with_cancel(seconds: float, token: CancellationToken) -> None:
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            token.raise_if_cancelled()
            time.sleep(min(0.05, deadline - time.monotonic()))

    def _setting_enabled(self, key: str) -> bool:
        value = self.settings.value(key, False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def shutdown(self) -> None:
        self.tasks.cancel_all()
        self._close_resources()
        self._close_serial()
