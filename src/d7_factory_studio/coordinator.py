from __future__ import annotations

import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from d7_factory_studio.application import ApplicationState
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
)
from d7_factory_studio.features.diagnostics.forwarding import (
    AdbNetworkForwarder,
    ForwardConfig,
)
from d7_factory_studio.features.firmware import (
    FirmwareImage,
    FirmwareUpgradeController,
    UpgradeOptions,
)
from d7_factory_studio.features.firmware.profiles import IapDeviceProfile, battery_profile
from d7_factory_studio.features.motor import OrinMotorService
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
from d7_factory_studio.protocols.serial485 import (
    CONTROLWORD_BRAKE_RELEASE,
    CONTROLWORD_ENABLE,
    CONTROLWORD_STOP_POSITION,
)
from d7_factory_studio.reports import ReportBundleWriter
from d7_factory_studio.settings_store import SettingsStore
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
        self._device_log_catalog: dict[str, RemoteFileInfo] = {}

        state.action_requested.connect(self.handle)
        self.tasks.progress.connect(self._on_progress)
        self.tasks.succeeded.connect(self._on_succeeded)
        self.tasks.failed.connect(self._on_failed)
        self.tasks.cancelled.connect(self._on_cancelled)
        self.tasks.finished.connect(self._on_finished)
        self.agent_event_received.connect(self._handle_agent_event)

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
            self.state.notify_task(action, "failed", {"error": str(exc)})

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
                transport = ZlgCanTransport(config)
                interface = self.state.evt.interfaces[self.state.active_interface]
                report(45, "正在打开 USBCANFD-200U")
                try:
                    transport.open(int(self.settings.value("zlg/channel", 0)), interface.mode)
                    token.raise_if_cancelled()
                    report(100, "PC CAN 已连接")
                    return ConnectionResources(mode, can_transport=transport)
                except BaseException:
                    transport.close()
                    raise

            host = str(self.settings.value("ssh/host", "")).strip()
            username = str(self.settings.value("ssh/username", "")).strip()
            port = int(self.settings.value("ssh/port", 22))
            fingerprint = str(self.settings.value("ssh/fingerprint", "")).strip()
            password = self.settings.ssh_password(host, username) or ""
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
                config_path = Path(str(self.settings.value("ssh/agent_config", ""))).expanduser()
                if binary.is_file() and config_path.is_file():
                    status, home, error = client.execute('printf "%s" "$HOME"')
                    if status != 0 or not home.strip().startswith("/"):
                        raise RuntimeError(error.strip() or "无法确定 Orin 用户目录")
                    remote_root = f"{home.strip()}/.local/share/d7-factory-studio"
                    remote_binary = f"{remote_root}/bin/d7-factory-agent"
                    remote_config = f"{remote_root}/config/d7-agent.yaml"
                    report(70, "正在部署会话级 motor agent")
                    client.deploy_file(binary, remote_binary, executable=True)
                    client.deploy_file(config_path, remote_config)
                    library_dir = Path(
                        str(self.settings.value("ssh/agent_library_dir", ""))
                    ).expanduser()
                    if library_dir.is_dir():
                        for library in sorted(library_dir.glob("*.so*")):
                            if library.is_file():
                                client.deploy_file(library, f"{remote_root}/lib/{library.name}")
                    token.raise_if_cancelled()
                    remote_library_path = (
                        f"{remote_root}/lib:{home.strip()}/.local/d7-factory-agent/lib"
                    )
                    hello = client.start(remote_binary, remote_config, remote_library_path)
                    if hello.get("protocol") != "d7-factory-agent-jsonl":
                        raise RuntimeError("Orin agent 协议握手失败")
                    client.request("state.subscribe")
                    motor_service = OrinMotorService(client)
                else:
                    warning = (
                        "未提供 ARM64 agent 或经验证的 D7 电机限位配置；"
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
        self._close_serial()
        report(100, "已断开")

    def _close_resources(self) -> None:
        errors: list[Exception] = []
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
        transport = self._firmware_transport()
        target_id = int(payload["target_id"])
        iap_id = int(payload["iap_id"])
        target_name = str(payload["target"])

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
        controller = self._serial(payload)
        operation_name = action.removeprefix("serial485.")

        def operation(token: CancellationToken, report) -> object:
            controller.on_progress = report
            comm_id = int(payload.get("station_id", 0))
            if operation_name == "scan":
                return controller.scan(token)
            if operation_name == "read_identity":
                return {"present": controller.probe(comm_id), "comm_id": comm_id}
            if operation_name == "write_identity":
                controller.write_comm_id(int(payload["new_id"]), token)
            elif operation_name == "enable":
                controller.set_controlword(comm_id, CONTROLWORD_ENABLE, token)
            elif operation_name == "release_brake":
                controller.set_controlword(comm_id, CONTROLWORD_BRAKE_RELEASE, token)
            elif operation_name == "set_velocity":
                value = float(payload.get("rad_s", 0))
                controller.set_speed(comm_id, "forward" if value > 0 else "reverse" if value < 0 else "stop", token)
            elif operation_name == "move_relative":
                controller.move_relative(comm_id, angle_degrees=float(payload["angle_deg"]), token=token)
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

        self._start(action, operation)

    def _serial(self, payload: dict[str, Any]) -> Serial485Controller:
        port = str(payload.get("port", ""))
        baud = int(payload.get("baud", 115200))
        config = Serial485Config(port, baud)
        if self._serial_service is None or self._serial_service.config != config:
            self._close_serial()
            self._serial_service = Serial485Service(config)
            self._serial_service.open()
            self._serial_controller = Serial485Controller(
                self._serial_service,
                on_log=lambda message: self.state.log("485", message),
            )
        assert self._serial_controller is not None
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
        service = self._require_motor_service()
        target = dict(payload.get("target", {}))
        operation_name = action.removeprefix("motor.")
        unlocked_operations = {
            "enable",
            "set_mode",
            "set_position",
            "set_velocity",
            "param_write",
            "param_save",
            "zero_prepare",
            "zero_commit",
            "long_test_start",
        }
        if operation_name in unlocked_operations and self.state.safety_locked:
            raise PermissionError("本次连接会话尚未解除安全锁")

        def operation(token: CancellationToken, report) -> object:
            if operation_name in {"enable", "disable", "clear_errors"}:
                getattr(service, operation_name)(target)
                return {}
            if operation_name == "set_mode":
                service.set_mode(target, str(payload["mode"]))
            elif operation_name == "set_position":
                service.set_position(target, float(payload["angle_deg"]))
            elif operation_name == "set_velocity":
                service.set_velocity(target, float(payload["rad_s"]), int(payload["accel_time_ms"]))
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
            self._cancel_action("diagnostics.stress_start")
            return
        session = self._require_remote()
        service = DiagnosticService(self.state.evt, session)

        def operation(token: CancellationToken, report) -> object:
            def emit(line: str, is_error: bool) -> None:
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
                    for request in configure_requests(config):
                        results.append(session.execute(request, token, emit))
                    report(round(index * 100 / len(self.state.evt.interfaces)), config.name)
                return {"configured": [config.name for config in self.state.evt.interfaces.values()]}
            if action == "diagnostics.stress_start":
                profile = DiagnosticProfile(str(payload["profile"]))
                from d7_factory_studio.features.diagnostics.profiles import safe_random_ids

                clear_dmesg = bool(payload.get("clear_dmesg", False))
                if clear_dmesg and not self._setting_enabled("safety/allow_dmesg_clear"):
                    raise PermissionError("设置中未允许 dmesg -C 高风险操作")
                options = DiagnosticOptions(
                    profile=profile,
                    fixed_can_id=safe_random_ids(self.state.evt)[0],
                    duration_s=int(payload["duration_s"]),
                    gap_ms=int(payload["gap_ms"]),
                    clear_dmesg=clear_dmesg,
                    high_risk_dmesg_clear_confirmed=clear_dmesg,
                )
                result = service.run(tuple(payload["interfaces"]), options, token, emit)
                run_id = time.strftime("%Y%m%d-%H%M%S") + "-diagnostics"
                bundle = ReportBundleWriter().write(self.settings.report_directory / run_id, result)
                return {"result": result, "bundle": bundle}
            if action == "diagnostics.node_param_read" or action == "diagnostics.node_param_write":
                return service.run_node_parameters(
                    str(self.settings.value("diagnostics/node_binary", "~/d7/bin/joint_param")),
                    token,
                    logic_ids=tuple(int(value) for value in payload.get("logic_ids", [])),
                    write_then_read=action.endswith("write"),
                    on_output=emit,
                )
            if action == "diagnostics.broadcast":
                return service.run_broadcast(self.state.active_interface, 60, token, on_output=emit)
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
        transport = self._require_can()

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

        interface = self.state.active_interface
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
            self.state.lock(f"Orin agent 安全事件: {message.get('reason') or message.get('message')}")

    def _require_can(self) -> CanTransport:
        transport = self._resources.can_transport
        if transport is None:
            if self._resources.mode is ConnectionMode.ORIN_REMOTE:
                client = self._require_agent()
                interface = self.state.evt.interfaces[self.state.active_interface]
                transport = AgentCanTransport(client, self.state.active_interface)
                transport.open(0, interface.mode)
                self._resources.can_transport = transport
            else:
                raise RuntimeError("CAN 尚未连接")
        return transport

    def _require_remote(self) -> RemoteSession:
        if self._resources.remote_session is None:
            raise RuntimeError("Orin SSH 尚未连接")
        return self._resources.remote_session

    def _require_agent(self) -> OrinAgentClient:
        if self._resources.agent_client is None or not self._resources.agent_client.is_running:
            raise RuntimeError("Orin motor agent 未部署或未运行；请提供 ARM64 agent 与经验证的 D7 限位配置")
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
        packaged = base / "d7_factory_studio" / "agent" / "aarch64" / "d7-factory-agent"
        if packaged.is_file():
            return packaged
        return Path(__file__).resolve().parents[2] / "artifacts" / "agent" / "aarch64" / "d7-factory-agent"

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
