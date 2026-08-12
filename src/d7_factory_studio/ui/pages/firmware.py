from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import ConnectionMode, LinkState
from d7_factory_studio.features.firmware import (
    FirmwareImage,
    UpgradeOptions,
    build_upgrade_preview_frames,
)
from d7_factory_studio.features.firmware.profiles import battery_profile
from d7_factory_studio.protocols.iap import IapProtocol
from d7_factory_studio.protocols.pace_bms_upgrade import BLOCK_SIZE, firmware_identifier
from d7_factory_studio.ui.controls import D7ComboBox
from d7_factory_studio.ui.pages.base import FormSection, InlineMessage, LogConsole, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, PageHeader


class FirmwareTargetPanel(QWidget):
    def __init__(self, state: ApplicationState, target: str) -> None:
        super().__init__()
        self.state = state
        self._pending_source_mode: ConnectionMode | None = None
        self._connect_can_after_disconnect = False
        self.target = target
        self._query_busy = False
        self._upgrade_busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(14)

        settings = Card("升级设置")
        form = FormSection()
        self.protocol = D7ComboBox()
        if target == "battery":
            self.protocol.addItem("Pace BIN（V9213 实机验证）", "pace_bin")
            self.protocol.addItem("D7 IAP（0x16）", "d7_iap")
            form.add_field("升级协议", self.protocol)
        self.target_id = QLineEdit("0x18" if target == "pmu" else "0x42")
        self.target_id.setPlaceholderText("IAP 协议目标字节，例如 0x42")
        self.iap_id = QLineEdit("0x7FF")
        self.battery_address = QLineEdit("0")
        self.battery_address.setPlaceholderText("Pace 地址 0–16，默认 0")
        file_row = QWidget()
        file_layout = QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.setSpacing(8)
        self.file_path = QLineEdit()
        self.file_path.setReadOnly(True)
        self.file_path.setPlaceholderText("选择 D7 固件文件")
        browse = QPushButton("选择文件")
        browse.clicked.connect(self._browse)
        file_layout.addWidget(self.file_path, 1)
        file_layout.addWidget(browse)
        self.target_id_label = form.add_field("目标设备 ID", self.target_id)
        self.iap_id_label = form.add_field("IAP CAN ID", self.iap_id)
        self.battery_address_label = form.add_field("电池地址", self.battery_address)
        form.add_field("固件文件", file_row)
        self.firmware_details = QLabel("尚未校验固件")
        self.firmware_details.setObjectName("Muted")
        self.firmware_details.setWordWrap(True)
        form.add_field("固件信息", self.firmware_details)
        settings.body.addWidget(form)
        if target == "battery":
            self.quick_widget = QWidget()
            quick = QHBoxLayout(self.quick_widget)
            quick.setContentsMargins(0, 0, 0, 0)
            quick.addWidget(QLabel("快速选择"))
            for value in ("0x41", "0x42", "0x43"):
                button = QPushButton(value)
                button.clicked.connect(
                    lambda _checked=False, selected=value: self._set_battery_target(selected)
                )
                quick.addWidget(button)
            quick.addStretch(1)
            settings.body.addWidget(self.quick_widget)
            self.protocol.currentIndexChanged.connect(self._update_protocol_fields)
            self._update_protocol_fields()
        else:
            self.protocol.addItem("D7 IAP（0x16）", "d7_iap")
            self.battery_address.hide()
            self.battery_address_label.hide()
        layout.addWidget(settings)

        device_info = Card("D7 IAP 设备信息")
        self.info_hint = QLabel("连接设备后可读取当前运行角色和软件版本。")
        self.info_hint.setObjectName("Muted")
        device_info.body.addWidget(self.info_hint)
        info_form = FormSection()
        role_row = QWidget()
        role_layout = QHBoxLayout(role_row)
        role_layout.setContentsMargins(0, 0, 0, 0)
        self.role_value = QLabel("未查询")
        self.role_value.setStyleSheet("font-weight:700; color:#163B73;")
        self.query_role_button = QPushButton("查询角色")
        self.query_role_button.clicked.connect(lambda: self._query_device_info("role"))
        role_layout.addWidget(self.role_value)
        role_layout.addStretch(1)
        role_layout.addWidget(self.query_role_button)
        version_row = QWidget()
        version_layout = QHBoxLayout(version_row)
        version_layout.setContentsMargins(0, 0, 0, 0)
        self.version_value = QLabel("未查询")
        self.version_value.setStyleSheet("font-weight:700; color:#163B73;")
        self.query_version_button = QPushButton("查询版本")
        self.query_version_button.clicked.connect(lambda: self._query_device_info("version"))
        version_layout.addWidget(self.version_value)
        version_layout.addStretch(1)
        version_layout.addWidget(self.query_version_button)
        info_form.add_field("当前设备角色", role_row)
        info_form.add_field("当前软件版本", version_row)
        device_info.body.addWidget(info_form)
        layout.addWidget(device_info)

        status = Card("升级任务")
        initial_message = (
            "Pace 模式使用 29 位扩展经典 CAN，仅支持 CAN 盒和 .bin 固件。"
            if self._is_pace()
            else "选择固件后先执行校验；连接设备前不会发送任何 CAN 帧。"
        )
        self.message = InlineMessage(initial_message)
        status.body.addWidget(self.message)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        status.body.addWidget(self.progress)
        elapsed_row = QHBoxLayout()
        elapsed_row.addWidget(QLabel("任务用时"))
        self.elapsed_value = QLabel("00:00:00.000")
        self.elapsed_value.setStyleSheet("font-family:'JetBrains Mono','Cascadia Mono'; font-weight:700;")
        elapsed_row.addWidget(self.elapsed_value)
        elapsed_row.addStretch(1)
        status.body.addLayout(elapsed_row)
        self.elapsed_clock = QElapsedTimer()
        self.elapsed_tick = QTimer(self)
        self.elapsed_tick.setInterval(100)
        self.elapsed_tick.timeout.connect(self._update_elapsed)
        actions = QHBoxLayout()
        validate = QPushButton("校验固件")
        validate.clicked.connect(self._validate)
        self.preview = QPushButton("模拟升级帧")
        self.preview.clicked.connect(self._preview_upgrade)
        self.start = QPushButton("开始升级")
        self.start.setProperty("primary", True)
        self.start.clicked.connect(self._start)
        self.stop = QPushButton("停止")
        self.stop.setProperty("danger", True)
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self._stop)
        actions.addWidget(validate)
        if target == "battery":
            actions.addWidget(self.preview)
        else:
            self.preview.hide()
        actions.addStretch(1)
        actions.addWidget(self.stop)
        actions.addWidget(self.start)
        status.body.addLayout(actions)
        layout.addWidget(status)

        logs = Card("升级日志")
        self.console = LogConsole()
        logs.body.addWidget(self.console)
        log_actions = QHBoxLayout()
        log_actions.addStretch(1)
        export_log = QPushButton("导出日志")
        export_log.clicked.connect(self._export_log)
        log_actions.addWidget(export_log)
        logs.body.addLayout(log_actions)
        layout.addWidget(logs)
        layout.addStretch(1)
        state.task_event.connect(self._on_task_event)
        state.changed.connect(self._sync_action_controls)
        if target == "battery":
            self._update_protocol_fields()
        else:
            self._sync_action_controls()

    def _browse(self) -> None:
        file_filter = (
            "Pace BIN 固件 (*.bin);;所有文件 (*)"
            if self._is_pace()
            else "固件文件 (*.bin *.hex);;所有文件 (*)"
        )
        path, _ = QFileDialog.getOpenFileName(self, "选择固件", "", file_filter)
        if path:
            self.file_path.setText(path)
            self._validate()

    def _validate_can_id(self, text: str) -> int:
        value = int(text.strip(), 0)
        if not 0 <= value <= 0x7FF:
            raise ValueError("CAN ID 必须在 0x000–0x7FF")
        return value

    def _validate_target_id(self, text: str) -> int:
        value = int(text.strip(), 0)
        if not 0 <= value <= 0xFF:
            raise ValueError("IAP 目标设备 ID 是协议内 1 字节，必须在 0x00–0xFF")
        return value

    def _validate_battery_address(self, text: str) -> int:
        value = int(text.strip(), 0)
        if not 0 <= value <= 16:
            raise ValueError("Pace 电池地址必须在 0–16")
        return value

    def _is_pace(self) -> bool:
        return self.target == "battery" and self.protocol.currentData() == "pace_bin"

    def _update_protocol_fields(self) -> None:
        pace = self._is_pace()
        for widget in (self.target_id, self.target_id_label, self.iap_id, self.iap_id_label):
            widget.setVisible(not pace)
        self.battery_address.setVisible(pace)
        self.battery_address_label.setVisible(pace)
        if hasattr(self, "quick_widget"):
            self.quick_widget.setVisible(not pace)
        self.file_path.clear()
        if hasattr(self, "firmware_details"):
            self.firmware_details.setText("尚未校验固件")
        if hasattr(self, "message"):
            self.message.set_text(
                "Pace 模式使用 29 位扩展经典 CAN，仅支持 CAN 盒和 .bin 固件。"
                if pace
                else "选择固件后先执行校验；连接设备前不会发送任何 CAN 帧。"
            )
        if hasattr(self, "info_hint"):
            self.info_hint.setText(
                "Pace BIN 不使用 D7 IAP 角色/版本命令；切换到 D7 IAP 后可查询。"
                if pace
                else "连接设备后可读取当前运行角色和软件版本。"
            )
        self._sync_action_controls()

    def _sync_action_controls(self) -> None:
        if not hasattr(self, "query_role_button"):
            return
        connected = self.state.link_state.value == "connected"
        idle = not self._query_busy and not self._upgrade_busy
        query_enabled = connected and idle and not self._is_pace()
        self.query_role_button.setEnabled(query_enabled)
        self.query_version_button.setEnabled(query_enabled)
        pace_remote = (
            self._is_pace()
            and self.state.connection_mode is ConnectionMode.ORIN_REMOTE
        )
        self.start.setEnabled(idle and not pace_remote)
        self.start.setToolTip("Pace BIN 需要 29 位扩展帧，仅支持 CAN 盒" if pace_remote else "")
        self.preview.setEnabled(idle and not self._is_pace())

    def _set_battery_target(self, value: str) -> None:
        self.target_id.setText(value)

    def _query_device_info(self, kind: str) -> None:
        if self._is_pace():
            QMessageBox.information(
                self,
                "当前协议不支持",
                "角色和版本属于 D7 IAP 命令，请先把升级协议切换为 D7 IAP。",
            )
            return
        if self.state.link_state.value != "connected":
            QMessageBox.warning(self, "通信未连接", "请先选择通信来源并完成连接。")
            return
        try:
            payload = {
                "target": self.target,
                "target_id": self._validate_target_id(self.target_id.text()),
                "iap_id": self._validate_can_id(self.iap_id.text()),
            }
        except ValueError as exc:
            self.message.set_text(str(exc))
            return
        self._query_busy = True
        self._sync_action_controls()
        action = "firmware.query_role" if kind == "role" else "firmware.query_version"
        self.state.request(action, **payload)
        self.console.appendPlainText(f"[查询] {'设备角色' if kind == 'role' else '软件版本'}")

    def _validate(self) -> bool:
        try:
            path = Path(self.file_path.text())
            if not path.is_file():
                raise ValueError("请先选择存在的固件文件")
            size = path.stat().st_size
            if size == 0:
                raise ValueError("固件文件为空")
            if self._is_pace():
                address = self._validate_battery_address(self.battery_address.text())
                if path.suffix.lower() != ".bin":
                    raise ValueError("Pace V9213 升级只支持 .bin 固件")
                identifier = firmware_identifier(path)
                blocks = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
            else:
                target_id = self._validate_target_id(self.target_id.text())
                iap_id = self._validate_can_id(self.iap_id.text())
                image = FirmwareImage.from_file(path)
        except (ValueError, OSError) as exc:
            self.message.set_text(str(exc))
            self.console.appendPlainText(f"[校验失败] {exc}")
            return False
        if self._is_pace():
            detail = (
                f"地址 {address} · 型号 {identifier} · {blocks} 个 128-byte 块"
            )
            self.firmware_details.setText(
                f"Size {size:,} bytes · 型号 {identifier} · 写入块数 {blocks}"
            )
        else:
            detail = f"目标 0x{target_id:X} / IAP 0x{iap_id:X}"
            self.firmware_details.setText(
                f"Size {image.size:,} bytes · SP 0x{image.initial_sp:08X} · "
                f"ResetVector 0x{image.reset_vector:08X} · APP"
            )
        self.message.set_text(f"校验通过 · {path.name} · {size:,} bytes · {detail}")
        self.console.appendPlainText(f"[校验通过] {path} ({size} bytes)")
        return True

    def _start(self) -> None:
        if not self._validate():
            return
        if self.state.link_state.value != "connected":
            QMessageBox.warning(self, "通信未连接", "请先选择通信来源并完成连接。")
            return
        if self._is_pace() and self.state.connection_mode.value != "pc_direct":
            QMessageBox.warning(self, "通信方式不支持", "Pace BIN 升级当前仅支持 CAN 盒。")
            return
        payload = {"target": self.target, "firmware": self.file_path.text()}
        if self._is_pace():
            payload.update(
                protocol="pace_bin",
                battery_address=self._validate_battery_address(self.battery_address.text()),
            )
        else:
            payload.update(
                protocol="d7_iap",
                target_id=self._validate_target_id(self.target_id.text()),
                iap_id=self._validate_can_id(self.iap_id.text()),
            )
        self._upgrade_busy = True
        self._sync_action_controls()
        self.elapsed_clock.restart()
        self.elapsed_tick.start()
        self._update_elapsed()
        self.state.request("firmware.start", **payload)
        self.state.log("升级", f"已请求开始{'MCU' if self.target == 'pmu' else '电池'}升级")
        self.stop.setEnabled(True)

    def _stop(self) -> None:
        self.state.request("firmware.cancel", target=self.target)
        self.state.log("升级", "已请求停止升级", "warning")
        self.stop.setEnabled(False)

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action in {
            f"firmware.query_role.{self.target}",
            f"firmware.query_version.{self.target}",
        }:
            self._on_query_event(action, event, payload)
            return
        if action != f"firmware.start.{self.target}":
            return
        if event == "progress" and isinstance(payload, dict):
            self.progress.setValue(int(payload.get("progress", 0)))
            message = str(payload.get("message", ""))
            if message:
                self.message.set_text(message)
                self.console.appendPlainText(message)
        elif event == "succeeded":
            self._upgrade_busy = False
            self.elapsed_tick.stop()
            self._update_elapsed()
            self.progress.setValue(100)
            self.message.set_text("升级完成，结果已写入任务记录。")
            self.console.appendPlainText("[完成] 固件升级成功")
            self._sync_action_controls()
            self.stop.setEnabled(False)
        elif event == "failed":
            self._upgrade_busy = False
            self.elapsed_tick.stop()
            self._update_elapsed()
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            self.message.set_text(f"升级失败：{error}")
            self.console.appendPlainText(f"[失败] {error}")
            self._sync_action_controls()
            self.stop.setEnabled(False)
        elif event == "cancelled":
            self._upgrade_busy = False
            self.elapsed_tick.stop()
            self._update_elapsed()
            self.message.set_text("升级已停止。")
            self.console.appendPlainText("[停止] 用户取消升级")
            self._sync_action_controls()
            self.stop.setEnabled(False)

    def _on_query_event(self, action: str, event: str, payload: object) -> None:
        if event == "succeeded" and isinstance(payload, dict):
            if action.startswith("firmware.query_role"):
                value = str(payload.get("role", "未知"))
                self.role_value.setText(value)
                self.console.appendPlainText(f"[查询完成] 当前设备角色：{value}")
            else:
                value = str(payload.get("version", "未知"))
                self.version_value.setText(value)
                self.console.appendPlainText(f"[查询完成] 当前软件版本：{value}")
        elif event == "failed":
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            self.message.set_text(f"设备信息查询失败：{error}")
            self.console.appendPlainText(f"[查询失败] {error}")
        if event in {"succeeded", "failed", "cancelled"}:
            self._query_busy = False
            self._sync_action_controls()

    def _preview_upgrade(self) -> None:
        if self.target != "battery" or self._is_pace():
            self.message.set_text("模拟升级帧是原 D7 IAP 电池功能，请先切换到 D7 IAP。")
            return
        if not self._validate():
            return
        try:
            image = FirmwareImage.from_file(self.file_path.text())
            target_id = self._validate_target_id(self.target_id.text())
            iap_id = self._validate_can_id(self.iap_id.text())
            profile = battery_profile(target_id, can_id=iap_id)
            frames = build_upgrade_preview_frames(
                IapProtocol(target_id, iap_id),
                image,
                UpgradeOptions.for_profile(profile),
                max_data_frames=10,
            )
        except ValueError as exc:
            self.message.set_text(f"模拟失败：{exc}")
            return
        self.console.appendPlainText("[模拟电池升级] 以下报文仅打印，不会发送到 CAN 总线")
        for index, (label, frame) in enumerate(frames, start=1):
            data = " ".join(f"{value:02X}" for value in frame.data)
            self.console.appendPlainText(
                f"模拟TX[{index:02d}] {label}：ID=0x{frame.arbitration_id:X}, "
                f"Len={len(frame.data)}, Data={data}"
            )
        self.message.set_text(f"模拟完成：已生成 {len(frames)} 帧，未发送任何 CAN 数据。")

    def _export_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出升级日志",
            f"d7-{'pmu' if self.target == 'pmu' else 'battery'}-upgrade.log",
            "日志文件 (*.log *.txt);;所有文件 (*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(self.console.toPlainText(), encoding="utf-8")
        except OSError as exc:
            self.message.set_text(f"导出日志失败：{exc}")
            return
        self.message.set_text(f"升级日志已导出：{path}")

    def _update_elapsed(self) -> None:
        elapsed_ms = self.elapsed_clock.elapsed() if self.elapsed_clock.isValid() else 0
        hours, remainder = divmod(elapsed_ms, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1_000)
        self.elapsed_value.setText(
            f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
        )


class FirmwarePage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(
            PageHeader("OTA 升级", "通过 CAN 盒或 Orin 升级 MCU 与电池，过程按阶段校验并记录。")
        )
        source = Card("通信来源")
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("升级通道"))
        self.source = D7ComboBox()
        self.source.addItem("CAN 盒", ConnectionMode.PC_DIRECT.value)
        self.source.addItem("Orin", ConnectionMode.ORIN_REMOTE.value)
        self.source.currentIndexChanged.connect(self._source_changed)
        source_row.addWidget(self.source)
        source_row.addStretch(1)
        self.source_status = QLabel("未连接")
        self.source_status.setObjectName("Muted")
        source_row.addWidget(self.source_status)
        self.connect_can = QPushButton("打开 CAN 盒")
        self.connect_can.clicked.connect(self._toggle_can_box)
        source_row.addWidget(self.connect_can)
        source.body.addLayout(source_row)
        self.layout.addWidget(source)

        self.tabs = QTabWidget()
        self.mcu_panel = FirmwareTargetPanel(state, "pmu")
        self.battery_panel = FirmwareTargetPanel(state, "battery")
        self.tabs.addTab(self.mcu_panel, "MCU 升级")
        self.tabs.addTab(self.battery_panel, "电池升级")
        self.layout.addWidget(self.tabs)
        self.layout.addStretch(1)
        state.changed.connect(self._refresh_source)
        state.task_event.connect(self._on_connection_event)
        self._refresh_source()

    def _selected_mode(self) -> ConnectionMode:
        return ConnectionMode(str(self.source.currentData()))

    def _source_changed(self) -> None:
        mode = self._selected_mode()
        if self.state.connection_mode is not mode and self.state.link_state is LinkState.CONNECTED:
            self._pending_source_mode = mode
            self.state.request("connection.disconnect")
        elif self.state.connection_mode is not mode:
            self.state.set_connection_mode(mode)
        self._refresh_source()
        self.mcu_panel._sync_action_controls()
        self.battery_panel._sync_action_controls()

    def _refresh_source(self) -> None:
        if self.state.link_state is LinkState.CONNECTED:
            index = self.source.findData(self.state.connection_mode.value)
            if index >= 0 and index != self.source.currentIndex():
                self.source.blockSignals(True)
                self.source.setCurrentIndex(index)
                self.source.blockSignals(False)
        mode = self._selected_mode()
        connected = self.state.connection_mode is mode and self.state.link_state is LinkState.CONNECTED
        self.source_status.setText("已连接" if connected else "未连接")
        self.connect_can.setVisible(mode is ConnectionMode.PC_DIRECT)
        self.connect_can.setText("关闭 CAN 盒" if connected else "打开 CAN 盒")

    def _toggle_can_box(self) -> None:
        connected = (
            self.state.connection_mode is ConnectionMode.PC_DIRECT
            and self.state.link_state is LinkState.CONNECTED
        )
        if connected:
            self.state.request("connection.disconnect")
            return
        if self.state.link_state is LinkState.CONNECTED:
            self._pending_source_mode = ConnectionMode.PC_DIRECT
            self._connect_can_after_disconnect = True
            self.state.request("connection.disconnect")
            return
        self._begin_can_box_connect()

    def _begin_can_box_connect(self) -> None:
        self.state.set_connection_mode(ConnectionMode.PC_DIRECT)
        self.state.set_link_state(LinkState.CONNECTING)
        self.state.request(
            "connection.connect",
            mode=ConnectionMode.PC_DIRECT.value,
            evt=self.state.evt.variant,
        )

    def _on_connection_event(self, action: str, event: str, _payload: object) -> None:
        if action != "connection.disconnect":
            return
        if event == "succeeded" and self._pending_source_mode is not None:
            mode = self._pending_source_mode
            self._pending_source_mode = None
            self.state.set_connection_mode(mode)
            if self._connect_can_after_disconnect:
                self._connect_can_after_disconnect = False
                self._begin_can_box_connect()
        elif event in {"failed", "cancelled"}:
            self._pending_source_mode = None
            self._connect_can_after_disconnect = False
