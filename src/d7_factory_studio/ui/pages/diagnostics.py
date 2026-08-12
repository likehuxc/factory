from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.evt import interface_role_label
from d7_factory_studio.core.models import ConnectionMode, LinkState
from d7_factory_studio.features.diagnostics.broadcast import (
    parse_broadcast_frame,
    parse_live_response_event,
    responding_node,
)
from d7_factory_studio.ui.controls import D7ComboBox as QComboBox
from d7_factory_studio.ui.controls import D7SpinBox as QSpinBox
from d7_factory_studio.ui.controls import D7TableWidget as QTableWidget
from d7_factory_studio.ui.pages.base import LogConsole, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, PageHeader, clear_layout


def _diagnostic_interfaces(state: ApplicationState) -> tuple[str, ...]:
    """Return CAN interfaces that contain configured motor nodes."""
    return tuple(sorted({node.bus for node in state.evt.nodes}, key=lambda name: int(name[3:])))


def _link_test_interfaces(state: ApplicationState) -> tuple[str, ...]:
    """Return all production CAN links, including the MCU communication channel."""
    return tuple(sorted(state.evt.interfaces, key=lambda name: int(name[3:])))


def _activity_message(level_and_message: str) -> str:
    _level, separator, message = level_and_message.partition("|")
    return message if separator else level_and_message


class LinkTestPage(WorkbenchPage):
    """Orin CAN link stress test page."""

    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self._topology_signature: tuple[object, ...] | None = None
        self._last_report: Path | None = None
        self.layout.setSpacing(12)
        self.layout.addWidget(PageHeader("链路测试", "对指定 Orin CAN 通道执行链路循环测试。"))

        setup = Card("测试配置")
        interface_row = QHBoxLayout()
        interface_row.addWidget(QLabel("CAN 通道"))
        self.interface_checks_widget = QWidget()
        self.interface_checks = QHBoxLayout(self.interface_checks_widget)
        self.interface_checks.setContentsMargins(0, 0, 0, 0)
        self.interface_checks.setSpacing(8)
        interface_row.addWidget(self.interface_checks_widget, 1)
        setup.body.addLayout(interface_row)

        option_row = QHBoxLayout()
        self.loop_test = QCheckBox("循环测试")
        self.loop_test.setChecked(True)
        option_row.addWidget(self.loop_test)
        option_row.addStretch(1)
        self.diag_stop = QPushButton("停止")
        self.diag_stop.setProperty("danger", True)
        self.diag_stop.setEnabled(False)
        self.diag_stop.clicked.connect(self._stop_stress)
        self.diag_start = QPushButton("开始测试")
        self.diag_start.setProperty("primary", True)
        self.diag_start.clicked.connect(self._start_stress)
        option_row.addWidget(self.diag_stop)
        option_row.addWidget(self.diag_start)
        setup.body.addLayout(option_row)
        self.layout.addWidget(setup)

        summary = Card("测试结果")
        self.diag_progress = QProgressBar()
        self.diag_progress.setRange(0, 100)
        summary.body.addWidget(self.diag_progress)
        result_row = QHBoxLayout()
        result_row.addWidget(QLabel("结果"))
        self.result_value = QLabel("未开始")
        self.result_value.setObjectName("Muted")
        result_row.addWidget(self.result_value)
        result_row.addSpacing(24)
        result_row.addWidget(QLabel("报告路径"))
        self.report_path = QLabel("-")
        self.report_path.setObjectName("Muted")
        self.report_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.report_path.setWordWrap(True)
        result_row.addWidget(self.report_path, 1)
        self.open_report = QPushButton("打开报告")
        self.open_report.setEnabled(False)
        self.open_report.clicked.connect(self._open_report)
        result_row.addWidget(self.open_report)
        summary.body.addLayout(result_row)
        self.layout.addWidget(summary)

        log = Card("运行日志")
        self.diag_console = LogConsole()
        self.diag_console.setMinimumHeight(190)
        log.body.addWidget(self.diag_console)
        self.layout.addWidget(log, 1)

        state.changed.connect(self._rebuild_interfaces)
        state.task_event.connect(self._on_task_event)
        state.activity_added.connect(self._on_activity)
        self._rebuild_interfaces()

    def _rebuild_interfaces(self) -> None:
        names = _link_test_interfaces(self.state)
        signature = (self.state.evt.variant, names)
        if signature == self._topology_signature:
            return
        selected = set(self._selected_interfaces())
        had_topology = self._topology_signature is not None
        self._topology_signature = signature
        clear_layout(self.interface_checks)
        for name in names:
            interface = self.state.evt.interfaces[name]
            role = "MCU" if interface.role == "ota" else interface_role_label(interface.role)
            button = QPushButton(f"{role} {name.upper()}")
            button.setCheckable(True)
            button.setChecked(name in selected if had_topology else True)
            button.setProperty("choice", True)
            button.setProperty("compact", True)
            button.setProperty("interface", name)
            button.setFixedWidth(164)
            button.setMinimumWidth(140)
            if interface.role == "ota":
                button.setToolTip("MCU 通信通道 · Classic CAN 500 kbit/s")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.interface_checks.addWidget(button)
        self.interface_checks.addStretch(1)

    def _selected_interfaces(self) -> list[str]:
        return [
            str(widget.property("interface"))
            for index in range(self.interface_checks.count())
            if isinstance((widget := self.interface_checks.itemAt(index).widget()), QPushButton)
            and widget.isChecked()
        ]

    def _start_stress(self) -> None:
        if (
            self.state.connection_mode is not ConnectionMode.ORIN_REMOTE
            or self.state.link_state is not LinkState.CONNECTED
        ):
            QMessageBox.warning(self, "Orin 未连接", "请先通过顶部全局连接区连接 Orin。")
            return
        interfaces = self._selected_interfaces()
        if not interfaces:
            QMessageBox.warning(self, "未选择 CAN", "请至少选择一个 CAN 通道。")
            return
        self.state.request(
            "diagnostics.stress_start",
            interfaces=interfaces,
            profile="quick",
            duration_s=60,
            gap_ms=1,
            capture_dmesg=True,
            capture_candump=True,
            clear_dmesg=True,
            setup_can_script="~/setup_can.sh",
            setup_each_stage=True,
            loop=self.loop_test.isChecked(),
        )
        self.result_value.setText("运行中")
        self._last_report = None
        self.report_path.setText("-")
        self.open_report.setEnabled(False)
        self.diag_progress.setValue(0)
        self.diag_start.setEnabled(False)
        self.diag_stop.setEnabled(True)

    def _stop_stress(self) -> None:
        self.state.request("diagnostics.stress_cancel")
        self.diag_stop.setEnabled(False)

    def _open_report(self) -> None:
        path = self._last_report
        if path is None or not path.is_file():
            self.open_report.setEnabled(False)
            QMessageBox.warning(self, "报告不存在", "测试报告文件不存在，请重新执行链路测试。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))):
            QMessageBox.warning(self, "无法打开报告", f"系统无法打开报告：\n{path}")

    def _on_activity(self, timestamp: str, source: str, level_and_message: str) -> None:
        if source in {"诊断", "璇婃柇"}:
            self.diag_console.appendPlainText(
                f"[{timestamp}] {_activity_message(level_and_message)}"
            )

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action != "diagnostics.stress_start":
            return
        if event == "started":
            self.result_value.setText("运行中")
            self.diag_start.setEnabled(False)
            self.diag_stop.setEnabled(True)
        elif event == "progress" and isinstance(payload, dict):
            self.diag_progress.setValue(int(payload.get("progress", 0)))
            if message := str(payload.get("message", "")):
                self.diag_console.appendPlainText(message)
        elif event == "succeeded":
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            evaluation = result.get("evaluation", {}) if isinstance(result, dict) else {}
            verdict = str(evaluation.get("verdict", "FAIL")).upper() if isinstance(evaluation, dict) else "FAIL"
            execution = result.get("execution", {}) if isinstance(result, dict) else {}
            execution_status = (
                str(execution.get("status", "complete"))
                if isinstance(execution, dict)
                else "complete"
            )
            planned = int(execution.get("planned_stage_count", 0)) if isinstance(execution, dict) else 0
            executed = int(execution.get("executed_stage_count", 0)) if isinstance(execution, dict) else 0
            labels = {"PASS": "通过", "WARN": "警告", "FAIL": "失败"}
            if execution_status == "not_started":
                self.result_value.setText("未执行（前置检查失败）")
                self.diag_progress.setValue(0)
                self.diag_console.appendPlainText(
                    "[中止] 诊断环境未通过，未发送链路测试流量。请按日志处理后重试。"
                )
            elif execution_status == "partial":
                self.result_value.setText(f"{labels.get(verdict, '失败')}（部分执行 {executed}/{planned}）")
                self.diag_progress.setValue(round(executed * 100 / max(1, planned)))
                self.diag_console.appendPlainText(
                    f"[中止] 计划 {planned} 个阶段，实际执行 {executed} 个阶段。"
                )
            else:
                self.result_value.setText(labels.get(verdict, "失败"))
                self.diag_progress.setValue(100)
            if isinstance(evaluation, dict):
                for finding in evaluation.get("findings", []):
                    self.diag_console.appendPlainText(f"[结论] {finding}")
            bundle = payload.get("bundle", {}) if isinstance(payload, dict) else {}
            path = bundle.get("report_html", "") if isinstance(bundle, dict) else ""
            self.report_path.setText(str(path) or "-")
            self._last_report = Path(str(path)) if path else None
            self.open_report.setEnabled(bool(self._last_report and self._last_report.is_file()))
            self.diag_console.appendPlainText(f"[报告] {path or '未生成'}")
        elif event == "failed":
            self.result_value.setText("失败")
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            self.diag_console.appendPlainText(f"[失败] {error}")
        elif event == "cancelled":
            self.result_value.setText("已停止")
            self.diag_console.appendPlainText("[停止] 链路测试已取消，远程进程已清理")
        if event in {"succeeded", "failed", "cancelled"}:
            self.diag_start.setEnabled(True)
            self.diag_stop.setEnabled(False)


class NodeTestPage(WorkbenchPage):
    """Motor node broadcast and parameter test page."""

    LOGIC_ID_COLUMN = 3
    PARAMETER_COLUMN = 6
    BROADCAST_COLUMN = 7
    CAN_COLORS = {
        "can0": "#246BFD",
        "can1": "#16845B",
        "can2": "#7C3AED",
        "can4": "#D97706",
        "can5": "#D97706",
        "can7": "#7C3AED",
    }

    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self._topology_signature: tuple[object, ...] | None = None
        self._parameter_status: dict[int, str] = {}
        self._broadcast_status: dict[int, str] = {}
        self._broadcast_live_counts: dict[int, int] = {}
        self._broadcast_running_interface = ""
        self._channel_buttons: list[QPushButton] = []
        self.layout.setSpacing(12)
        self.layout.addWidget(PageHeader("电机节点测试", "选择 CAN 通道，检查节点广播应答和参数。"))

        channel_strip = QWidget()
        channel_layout = QHBoxLayout(channel_strip)
        channel_layout.setContentsMargins(0, 0, 0, 0)
        channel_layout.setSpacing(8)
        channel_layout.addWidget(QLabel("CAN 通道"))
        self.interface_buttons_widget = QWidget()
        self.interface_buttons = QHBoxLayout(self.interface_buttons_widget)
        self.interface_buttons.setContentsMargins(0, 0, 0, 0)
        self.interface_buttons.setSpacing(6)
        channel_layout.addWidget(self.interface_buttons_widget, 1)
        self.layout.addWidget(channel_strip)

        workspace = QWidget()
        self.workspace = workspace
        workspace.setMinimumHeight(430)
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(12)

        inventory = Card("电机 ID 表")
        self.inventory_card = inventory
        inventory.body.setContentsMargins(14, 12, 14, 14)
        inventory.body.setSpacing(10)
        inventory.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        inventory_toolbar = QHBoxLayout()
        inventory_toolbar.setSpacing(8)
        inventory_toolbar.addStretch(1)
        self.node_count = QLabel("已选 0 / 当前 0")
        self.node_count.setObjectName("Muted")
        inventory_toolbar.addWidget(self.node_count)
        select_all = QPushButton("全选")
        select_all.setProperty("compact", True)
        select_all.clicked.connect(lambda: self._set_all_nodes(True))
        clear_all = QPushButton("清空")
        clear_all.setProperty("compact", True)
        clear_all.clicked.connect(lambda: self._set_all_nodes(False))
        inventory_toolbar.addWidget(select_all)
        inventory_toolbar.addWidget(clear_all)
        inventory.body.addLayout(inventory_toolbar)

        # The combo remains the single source of truth for CAN selection. Compact
        # channel buttons drive it so broadcast and parameter actions stay linked.
        self.node_interface = QComboBox(self.content)
        self.node_interface.hide()
        self.node_interface.currentIndexChanged.connect(self._on_interface_changed)

        self.node_table = QTableWidget(0, 8)
        self.node_table.setHorizontalHeaderLabels(
            ["选择", "节点", "配置名", "逻辑 ID", "设备 CAN ID", "CAN", "参数状态", "广播应答"]
        )
        header = self.node_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column in range(3, 8):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        self.node_table.setColumnWidth(0, 44)
        self.node_table.setColumnWidth(1, 112)
        self.node_table.setColumnWidth(3, 92)
        self.node_table.setColumnWidth(4, 102)
        self.node_table.setColumnWidth(5, 116)
        self.node_table.setColumnWidth(6, 92)
        self.node_table.setColumnWidth(7, 120)
        self.node_table.setMinimumWidth(620)
        self.node_table.setMinimumHeight(330)
        self.node_table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        inventory.body.addWidget(self.node_table, 1)
        workspace_layout.addWidget(inventory, 7)

        action_panel = QWidget()
        self.action_panel = action_panel
        action_panel.setMinimumWidth(280)
        action_panel.setMaximumWidth(350)
        action_layout = QVBoxLayout(action_panel)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(12)

        broadcast_card = Card("广播应答")
        broadcast_card.body.setContentsMargins(14, 12, 14, 14)
        broadcast_card.body.setSpacing(10)
        broadcast_fields = QHBoxLayout()
        broadcast_fields.addWidget(QLabel("广播时长"))
        self.broadcast_duration = QSpinBox()
        self.broadcast_duration.setRange(1, 300)
        self.broadcast_duration.setValue(10)
        self.broadcast_duration.setSuffix(" s")
        broadcast_fields.addWidget(self.broadcast_duration, 1)
        broadcast_card.body.addLayout(broadcast_fields)
        self.broadcast_detail = QLabel("等待开始")
        self.broadcast_detail.setObjectName("Muted")
        broadcast_card.body.addWidget(self.broadcast_detail)
        self.broadcast_progress = QProgressBar()
        self.broadcast_progress.setRange(0, 100)
        self.broadcast_progress.setValue(0)
        self.broadcast_progress.setTextVisible(False)
        broadcast_card.body.addWidget(self.broadcast_progress)
        self.broadcast = QPushButton("发送广播并监听")
        self.broadcast.setProperty("primary", True)
        self.broadcast.clicked.connect(self._run_broadcast)
        self.broadcast.setMinimumHeight(40)
        broadcast_card.body.addWidget(self.broadcast)
        metrics = QHBoxLayout()
        metrics.setSpacing(6)
        self.response_count = self._add_metric(metrics, "已应答")
        self.missing_count = self._add_metric(metrics, "未应答")
        self.frame_count = self._add_metric(metrics, "应答帧")
        broadcast_card.body.addLayout(metrics)
        broadcast_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        action_layout.addWidget(broadcast_card)

        parameters = Card("参数检查")
        parameters.body.setContentsMargins(14, 12, 14, 14)
        parameters.body.setSpacing(10)
        self.read_button = QPushButton("只读校验")
        self.read_button.setMinimumHeight(40)
        self.read_button.clicked.connect(self._read_nodes)
        parameters.body.addWidget(self.read_button)
        self.write_button = QPushButton("写入后校验")
        self.write_button.setMinimumHeight(40)
        self.write_button.setProperty("primary", True)
        self.write_button.clicked.connect(self._write_nodes)
        parameters.body.addWidget(self.write_button)
        self.parameter_detail = QLabel("等待选择电机")
        self.parameter_detail.setObjectName("Muted")
        parameters.body.addWidget(self.parameter_detail)
        self.parameter_progress = QProgressBar()
        self.parameter_progress.setRange(0, 100)
        self.parameter_progress.setValue(0)
        self.parameter_progress.setTextVisible(False)
        parameters.body.addWidget(self.parameter_progress)
        parameters.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        action_layout.addWidget(parameters)
        action_layout.addStretch(1)
        workspace_layout.addWidget(action_panel, 3)
        self.layout.addWidget(workspace)

        log = Card()
        self.log_card = log
        log.body.setContentsMargins(14, 12, 14, 14)
        log.body.setSpacing(8)
        log_header = QHBoxLayout()
        log_title = QLabel("实时输出")
        log_title.setObjectName("SectionTitle")
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        clear_log = QPushButton("清屏")
        clear_log.setProperty("compact", True)
        log_header.addWidget(clear_log)
        log.body.addLayout(log_header)
        self.diag_console = LogConsole()
        clear_log.clicked.connect(self.diag_console.clear)
        self.diag_console.setMinimumHeight(160)
        self.diag_console.setMaximumHeight(210)
        log.body.addWidget(self.diag_console)
        self.layout.addWidget(log)

        state.changed.connect(self._rebuild_interfaces)
        state.task_event.connect(self._on_task_event)
        state.activity_added.connect(self._on_activity)
        self._rebuild_interfaces()

    def _interface_label(self, name: str) -> str:
        interface = self.state.evt.interfaces.get(name)
        if interface is None:
            return name.upper()
        role = interface_role_label(interface.role)
        return f"{role} {name.upper()}" if role else name.upper()

    @staticmethod
    def _add_metric(layout: QHBoxLayout, label: str) -> QLabel:
        box = QWidget()
        box.setStyleSheet("background:#F4F7FB; border:1px solid #E1E7EF; border-radius:6px;")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(9, 7, 9, 7)
        box_layout.setSpacing(1)
        value = QLabel("0")
        value.setStyleSheet("border:none; font-size:18px; font-weight:700; color:#172033;")
        caption = QLabel(label)
        caption.setStyleSheet("border:none; font-size:12px; color:#667085;")
        box_layout.addWidget(value)
        box_layout.addWidget(caption)
        layout.addWidget(box, 1)
        return value

    def _on_interface_changed(self) -> None:
        self._sync_channel_buttons()
        self._rebuild_node_table()

    def _sync_channel_buttons(self) -> None:
        current = self.node_interface.currentData()
        for button in self._channel_buttons:
            if button.property("interface") == current:
                button.setChecked(True)
                break

    def _rebuild_interfaces(self) -> None:
        names = _diagnostic_interfaces(self.state)
        signature = (
            self.state.evt.variant,
            names,
            tuple((node.logic_id, node.dev_id, node.bus) for node in self.state.evt.nodes),
        )
        if signature == self._topology_signature:
            return
        previous = self.node_interface.currentData()
        if self._topology_signature is not None:
            self._parameter_status.clear()
            self._broadcast_status.clear()
        self._topology_signature = signature
        self.node_interface.blockSignals(True)
        self.node_interface.clear()
        clear_layout(self.interface_buttons)
        self._channel_buttons.clear()
        for name in names:
            count = len(self.state.evt.nodes_for_bus(name))
            label = self._interface_label(name)
            self.node_interface.addItem(
                f"{label} · {count} 个节点",
                name,
            )
            button = QPushButton(f"{label} ({count})")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setProperty("choice", True)
            button.setProperty("compact", True)
            button.setProperty("interface", name)
            index = self.node_interface.count() - 1
            button.setFixedWidth(164)
            button.clicked.connect(
                lambda _checked=False, target=index: self.node_interface.setCurrentIndex(target)
            )
            self.interface_buttons.addWidget(button)
            self._channel_buttons.append(button)
        self.interface_buttons.addStretch(1)
        index = self.node_interface.findData(previous)
        self.node_interface.setCurrentIndex(index if index >= 0 else 0)
        self.node_interface.blockSignals(False)
        self._sync_channel_buttons()
        self._rebuild_node_table()

    def _rebuild_node_table(self) -> None:
        interface = self.node_interface.currentData()
        nodes = self.state.evt.nodes_for_bus(str(interface)) if interface else ()
        channel_label = self._interface_label(str(interface)) if interface else "当前 CAN"
        self.node_table.setRowCount(0)
        for node in nodes:
            row = self.node_table.rowCount()
            self.node_table.insertRow(row)
            selected = QCheckBox()
            selected.setProperty("logic_id", node.logic_id)
            selected.toggled.connect(lambda _checked=False: self._update_selection_summary())
            self.node_table.setCellWidget(row, 0, selected)
            values = (
                node.label,
                node.name,
                f"{node.logic_id} (0x{node.logic_id:02X})",
                f"0x{node.dev_id:02X}",
                channel_label,
                self._parameter_status.get(node.logic_id, "未测试"),
                self._broadcast_status.get(node.logic_id, "未测试"),
            )
            for column, value in enumerate(values, 1):
                item = QTableWidgetItem(str(value))
                if column != 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 1:
                    item.setToolTip(node.label)
                if column == 2:
                    item.setFont(QFont("Consolas"))
                    item.setToolTip(node.name)
                if column == self.LOGIC_ID_COLUMN:
                    item.setData(Qt.ItemDataRole.UserRole, node.logic_id)
                    item.setFont(QFont("Consolas"))
                if column == 5:
                    item.setForeground(QColor(self.CAN_COLORS.get(node.bus, "#246BFD")))
                self.node_table.setItem(row, column, item)
            self._style_status_item(self.node_table.item(row, self.PARAMETER_COLUMN))
            self._style_status_item(self.node_table.item(row, self.BROADCAST_COLUMN))
            self.node_table.setRowHeight(row, 42)
        self._update_selection_summary()
        self.broadcast.setText(f"发送 {channel_label} 广播并监听")
        self.broadcast_detail.setText(f"{channel_label} · 等待开始")

    def _update_selection_summary(self) -> None:
        selected = len(self._selected_nodes())
        total = self.node_table.rowCount()
        self.node_count.setText(f"已选 {selected} / 当前 {total}")
        if self.parameter_progress.maximum() != 0:
            self.parameter_detail.setText(
                f"已选择 {selected} 个电机" if selected else "等待选择电机"
            )

    def _set_all_nodes(self, checked: bool) -> None:
        for row in range(self.node_table.rowCount()):
            widget = self.node_table.cellWidget(row, 0)
            if isinstance(widget, QCheckBox):
                widget.setChecked(checked)
        self._update_selection_summary()

    def _selected_nodes(self) -> list[int]:
        return [
            int(self.node_table.item(row, self.LOGIC_ID_COLUMN).data(Qt.ItemDataRole.UserRole))
            for row in range(self.node_table.rowCount())
            if isinstance((widget := self.node_table.cellWidget(row, 0)), QCheckBox)
            and widget.isChecked()
        ]

    def _selected_interface(self) -> str:
        return str(self.node_interface.currentData() or "")

    def _read_nodes(self) -> None:
        nodes = self._selected_nodes()
        if not nodes:
            QMessageBox.warning(self, "未选择节点", "请至少选择一个节点。")
            return
        self._set_parameter_pending(nodes, "读取校验")
        self.state.request(
            "diagnostics.node_param_read",
            interface=self._selected_interface(),
            logic_ids=nodes,
        )

    def _write_nodes(self) -> None:
        nodes = self._selected_nodes()
        if not nodes:
            QMessageBox.warning(self, "未选择节点", "请至少选择一个节点。")
            return
        answer = QMessageBox.question(
            self,
            "写入节点参数",
            f"确认向 {self._interface_label(self._selected_interface())} 的 "
            f"{len(nodes)} 个节点写入参数并回读？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_parameter_pending(nodes, "写入并回读")
        self.state.request(
            "diagnostics.node_param_write",
            interface=self._selected_interface(),
            logic_ids=nodes,
        )

    def _set_parameter_pending(self, nodes: list[int], operation: str) -> None:
        for logic_id in nodes:
            self._parameter_status[logic_id] = "测试中"
        self.parameter_progress.setRange(0, 0)
        self.parameter_detail.setText(f"{operation} · {len(nodes)} 个电机")
        self._refresh_status_columns()

    def _run_broadcast(self) -> None:
        interface = self._selected_interface()
        if not interface:
            QMessageBox.warning(self, "未选择 CAN", "请选择广播 CAN 通道。")
            return
        for node in self.state.evt.nodes_for_bus(interface):
            self._broadcast_status[node.logic_id] = "监听中"
        self.broadcast_progress.setRange(0, 0)
        self.broadcast_detail.setText(
            f"{self._interface_label(interface)} · 监听 {self.broadcast_duration.value()} 秒"
        )
        self.response_count.setText("0")
        self.missing_count.setText("0")
        self.frame_count.setText("0")
        self._broadcast_live_counts.clear()
        self._broadcast_running_interface = interface
        self._refresh_status_columns()
        self.state.request(
            "diagnostics.broadcast",
            interface=interface,
            duration_s=self.broadcast_duration.value(),
        )

    def _refresh_status_columns(self) -> None:
        for row in range(self.node_table.rowCount()):
            logic_id = int(
                self.node_table.item(row, self.LOGIC_ID_COLUMN).data(Qt.ItemDataRole.UserRole)
            )
            self.node_table.item(row, self.PARAMETER_COLUMN).setText(
                self._parameter_status.get(logic_id, "未测试")
            )
            self.node_table.item(row, self.BROADCAST_COLUMN).setText(
                self._broadcast_status.get(logic_id, "未测试")
            )
            self._style_status_item(self.node_table.item(row, self.PARAMETER_COLUMN))
            self._style_status_item(self.node_table.item(row, self.BROADCAST_COLUMN))

    @staticmethod
    def _style_status_item(item: QTableWidgetItem) -> None:
        status = item.text()
        if status in {"PASS", "通过", "完成"} or status.startswith("已应答"):
            foreground, background = "#16845B", "#E7F6EF"
        elif status in {"FAIL", "失败", "无应答"}:
            foreground, background = "#C43F45", "#FDEBEC"
        elif status in {"测试中", "监听中"}:
            foreground, background = "#B76B12", "#FFF4DD"
        else:
            foreground, background = "#596579", "#EEF1F5"
        item.setForeground(QColor(foreground))
        item.setBackground(QColor(background))

    def _set_busy(self, action: str, busy: bool) -> None:
        if action == "diagnostics.broadcast":
            self.broadcast.setEnabled(not busy)
        elif action in {"diagnostics.node_param_read", "diagnostics.node_param_write"}:
            self.read_button.setEnabled(not busy)
            self.write_button.setEnabled(not busy)
        self.node_interface.setEnabled(not busy)
        for button in self._channel_buttons:
            button.setEnabled(not busy)

    def _on_activity(self, timestamp: str, source: str, level_and_message: str) -> None:
        if source in {"诊断", "璇婃柇"}:
            message = _activity_message(level_and_message)
            visible = self._consume_broadcast_output(message)
            if visible.strip():
                self.diag_console.appendPlainText(f"[{timestamp}] {visible}")

    def _consume_broadcast_output(self, output: str) -> str:
        interface = self._broadcast_running_interface
        if not interface:
            return output
        changed = False
        visible_lines: list[str] = []
        for line in output.splitlines():
            live_response = parse_live_response_event(line)
            if live_response is not None:
                event_interface, logic_id, count, _frame_id = live_response
                node = next(
                    (
                        item
                        for item in self.state.evt.nodes_for_bus(interface)
                        if item.logic_id == logic_id
                    ),
                    None,
                )
                if event_interface == interface and node is not None:
                    self._broadcast_live_counts[logic_id] = count
                    self._broadcast_status[logic_id] = f"已应答 {count} 帧"
                    changed = True
                continue
            parsed = parse_broadcast_frame(line)
            if parsed is None:
                visible_lines.append(line)
                continue
            if parsed[0] != interface:
                continue
            node = responding_node(self.state.evt, interface, parsed[1])
            if node is None:
                continue
            self._broadcast_live_counts[node.logic_id] = (
                self._broadcast_live_counts.get(node.logic_id, 0) + 1
            )
            self._broadcast_status[node.logic_id] = (
                f"已应答 {self._broadcast_live_counts[node.logic_id]} 帧"
            )
            changed = True
        if changed:
            self.response_count.setText(str(len(self._broadcast_live_counts)))
            self.frame_count.setText(str(sum(self._broadcast_live_counts.values())))
            self._refresh_status_columns()
        return "\n".join(visible_lines)

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        supported = {
            "diagnostics.broadcast",
            "diagnostics.node_param_read",
            "diagnostics.node_param_write",
        }
        if action not in supported:
            return
        if event == "started":
            self._set_busy(action, True)
        elif event in {"succeeded", "failed", "cancelled"}:
            self._set_busy(action, False)
        if event == "output" and action == "diagnostics.broadcast" and isinstance(payload, dict):
            self._consume_broadcast_output(str(payload.get("output", "")))
            return
        if event == "progress" and isinstance(payload, dict):
            progress = int(payload.get("progress", 0))
            if action == "diagnostics.broadcast":
                self.broadcast_progress.setRange(0, 100)
                self.broadcast_progress.setValue(progress)
            else:
                self.parameter_progress.setRange(0, 100)
                self.parameter_progress.setValue(progress)
            if message := str(payload.get("message", "")):
                self.diag_console.appendPlainText(message)
            return
        if event == "failed":
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            self.diag_console.appendPlainText(f"[失败] {action}: {error}")
            if action == "diagnostics.broadcast":
                self._broadcast_running_interface = ""
                self._replace_pending_status(self._broadcast_status, "监听中", "失败")
                self.broadcast_progress.setRange(0, 100)
                self.broadcast_progress.setValue(0)
                self.broadcast_detail.setText("广播失败")
            else:
                self._replace_pending_status(self._parameter_status, "测试中", "失败")
                self.parameter_progress.setRange(0, 100)
                self.parameter_progress.setValue(0)
                self.parameter_detail.setText("参数检查失败")
            self._refresh_status_columns()
            return
        if event == "cancelled":
            self.diag_console.appendPlainText(f"[停止] {action} 已取消")
            if action == "diagnostics.broadcast":
                self._broadcast_running_interface = ""
                self._replace_pending_status(self._broadcast_status, "监听中", "已停止")
                self.broadcast_progress.setRange(0, 100)
                self.broadcast_progress.setValue(0)
                self.broadcast_detail.setText("已停止")
            else:
                self._replace_pending_status(self._parameter_status, "测试中", "已停止")
                self.parameter_progress.setRange(0, 100)
                self.parameter_progress.setValue(0)
                self.parameter_detail.setText("已停止")
            self._refresh_status_columns()
            return
        if event != "succeeded" or not isinstance(payload, dict):
            return
        if action in {"diagnostics.node_param_read", "diagnostics.node_param_write"}:
            for item in payload.get("nodes", []):
                if isinstance(item, dict) and "logic_id" in item:
                    self._parameter_status[int(item["logic_id"])] = str(
                        item.get("verdict", "完成")
                    )
            self.parameter_progress.setRange(0, 100)
            self.parameter_progress.setValue(100)
            self.parameter_detail.setText(f"已完成 · {len(payload.get('nodes', []))} 个电机")
            self._refresh_status_columns()
            self.diag_console.appendPlainText(f"[完成] {action}")
            return
        self._broadcast_running_interface = ""
        raw_responses = payload.get("responses", {})
        responses = {int(value) for value in raw_responses} if isinstance(raw_responses, dict) else set()
        interface = str(payload.get("interface", self._selected_interface()))
        nodes = self.state.evt.nodes_for_bus(interface)
        for node in nodes:
            details = raw_responses.get(node.logic_id, raw_responses.get(str(node.logic_id), {}))
            count = int(details.get("count", 0)) if isinstance(details, dict) else 0
            self._broadcast_status[node.logic_id] = f"已应答 {count} 帧" if count else "无应答"
        frame_total = int(payload.get("response_frame_count", 0))
        if not frame_total and isinstance(raw_responses, dict):
            frame_total = sum(
                int(details.get("count", 0)) if isinstance(details, dict) else 1
                for details in raw_responses.values()
            )
        self.broadcast_progress.setRange(0, 100)
        self.broadcast_progress.setValue(100)
        self.broadcast_detail.setText(
            f"{self._interface_label(interface)} · {len(responses)}/{len(nodes)} 个节点应答"
        )
        self.response_count.setText(str(len(responses)))
        self.missing_count.setText(str(max(0, len(nodes) - len(responses))))
        self.frame_count.setText(str(frame_total))
        self._refresh_status_columns()
        self.diag_console.appendPlainText(
            f"[广播] {self._interface_label(interface)} · "
            f"{len(responses)}/{len(nodes)} 个节点应答"
        )

    @staticmethod
    def _replace_pending_status(statuses: dict[int, str], pending: str, replacement: str) -> None:
        for logic_id, status in tuple(statuses.items()):
            if status == pending:
                statuses[logic_id] = replacement


class DiagnosticsPage(WorkbenchPage):
    """Compatibility shell for existing navigation registrations."""

    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.tabs = QTabWidget()
        self.link_page = LinkTestPage(state)
        self.node_page = NodeTestPage(state)
        self.tabs.addTab(self.link_page, "链路测试")
        self.tabs.addTab(self.node_page, "电机节点测试")
        self.layout.addWidget(self.tabs)

        # Keep the former public attributes usable until navigation is registered per page.
        self.interface_checks = self.link_page.interface_checks
        self.node_interface = self.node_page.node_interface
        self.node_table = self.node_page.node_table
        self.broadcast = self.node_page.broadcast
        self.diag_start = self.link_page.diag_start
        self.diag_stop = self.link_page.diag_stop
        self.diag_progress = self.link_page.diag_progress
        self.diag_console = self.link_page.diag_console

    def _selected_interfaces(self) -> list[str]:
        return self.link_page._selected_interfaces()

    def _selected_nodes(self) -> list[int]:
        return self.node_page._selected_nodes()

    def _run_broadcast(self) -> None:
        self.node_page._run_broadcast()

    def _read_nodes(self) -> None:
        self.node_page._read_nodes()

    def _write_nodes(self) -> None:
        self.node_page._write_nodes()

    def _start_stress(self) -> None:
        self.link_page._start_stress()

    def _stop_stress(self) -> None:
        self.link_page._stop_stress()
