from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.evt import group_label
from d7_factory_studio.core.models import ConnectionMode, LinkState
from d7_factory_studio.settings_store import SettingsStore
from d7_factory_studio.ui.controls import (
    D7ComboBox as QComboBox,
)
from d7_factory_studio.ui.controls import (
    D7DoubleSpinBox as QDoubleSpinBox,
)
from d7_factory_studio.ui.controls import (
    D7SpinBox as QSpinBox,
)
from d7_factory_studio.ui.controls import D7TableWidget as QTableWidget
from d7_factory_studio.ui.icons import lucide_icon
from d7_factory_studio.ui.pages.base import FormSection, InlineMessage, LogConsole, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, FunctionConnectionBar, Metric, PageHeader


def add_motor_targets(combo: QComboBox, state: ApplicationState, include_groups: bool = True) -> None:
    combo.clear()
    if include_groups:
        for group, ids in state.evt.fixed_groups.items():
            combo.addItem(f"分组 · {group_label(group)} ({len(ids)})", ("group", group))
        combo.insertSeparator(combo.count())
    for node in state.evt.nodes:
        combo.addItem(
            f"{node.logic_id:02d} · {node.label} · {node.bus.upper()} / 0x{node.dev_id:02X}",
            ("motor", node.logic_id),
        )


def require_motion_ready(widget: QWidget, state: ApplicationState) -> bool:
    if state.connection_mode is not ConnectionMode.ORIN_REMOTE or state.link_state is not LinkState.CONNECTED:
        QMessageBox.warning(widget, "Orin 未连接", "请先在顶部状态栏连接 Orin。")
        return False
    return True


def serial_servo_state(statusword: int) -> str:
    """Interpret the low seven status bits exactly as JiHua ServoStudio does."""
    state = int(statusword) & 0x7F
    if state == 55:
        return "使能"
    if state in {8, 24}:
        return "故障"
    if state in {0, 7, 15, 23, 31, 33, 35, 49, 51, 64, 80}:
        return "禁能"
    return "未定义"


class ModuleDiagramDialog(QDialog):
    def __init__(self, pixmap: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source_pixmap = pixmap
        self.setWindowTitle("D7 模组示意图")
        self.setMinimumSize(460, 640)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 12)
        layout.setSpacing(10)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.image_label, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(620, 860)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self._fit_image()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._fit_image()

    def _fit_image(self) -> None:
        target = self.image_label.size()
        if target.width() <= 0 or target.height() <= 0:
            return
        self.image_label.setPixmap(
            self._source_pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class MotorIdPage(WorkbenchPage):
    """Single-motor production flow for assigning D7 communication IDs."""

    def __init__(self, state: ApplicationState, settings: SettingsStore | None = None) -> None:
        super().__init__()
        self.state = state
        self.settings = settings or SettingsStore()
        self._serial_open = False
        self._pending_logic_id: int | None = None
        self._pending_target_id: int | None = None
        self._status_variant = ""
        self._status_by_logic_id: dict[int, str] = {}
        self._tables: list[QTableWidget] = []

        self.layout.addWidget(
            PageHeader("电机 ID 写入", "一次只连接一台新电机；双击关节行即可完成写入、重启和读回验证。")
        )
        self.layout.addWidget(
            InlineMessage(
                "新电机默认通信 ID 为 1。底盘轮毂电机完成 ID 写入后会继续执行动力线相序与编码器偏置辨识。",
                "info",
            )
        )

        tools = QHBoxLayout()
        verify_card = Card(
            "当前电机", "先确认顶部 485 已打开，再连接当前通信 ID；连接后在左侧显示电机返回数据。"
        )
        verify_form = FormSection()
        self.current_id = QSpinBox()
        self.current_id.setRange(0, 255)
        self.current_id.setValue(1)
        self.current_id.setDisplayIntegerBase(16)
        self.current_id.setPrefix("0x")
        verify_form.add_field("通信 ID", self.current_id)
        verify_card.body.addWidget(verify_form)
        verify_actions = QHBoxLayout()
        self.connection_status = QLabel("未连接")
        self.connection_status.setObjectName("Muted")
        self.connection_status.setWordWrap(True)
        connect = QPushButton("点击连接")
        connect.setProperty("primary", True)
        connect.clicked.connect(self._connect_motor)
        verify_actions.addWidget(self.connection_status)
        verify_actions.addStretch(1)
        verify_actions.addWidget(connect)
        verify_card.body.addLayout(verify_actions)
        tools.addWidget(verify_card, 1)

        address_card = Card("轴地址工具", "检测未知 ID，或在单电机接线条件下恢复默认轴地址。")
        address_actions = QHBoxLayout()
        detect = QPushButton("检测轴地址")
        detect.clicked.connect(self._scan)
        reset = QPushButton("轴地址重置")
        reset.setProperty("danger", True)
        reset.clicked.connect(self._reset_axis)
        address_actions.addWidget(detect)
        address_actions.addWidget(reset)
        address_card.body.addLayout(address_actions)
        self.scan_result = QLabel("尚未检测")
        self.scan_result.setObjectName("Muted")
        address_card.body.addWidget(self.scan_result)
        tools.addWidget(address_card, 1)
        self.layout.addLayout(tools)

        table_card = Card("D7 电机清单", "选择区域后双击目标电机；写入结果会按机型自动记录。")
        summary_row = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setObjectName("Muted")
        summary_row.addWidget(self.summary)
        summary_row.addStretch(1)
        module_diagram = QPushButton("模组示意图")
        module_diagram.setIcon(lucide_icon("machine", "#52637A"))
        module_diagram.clicked.connect(self._show_module_diagram)
        summary_row.addWidget(module_diagram)
        clear_status = QPushButton("清空写入标记")
        clear_status.setIcon(lucide_icon("rotate-ccw", "#52637A"))
        clear_status.clicked.connect(self._clear_status_marks)
        summary_row.addWidget(clear_status)
        table_card.body.addLayout(summary_row)
        self.tabs = QTabWidget()
        table_card.body.addWidget(self.tabs)
        self.layout.addWidget(table_card)
        state.changed.connect(self._rebuild_tables)
        state.task_event.connect(self._on_task_event)
        self._rebuild_tables()

    def _show_module_diagram(self) -> None:
        diagram_path = Path(__file__).resolve().parents[2] / "resources" / "d7-module-diagram.png"
        diagram = QPixmap(str(diagram_path))
        if diagram.isNull():
            QMessageBox.warning(self, "图片加载失败", "无法加载 D7 模组示意图。")
            return
        ModuleDiagramDialog(diagram, self).exec()

    @staticmethod
    def _needs_phase_identification(node) -> bool:  # type: ignore[no-untyped-def]
        return node.name.startswith("wheel_")

    def _rebuild_tables(self, preserve_view: bool = True) -> None:
        self._ensure_status_variant()
        active_table = self.tabs.currentWidget() if preserve_view else None
        active_group = str(active_table.property("groupKey")) if active_table is not None else ""
        view_states: dict[str, tuple[int | None, int, int]] = {}
        old_tables = tuple(self._tables)
        if preserve_view:
            for table in old_tables:
                group_key = str(table.property("groupKey"))
                selected_logic_id: int | None = None
                current_row = table.currentRow()
                if current_row >= 0 and (item := table.item(current_row, 0)) is not None:
                    selected_logic_id = int(item.data(Qt.ItemDataRole.UserRole))
                vertical_bar = table.verticalScrollBar()
                horizontal_bar = table.horizontalScrollBar()
                view_states[group_key] = (
                    selected_logic_id,
                    vertical_bar.value(),
                    horizontal_bar.value(),
                )
        self.tabs.clear()
        self._tables.clear()
        for table in old_tables:
            table.deleteLater()
        grouped: dict[str, list[object]] = {}
        for node in self.state.evt.nodes:
            grouped.setdefault(group_label(node.group), []).append(node)
        for label, nodes in grouped.items():
            table = QTableWidget(0, 8)
            table.setProperty("groupKey", label)
            table.setHorizontalHeaderLabels(
                ["状态", "节点", "配置名", "名称", "逻辑 ID", "设备 CAN ID", "CAN", "寻相"]
            )
            header = table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            for column in (3, 4, 5, 6, 7):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            table.verticalHeader().setVisible(False)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setMinimumHeight(315)
            for node in nodes:
                row = table.rowCount()
                table.insertRow(row)
                status = self._status_by_logic_id.get(node.logic_id, "未写入")
                values = (
                    f"● {status}",
                    node.label,
                    node.name,
                    node.factory_name,
                    str(node.logic_id),
                    f"0x{node.dev_id:02X} ({node.dev_id})",
                    node.bus.upper(),
                    "写入后自动执行" if self._needs_phase_identification(node) else "不需要",
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, node.logic_id)
                    if column == 0:
                        self._style_status_item(item, status)
                    if column in {0, 3, 4, 5, 6, 7}:
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    table.setItem(row, column, item)
            table.cellDoubleClicked.connect(
                lambda row, _column, target=table: self._write_selected(target, row)
            )
            written = sum(
                self._status_by_logic_id.get(node.logic_id, "").startswith("已写入") for node in nodes
            )
            self.tabs.addTab(table, f"{label}  {written}/{len(nodes)}")
            self._tables.append(table)
        if active_group:
            for index, table in enumerate(self._tables):
                if str(table.property("groupKey")) == active_group:
                    self.tabs.setCurrentIndex(index)
                    break
        for table in self._tables:
            group_key = str(table.property("groupKey"))
            selected_logic_id, vertical_value, horizontal_value = view_states.get(group_key, (None, 0, 0))
            if selected_logic_id is not None:
                for row in range(table.rowCount()):
                    item = table.item(row, 0)
                    if item is not None and item.data(Qt.ItemDataRole.UserRole) == selected_logic_id:
                        table.setCurrentCell(row, 0)
                        table.selectRow(row)
                        break
            QTimer.singleShot(
                0,
                lambda target=table, vertical=vertical_value, horizontal=horizontal_value: (
                    target.verticalScrollBar().setValue(vertical),
                    target.horizontalScrollBar().setValue(horizontal),
                ),
            )
        done = sum(value.startswith("已写入") for value in self._status_by_logic_id.values())
        remaining = len(self.state.evt.nodes) - done
        self.summary.setText(f"ID 已写入 {done} / {len(self.state.evt.nodes)} 台 · 待写入 {remaining} 台")

    def _status_key(self) -> str:
        return f"motor_id/status/{self.state.evt.variant}"

    def _ensure_status_variant(self) -> None:
        if self._status_variant == self.state.evt.variant:
            return
        self._status_variant = self.state.evt.variant
        stored = self.settings.value(self._status_key(), "{}")
        try:
            values = json.loads(str(stored)) if stored else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            values = {}
        if not isinstance(values, dict):
            values = {}
        valid_ids = {node.logic_id for node in self.state.evt.nodes}
        self._status_by_logic_id = {
            int(logic_id): str(status)
            for logic_id, status in values.items()
            if str(logic_id).isdigit() and int(logic_id) in valid_ids and str(status).startswith("已写入")
        }

    def _persist_statuses(self) -> None:
        completed = {
            str(logic_id): ("已写入" if "中" in status or "等待" in status else status)
            for logic_id, status in self._status_by_logic_id.items()
            if status.startswith("已写入")
        }
        self.settings.set_value(self._status_key(), json.dumps(completed, ensure_ascii=True))

    @staticmethod
    def _style_status_item(item: QTableWidgetItem, status: str) -> None:
        if status.startswith("已写入") and "失败" not in status:
            foreground, background = "#15803D", "#ECFDF3"
        elif "失败" in status:
            foreground, background = "#C73737", "#FFF1F1"
        elif "中" in status or "等待" in status:
            foreground, background = "#9A6700", "#FFF8E1"
        else:
            foreground, background = "#6B778A", "#F4F6F9"
        item.setForeground(QBrush(QColor(foreground)))
        item.setBackground(QBrush(QColor(background)))
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setToolTip(status)

    def _clear_status_marks(self) -> None:
        if (
            QMessageBox.question(
                self,
                "清空写入标记",
                f"确认清空 {self.state.evt.variant} 的全部电机 ID 写入标记？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._status_by_logic_id.clear()
        self._persist_statuses()
        self._rebuild_tables()

    def _reset_page(self) -> None:
        """Reset the 485 session while retaining completed motor write marks."""
        self.current_id.setValue(1)
        self.connection_status.setText("未连接")
        self.connection_status.setToolTip("")
        self.scan_result.setText("尚未检测")
        self.scan_result.setToolTip("")
        self._clear_pending()
        # Completed write marks have their own explicit clear button. Reload
        # them from settings so only transient states such as writing, failed,
        # and phase-identification progress are discarded with the 485 session.
        self._status_variant = ""
        self._ensure_status_variant()
        self._rebuild_tables(preserve_view=False)
        self.tabs.setCurrentIndex(0)

    def _node_for_logic_id(self, logic_id: int):  # type: ignore[no-untyped-def]
        return next(node for node in self.state.evt.nodes if node.logic_id == logic_id)

    def _require_serial(self) -> bool:
        if self._serial_open:
            return True
        QMessageBox.warning(self, "485 未打开", "请先在顶部状态栏选择串口并打开 485。")
        return False

    def _connect_motor(self) -> None:
        if not self._require_serial():
            return
        self.connection_status.setText("正在连接并读取电机数据…")
        self.state.request("serial485.connect", station_id=self.current_id.value())

    def _scan(self) -> None:
        if not self._require_serial():
            return
        self.scan_result.setText("正在检测轴地址…")
        self.state.request("serial485.scan")

    def _reset_axis(self) -> None:
        if not self._require_serial():
            return
        if (
            QMessageBox.warning(
                self,
                "确认轴地址重置",
                "确认总线上只连接了一台电机。重置后需要按现场要求断电再上电。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.state.request("serial485.reset_identity")

    def _write_selected(self, table: QTableWidget, row: int) -> None:
        if not self._require_serial():
            return
        item = table.item(row, 0)
        if item is None:
            return
        logic_id = int(item.data(Qt.ItemDataRole.UserRole))
        node = self._node_for_logic_id(logic_id)
        phase_note = (
            "\n\n该轮毂电机写入后将自动执行两项寻相。" if self._needs_phase_identification(node) else ""
        )
        if (
            QMessageBox.question(
                self,
                "写入电机 ID",
                f"当前连接：0x{self.current_id.value():02X}\n目标电机：{node.label}\n"
                f"目标 ID：0x{node.dev_id:02X}{phase_note}",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._pending_logic_id = logic_id
        self._pending_target_id = node.dev_id
        self._status_by_logic_id[logic_id] = "写入中"
        self._rebuild_tables()
        self.state.request(
            "serial485.write_identity",
            station_id=self.current_id.value(),
            new_id=node.dev_id,
            logic_id=logic_id,
            phase_required=self._needs_phase_identification(node),
        )

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action == "serial485.open":
            if event == "succeeded":
                self._serial_open = True
                self._reset_page()
            elif event in {"started", "failed", "cancelled"}:
                self._serial_open = False
                self._reset_page()
            return
        if action == "serial485.close":
            if event in {"started", "succeeded", "failed", "cancelled"}:
                self._serial_open = False
                self._reset_page()
            return
        if action.startswith("serial485.") and not self._serial_open:
            # A cancelled worker can still have queued events when the serial
            # session is being closed. Never let those stale results repopulate
            # a page that has already been reset.
            self._serial_open = False
            return

        if action == "serial485.connect":
            if event == "succeeded":
                values = payload if isinstance(payload, dict) else {}
                current = values.get("comm_id", self.current_id.value())
                station = values.get("station", current)
                register_value = values.get("register_value")
                lines = [f"连接地址：0x{int(station):02X}（{int(station)}）"]
                parameters = values.get("parameters", [])
                parameter_values: dict[int, int] = {}
                if isinstance(parameters, list):
                    for item in parameters:
                        if not isinstance(item, dict):
                            continue
                        address = int(item.get("address", 0))
                        value = int(item.get("value", 0))
                        parameter_values[address] = value
                device_id = parameter_values.get(0x200129, values.get("device_id"))
                if device_id is not None:
                    lines.append(f"通信 ID：0x{int(device_id):02X}（{int(device_id)}）")
                for label, key in (
                    ("设备名称", "device_name"),
                    ("硬件版本", "hardware_version"),
                    ("软件版本", "software_version"),
                ):
                    text = str(values.get(key, "")).strip()
                    if text:
                        lines.append(f"{label}：{text}")
                if 0x200201 in parameter_values:
                    value = parameter_values[0x200201]
                    authority = {0: "485", 1: "CAN"}.get(value, "未知")
                    lines.append(f"当前控制权：{authority}（{value}）")
                for address, name in (
                    (0x604100, "伺服状态"),
                    (0x200804, "当前位置"),
                ):
                    if address in parameter_values:
                        value = parameter_values[address]
                        lines.append(f"{name} = {value}（0x{value & 0xFFFFFFFF:X}）")
                if not parameters and register_value is None:
                    lines.append("已连接，但电机参数未返回")
                self.current_id.setValue(int(current))
                self.connection_status.setText("\n".join(lines))
                self.connection_status.setToolTip(str(values.get("parameter_error", "")))
            elif event == "failed":
                error = payload.get("error", "未收到电机应答") if isinstance(payload, dict) else payload
                self.connection_status.setText(f"连接失败：{error}")
                self.connection_status.setToolTip(str(error))
        elif action == "serial485.scan" and event == "succeeded":
            station = getattr(payload, "station", None)
            comm_id = getattr(payload, "comm_id", None)
            if station is None:
                self.scan_result.setText("未检测到电机")
            else:
                self.current_id.setValue(int(comm_id))
                self.scan_result.setText(f"站号 0x{int(station):02X} · 通信 ID 0x{int(comm_id):02X}")
        elif action == "serial485.reset_identity" and event == "succeeded":
            self.current_id.setValue(1)
            self.scan_result.setText("重置指令已发送，请断电再上电")
        elif action == "serial485.write_identity" and self._pending_logic_id is not None:
            if event == "progress" and isinstance(payload, dict):
                message = str(payload.get("message", "写入中"))
                self._status_by_logic_id[self._pending_logic_id] = message
            elif event == "succeeded":
                node = self._node_for_logic_id(self._pending_logic_id)
                self.current_id.setValue(int(self._pending_target_id or node.dev_id))
                if self._needs_phase_identification(node):
                    self._status_by_logic_id[self._pending_logic_id] = "已写入 · 寻相中"
                    self._persist_statuses()
                    self.state.request(
                        "serial485.phase_identify",
                        station_id=node.dev_id,
                        logic_id=node.logic_id,
                    )
                else:
                    self._status_by_logic_id[self._pending_logic_id] = "已写入"
                    self._persist_statuses()
                    self._clear_pending()
            elif event in {"failed", "cancelled"}:
                self._status_by_logic_id[self._pending_logic_id] = "失败"
                self._clear_pending()
            self._rebuild_tables()
        elif action == "serial485.phase_identify" and self._pending_logic_id is not None:
            if event == "progress" and isinstance(payload, dict):
                message = str(payload.get("message", "寻相中"))
                self._status_by_logic_id[self._pending_logic_id] = f"已写入 · {message}"
            elif event == "succeeded":
                values = payload if isinstance(payload, dict) else {}
                sequence = values.get("phase_sequence_text", "-")
                offset = values.get("encoder_offset", "-")
                self._status_by_logic_id[self._pending_logic_id] = "已写入 · 寻相完成"
                self.connection_status.setText(f"寻相完成 · {sequence} · 编码器偏置 {offset}")
                self._persist_statuses()
                self._clear_pending()
            elif event in {"failed", "cancelled"}:
                self._status_by_logic_id[self._pending_logic_id] = "已写入 · 寻相失败"
                self._persist_statuses()
                self._clear_pending()
            self._rebuild_tables()

    def _clear_pending(self) -> None:
        self._pending_logic_id = None
        self._pending_target_id = None


class NodeOverviewPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self._motor_states: dict[int, dict[str, object]] = {}
        self.layout.addWidget(
            PageHeader("电机节点总览", "按当前 EVT 拓扑查看 30 个 D7 节点、总线归属与实时状态。")
        )
        self.layout.addWidget(FunctionConnectionBar(state, ((ConnectionMode.ORIN_REMOTE, "Orin"),)))
        metrics_card = Card()
        metrics = QHBoxLayout()
        self.total = Metric("配置节点", "30")
        self.online = Metric("在线", "0", "#21875A")
        self.faults = Metric("故障", "0", "#D94747")
        for item in (self.total, self.online, self.faults):
            metrics.addWidget(item)
        metrics.addStretch(1)
        metrics_card.body.addLayout(metrics)
        self.layout.addWidget(metrics_card)

        table_card = Card("节点目录", "实时状态由 Orin Agent 的电机状态流更新。")
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["逻辑 ID", "节点", "设备 ID", "总线", "分组", "模式", "状态", "故障"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table_card.body.addWidget(self.table)
        self.layout.addWidget(table_card)
        state.changed.connect(self.refresh)
        state.task_event.connect(self._on_task_event)
        self.refresh()

    def refresh(self) -> None:
        self.total.set_value(str(len(self.state.evt.nodes)))
        self.online.set_value(str(self.state.online_nodes))
        self.table.setRowCount(0)
        for node in self.state.evt.nodes:
            row = self.table.rowCount()
            self.table.insertRow(row)
            motor = self._motor_states.get(node.logic_id, {})
            faults = motor.get("faults", motor.get("fault", motor.get("errors", "—")))
            values = [
                str(node.logic_id),
                node.label,
                f"0x{node.dev_id:02X}",
                node.bus.upper(),
                group_label(node.group),
                str(motor.get("mode", "—")),
                "在线" if motor else "未连接",
                str(faults or "—"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {0, 2, 3}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action != "motor.state" or event != "event" or not isinstance(payload, dict):
            return
        data = payload.get("data", payload)
        motors = data.get("motors", []) if isinstance(data, dict) else []
        self._motor_states = {
            int(item.get("logic_id", item.get("id"))): item
            for item in motors
            if isinstance(item, dict) and item.get("logic_id", item.get("id")) is not None
        }
        self.refresh()


class CanMotionPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self._authority_target: tuple[str, object] | None = None
        self._pending_authority_target: tuple[str, object] | None = None
        self.layout.addWidget(
            PageHeader("CAN 运动控制", "统一控制单电机或固定分组；速度台架上限固定为 ±0.5 rad/s。")
        )
        self.layout.addWidget(FunctionConnectionBar(state, ((ConnectionMode.ORIN_REMOTE, "Orin"),)))
        self.layout.addWidget(
            InlineMessage("模式切换会先失能并读回校验；切换完成后保持失能，需要重新使能。", "warning")
        )

        row = QHBoxLayout()
        target_card = Card("控制目标")
        target_form = FormSection()
        self.target = QComboBox()
        add_motor_targets(self.target, state)
        self.target.currentIndexChanged.connect(self._authority_target_changed)
        self.mode = QComboBox()
        self.mode.addItem("位置模式", "position")
        self.mode.addItem("速度模式", "velocity")
        target_form.add_field("电机或固定分组", self.target)
        target_form.add_field("工作模式", self.mode)
        target_card.body.addWidget(target_form)
        authority_actions = QHBoxLayout()
        self.authority_status = QLabel("控制权：未切换")
        self.authority_status.setObjectName("Muted")
        take_control = QPushButton("获取控制权")
        take_control.clicked.connect(lambda: self._request_control("take_control"))
        release_control = QPushButton("释放控制权")
        release_control.clicked.connect(
            lambda: self._request_control("release_control", require_unlock=False)
        )
        authority_actions.addWidget(self.authority_status)
        authority_actions.addStretch(1)
        authority_actions.addWidget(release_control)
        authority_actions.addWidget(take_control)
        target_card.body.addLayout(authority_actions)
        mode_actions = QHBoxLayout()
        set_mode = QPushButton("切换模式")
        set_mode.clicked.connect(self._set_mode)
        clear = QPushButton("清除故障")
        clear.clicked.connect(lambda: self._request_control("clear_errors"))
        enable = QPushButton("使能")
        enable.setProperty("primary", True)
        enable.clicked.connect(lambda: self._request_control("enable"))
        disable = QPushButton("失能")
        disable.setProperty("danger", True)
        disable.clicked.connect(lambda: self._request_control("disable", require_unlock=False))
        mode_actions.addWidget(clear)
        mode_actions.addStretch(1)
        mode_actions.addWidget(disable)
        mode_actions.addWidget(enable)
        mode_actions.addWidget(set_mode)
        target_card.body.addLayout(mode_actions)
        row.addWidget(target_card, 1)

        command_card = Card("运动指令")
        form = FormSection()
        self.position = QDoubleSpinBox()
        self.position.setRange(-360.0, 360.0)
        self.position.setDecimals(3)
        self.position.setSuffix(" °")
        self.velocity = QDoubleSpinBox()
        self.velocity.setRange(-10.0, 10.0)
        self.velocity.setDecimals(3)
        self.velocity.setSingleStep(0.1)
        self.velocity.setSuffix(" rad/s")
        self.accel = QSpinBox()
        self.accel.setRange(1, 65535)
        self.accel.setValue(1000)
        self.accel.setSuffix(" ms")
        form.add_field("指定角度", self.position)
        form.add_field("目标速度", self.velocity)
        form.add_field("加速时间", self.accel)
        command_card.body.addWidget(form)
        command_actions = QHBoxLayout()
        emergency = QPushButton("紧急停止")
        emergency.setProperty("danger", True)
        emergency.clicked.connect(self._emergency_stop)
        execute = QPushButton("发送运动指令")
        execute.setProperty("primary", True)
        execute.clicked.connect(self._execute)
        command_actions.addWidget(emergency)
        command_actions.addStretch(1)
        command_actions.addWidget(execute)
        command_card.body.addLayout(command_actions)
        row.addWidget(command_card, 1)
        self.layout.addLayout(row)

        state_card = Card("实时状态")
        self.status_table = QTableWidget(0, 7)
        self.status_table.setHorizontalHeaderLabels(["节点", "模式", "位置", "速度", "电流", "温度", "故障"])
        self.status_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.status_table.verticalHeader().setVisible(False)
        self.status_table.setMinimumHeight(250)
        state_card.body.addWidget(self.status_table)
        self.layout.addWidget(state_card)
        state.changed.connect(self._evt_changed)
        state.task_event.connect(self._on_task_event)

    def _evt_changed(self) -> None:
        if self.state.link_state is not LinkState.CONNECTED:
            self._authority_target = None
        selected = self.target.currentData()
        add_motor_targets(self.target, self.state)
        index = self.target.findData(selected)
        if index >= 0:
            self.target.setCurrentIndex(index)

    def _target_payload(self) -> dict[str, object]:
        kind, value = self.target.currentData()
        return {kind: value}

    def _request_control(self, operation: str, require_unlock: bool = True) -> None:
        if require_unlock and not require_motion_ready(self, self.state):
            return
        if not require_unlock and self.state.link_state is not LinkState.CONNECTED:
            return
        if operation == "enable" and not self._has_control_authority():
            QMessageBox.warning(self, "尚未获取控制权", "请先为当前目标获取控制权，再执行使能。")
            return
        self._pending_authority_target = self.target.currentData()
        self.state.request(f"motor.{operation}", target=self._target_payload())
        self.state.log("电机", f"已请求{operation}: {self.target.currentText()}")

    def _set_mode(self) -> None:
        if not require_motion_ready(self, self.state):
            return
        if not self._has_control_authority():
            QMessageBox.warning(self, "尚未获取控制权", "请先为当前目标获取控制权，再切换模式。")
            return
        self.state.request("motor.set_mode", target=self._target_payload(), mode=self.mode.currentData())

    def _execute(self) -> None:
        if not require_motion_ready(self, self.state):
            return
        if not self._has_control_authority():
            QMessageBox.warning(self, "尚未获取控制权", "请先为当前目标获取控制权，再发送运动指令。")
            return
        mode = self.mode.currentData()
        args = (
            {"angle_deg": self.position.value()}
            if mode == "position"
            else {"rad_s": self.velocity.value(), "accel_time_ms": self.accel.value()}
        )
        self.state.request(f"motor.set_{mode}", target=self._target_payload(), **args)
        self.state.log("电机", f"已发送{'位置' if mode == 'position' else '速度'}指令", "warning")

    def _emergency_stop(self) -> None:
        self.state.request("motor.emergency_stop", target=self._target_payload())

    def _authority_target_changed(self) -> None:
        self.authority_status.setText("控制权：CAN（1）" if self._has_control_authority() else "控制权：未切换")

    def _has_control_authority(self) -> bool:
        return self._authority_target == self.target.currentData()

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action in {"motor.take_control", "motor.release_control"} and event == "succeeded":
            self._authority_target = (
                self._pending_authority_target if action == "motor.take_control" else None
            )
            self._pending_authority_target = None
            self.authority_status.setText(
                "控制权：CAN（1）"
                if action == "motor.take_control" and self._has_control_authority()
                else "控制权：已释放"
                if action == "motor.release_control"
                else "控制权：未切换"
            )
            return
        if action in {"motor.take_control", "motor.release_control"} and event in {
            "failed",
            "cancelled",
        }:
            self._pending_authority_target = None
            return
        if action != "motor.state" or event != "event" or not isinstance(payload, dict):
            return
        data = payload.get("data", payload)
        motors = data.get("motors", []) if isinstance(data, dict) else []
        self.status_table.setRowCount(0)
        for motor in motors:
            if not isinstance(motor, dict):
                continue
            row = self.status_table.rowCount()
            self.status_table.insertRow(row)
            values = (
                motor.get("label", motor.get("logic_id", motor.get("id", "—"))),
                motor.get("mode", "—"),
                motor.get("position_rad", motor.get("position", "—")),
                motor.get("velocity_rad_s", motor.get("velocity", "—")),
                motor.get("current_a", motor.get("current", "—")),
                motor.get("temperature_c", motor.get("temperature", "—")),
                motor.get("faults", motor.get("fault", motor.get("errors", "—"))),
            )
            for column, value in enumerate(values):
                self.status_table.setItem(row, column, QTableWidgetItem(str(value)))


class Serial485Page(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self._authority_comm_id: int | None = None
        self._pending_authority_comm_id: int | None = None
        self._serial_open = False
        self._serial_busy = False
        self.layout.addWidget(
            PageHeader("485 电机控制", "保留扫描、ID/EEPROM、抱闸、速度、相对位置和循环测试。")
        )
        row = QHBoxLayout()
        connection = Card("串口连接")
        form = FormSection()
        self.port = QComboBox()
        self.baud = QComboBox()
        self.baud.addItems(["115200", "921600", "1000000", "2000000"])
        self.baud.setCurrentText("115200")
        form.add_field("串口", self.port)
        form.add_field("波特率", self.baud)
        connection.body.addWidget(form)
        actions = QHBoxLayout()
        refresh = QPushButton("刷新串口")
        refresh.clicked.connect(self.refresh_ports)
        self.serial_status = QLabel("485：已关闭")
        self.serial_status.setObjectName("Muted")
        self.open_serial = QPushButton("打开 485")
        self.open_serial.setProperty("primary", True)
        self.open_serial.clicked.connect(lambda: self._request("open"))
        self.close_serial = QPushButton("关闭 485")
        self.close_serial.clicked.connect(lambda: self._request("close"))
        self.scan_once = QPushButton("扫描一遍")
        self.scan_once.clicked.connect(lambda: self._request("scan"))
        actions.addWidget(refresh)
        actions.addWidget(self.serial_status)
        actions.addStretch(1)
        actions.addWidget(self.close_serial)
        actions.addWidget(self.open_serial)
        actions.addWidget(self.scan_once)
        connection.body.addLayout(actions)
        row.addWidget(connection)

        identity = Card("电机身份")
        identity_form = FormSection()
        self.station_id = QSpinBox()
        self.station_id.setRange(0, 255)
        self.station_id.valueChanged.connect(self._authority_id_changed)
        self.new_id = QSpinBox()
        self.new_id.setRange(0, 255)
        identity_form.add_field("当前 ID", self.station_id)
        identity_form.add_field("新 ID", self.new_id)
        identity.body.addWidget(identity_form)
        id_actions = QHBoxLayout()
        read = QPushButton("读取")
        read.clicked.connect(lambda: self._request("read_identity"))
        write = QPushButton("写入 EEPROM 并回读")
        write.clicked.connect(lambda: self._request("write_identity"))
        id_actions.addWidget(read)
        id_actions.addWidget(write)
        identity.body.addLayout(id_actions)
        row.addWidget(identity)
        self.layout.addLayout(row)

        motion = Card("485 运动")
        motion_form = QFormLayout()
        self.speed = QDoubleSpinBox()
        self.speed.setRange(-1.0, 1.0)
        self.speed.setDecimals(3)
        self.speed.setSuffix(" rad/s")
        self.relative = QDoubleSpinBox()
        self.relative.setRange(-360.0, 360.0)
        self.relative.setDecimals(2)
        self.relative.setSuffix(" °")
        self.cycle_count = QSpinBox()
        self.cycle_count.setRange(1, 100000)
        self.cycle_count.setValue(100)
        self.cycle_run = QDoubleSpinBox()
        self.cycle_run.setRange(0.1, 3600)
        self.cycle_run.setValue(5.0)
        self.cycle_run.setSuffix(" s")
        self.cycle_wait = QDoubleSpinBox()
        self.cycle_wait.setRange(0, 3600)
        self.cycle_wait.setValue(10.0)
        self.cycle_wait.setSuffix(" s")
        motion_form.addRow("速度", self.speed)
        motion_form.addRow("相对角度", self.relative)
        motion_form.addRow("循环次数", self.cycle_count)
        motion_form.addRow("正/反转时间", self.cycle_run)
        motion_form.addRow("轮次间隔", self.cycle_wait)
        motion.body.addLayout(motion_form)
        authority_actions = QHBoxLayout()
        self.authority_status = QLabel("控制权：未切换")
        self.authority_status.setObjectName("Muted")
        take_control = QPushButton("获取控制权")
        take_control.clicked.connect(lambda: self._request("take_control", motion=True))
        release_control = QPushButton("释放控制权")
        release_control.clicked.connect(lambda: self._request("release_control"))
        authority_actions.addWidget(self.authority_status)
        authority_actions.addStretch(1)
        authority_actions.addWidget(release_control)
        authority_actions.addWidget(take_control)
        motion.body.addLayout(authority_actions)
        motion_actions = QHBoxLayout()
        for label, action in (
            ("使能", "enable"),
            ("释放抱闸", "release_brake"),
            ("速度转动", "set_velocity"),
            ("相对转动", "move_relative"),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, op=action: self._request(op, motion=True))
            motion_actions.addWidget(button)
        stop = QPushButton("停止")
        stop.setProperty("danger", True)
        stop.clicked.connect(lambda: self._request("stop"))
        motion_actions.addStretch(1)
        motion_actions.addWidget(stop)
        motion.body.addLayout(motion_actions)
        cycle_actions = QHBoxLayout()
        self.cycle_stop = QPushButton("停止循环")
        self.cycle_stop.setProperty("danger", True)
        self.cycle_stop.setEnabled(False)
        self.cycle_stop.clicked.connect(lambda: self.state.request("serial485.cycle_cancel"))
        self.cycle_start = QPushButton("开始循环测试")
        self.cycle_start.setProperty("primary", True)
        self.cycle_start.clicked.connect(lambda: self._request("cycle_start", motion=True))
        cycle_actions.addStretch(1)
        cycle_actions.addWidget(self.cycle_stop)
        cycle_actions.addWidget(self.cycle_start)
        motion.body.addLayout(cycle_actions)
        self.layout.addWidget(motion)

        output = Card("485 日志")
        self.console = LogConsole()
        output.body.addWidget(self.console)
        self.layout.addWidget(output)
        state.task_event.connect(self._on_task_event)
        self.refresh_ports()
        self._sync_serial_controls()

    def refresh_ports(self) -> None:
        self.port.clear()
        try:
            from serial.tools import list_ports

            ports = [port.device for port in list_ports.comports()]
        except ImportError:
            ports = []
        self.port.addItems(ports)
        if not ports:
            self.port.addItem("未发现串口", None)
        self._sync_serial_controls()

    def _sync_serial_controls(self) -> None:
        has_port = not (self.port.currentData() is None and self.port.currentText() == "未发现串口")
        self.open_serial.setEnabled(has_port and not self._serial_open and not self._serial_busy)
        self.close_serial.setEnabled(self._serial_open and not self._serial_busy)
        self.scan_once.setEnabled(self._serial_open and not self._serial_busy)

    def _request(self, operation: str, motion: bool = False) -> None:
        if self._serial_busy:
            QMessageBox.information(self, "485 忙碌", "请等待当前打开、关闭或扫描操作完成。")
            return
        if motion and not self._serial_open:
            QMessageBox.warning(self, "485 未打开", "请先点击“打开 485”。")
            return
        if motion and operation != "take_control" and self._authority_comm_id != self.station_id.value():
            QMessageBox.warning(self, "尚未获取控制权", "请先为当前通信 ID 获取控制权。")
            return
        if (
            operation != "close"
            and self.port.currentData() is None
            and self.port.currentText() == "未发现串口"
        ):
            QMessageBox.warning(self, "无可用串口", "请连接 485 转换器后刷新串口。")
            return
        if operation not in {"open", "close"} and not self._serial_open:
            QMessageBox.warning(self, "485 未打开", "请先点击“打开 485”。")
            return
        if operation in {"take_control", "release_control"}:
            self._pending_authority_comm_id = self.station_id.value()
        self.state.request(
            f"serial485.{operation}",
            port=self.port.currentText(),
            baud=int(self.baud.currentText()),
            station_id=self.station_id.value(),
            new_id=self.new_id.value(),
            rad_s=self.speed.value(),
            angle_deg=self.relative.value(),
            cycles=self.cycle_count.value(),
            run_seconds=self.cycle_run.value(),
            round_wait_seconds=self.cycle_wait.value(),
        )
        self.console.appendPlainText(f"[请求] {operation}")

    def _authority_id_changed(self) -> None:
        if self._authority_comm_id != self.station_id.value():
            self.authority_status.setText("控制权：未切换")

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if not action.startswith("serial485."):
            return
        if action in {"serial485.open", "serial485.close"} and event == "started":
            self._serial_busy = True
            self.serial_status.setText("485：正在打开" if action.endswith("open") else "485：正在关闭")
            self._sync_serial_controls()
        if action == "serial485.scan" and event == "started":
            self._serial_busy = True
            self.serial_status.setText("485：扫描中")
            self._sync_serial_controls()
        if action == "serial485.cycle_start" and event == "started":
            self.cycle_start.setEnabled(False)
            self.cycle_stop.setEnabled(True)
        if event == "progress" and isinstance(payload, dict):
            message = str(payload.get("message", ""))
            if action in {"serial485.open", "serial485.close", "serial485.scan"}:
                self.serial_status.setText(f"485：{message}")
            elif message:
                self.console.appendPlainText(message)
        elif event == "succeeded":
            if action == "serial485.open":
                self._serial_open = True
                self.serial_status.setText("485：已打开")
                self.console.appendPlainText("[完成] 485 通信已打开")
            elif action == "serial485.close":
                self._serial_open = False
                self._authority_comm_id = None
                self._pending_authority_comm_id = None
                self.authority_status.setText("控制权：未切换")
                self.serial_status.setText("485：已关闭")
                self.console.appendPlainText("[完成] 485 通信已关闭")
            elif action == "serial485.take_control":
                self._authority_comm_id = self._pending_authority_comm_id
                self.authority_status.setText(
                    "控制权：CAN（1）"
                    if self._authority_comm_id == self.station_id.value()
                    else "控制权：未切换"
                )
            elif action == "serial485.release_control":
                self._authority_comm_id = None
                self.authority_status.setText("控制权：已释放")
            if action in {"serial485.take_control", "serial485.release_control"}:
                self._pending_authority_comm_id = None
            if action == "serial485.scan" and payload is not None:
                station = getattr(payload, "station", None)
                comm_id = getattr(payload, "comm_id", None)
                if station is not None:
                    self.station_id.setValue(int(comm_id))
                    self.console.appendPlainText(
                        f"[发现] 站号 0x{int(station):02X} · 通信 ID 0x{int(comm_id):02X}"
                    )
                else:
                    self.console.appendPlainText("[完成] 未发现设备")
                self.serial_status.setText("485：已打开")
            elif action not in {"serial485.open", "serial485.close"}:
                self.console.appendPlainText(f"[完成] {action.removeprefix('serial485.')}")
        elif event == "failed":
            if action in {"serial485.take_control", "serial485.release_control"}:
                self._pending_authority_comm_id = None
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            self.console.appendPlainText(f"[失败] {error}")
            if action in {"serial485.open", "serial485.close", "serial485.scan"}:
                self.serial_status.setText("485：通信异常" if action == "serial485.open" else "485：已打开")
        elif event == "cancelled":
            if action in {"serial485.take_control", "serial485.release_control"}:
                self._pending_authority_comm_id = None
            self.console.appendPlainText("[停止] 任务已取消并发送停止帧")
            if action == "serial485.open":
                self.serial_status.setText("485：已关闭")
            elif action in {"serial485.close", "serial485.scan"}:
                self.serial_status.setText("485：已打开")
        if action in {"serial485.open", "serial485.close", "serial485.scan"} and event in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            self._serial_busy = False
            self._sync_serial_controls()
        if action == "serial485.cycle_start" and event in {"succeeded", "failed", "cancelled"}:
            self.cycle_start.setEnabled(True)
            self.cycle_stop.setEnabled(False)


class Serial485ControlPage(WorkbenchPage):
    def __init__(
        self,
        state: ApplicationState,
        settings: SettingsStore | None = None,
    ) -> None:
        super().__init__()
        self.state = state
        self.settings = settings or SettingsStore()
        self._serial_open = False
        self._authority_comm_id: int | None = None
        self._connected_comm_id: int | None = None
        self._pending_connect_comm_id: int | None = None
        self._pending_authority = False
        self._status_readback_generation = 0
        self._readback_pending = False
        self._busy_action_counts: dict[str, int] = {}
        self._operation_buttons: list[QPushButton] = []

        self.layout.addWidget(
            PageHeader("电机控制 485", "一次只控制一个通信 ID；串口连接统一使用顶部状态栏。")
        )

        common = Card("控制准备")
        common_row = QHBoxLayout()
        self.comm_id = QComboBox()
        self.comm_id.setMinimumWidth(260)
        self._fill_comm_ids()
        self.comm_id.currentIndexChanged.connect(self._target_changed)
        common_row.addWidget(QLabel("通信 ID"))
        common_row.addWidget(self.comm_id)
        self.connect_motor = QPushButton("连接")
        self.connect_motor.setEnabled(False)
        self.connect_motor.clicked.connect(self._connect_motor)
        common_row.addWidget(self.connect_motor)
        self.connection_status = QLabel("连接：未连接")
        self.connection_status.setObjectName("Muted")
        common_row.addWidget(self.connection_status)
        self.enable_status = QLabel("使能状态：未读取")
        self.enable_status.setObjectName("Muted")
        common_row.addWidget(self.enable_status)
        self.authority_status = QLabel("控制权：未读取")
        self.authority_status.setObjectName("Muted")
        common_row.addWidget(self.authority_status)
        common_row.addStretch(1)

        control_row = QHBoxLayout()
        control_row.addStretch(1)
        for label, operation in (
            ("释放控制权", "release_control"),
            ("获取控制权", "take_control"),
            ("使能", "enable"),
            ("释放抱闸", "release_brake"),
        ):
            button = QPushButton(label)
            if operation == "enable":
                button.setProperty("primary", True)
            button.clicked.connect(lambda _checked=False, selected=operation: self._request_common(selected))
            self._operation_buttons.append(button)
            if operation == "enable":
                self.enable_button = button
            control_row.addWidget(button)
        stop = QPushButton("停止")
        stop.setProperty("danger", True)
        stop.clicked.connect(lambda: self._request("stop", require_authority=False))
        self.stop_button = stop
        control_row.addWidget(stop)
        common.body.addLayout(common_row)
        common.body.addLayout(control_row)
        self.layout.addWidget(common)

        self.modes = QTabWidget()
        self.modes.addTab(self._speed_tab(), "速度控制")
        self.modes.addTab(self._position_tab(), "位置控制")
        self.modes.addTab(self._angle_tab(), "角度控制")
        self.layout.addWidget(self.modes)

        output = Card("485 操作记录")
        self.console = LogConsole()
        self.console.setMinimumHeight(150)
        output.body.addWidget(self.console)
        self.layout.addWidget(output)
        state.changed.connect(self._evt_changed)
        state.task_event.connect(self._on_task_event)
        self._sync_controls()

    def _speed_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 16, 12, 12)
        form = FormSection()
        self.velocity = QDoubleSpinBox()
        self.velocity.setRange(-10.0, 10.0)
        self.velocity.setDecimals(3)
        self.velocity.setSingleStep(0.05)
        self.velocity.setValue(0.5)
        self.velocity.setSuffix(" rad/s")
        self.velocity_duration = QDoubleSpinBox()
        self.velocity_duration.setRange(0.1, 600.0)
        self.velocity_duration.setValue(2.0)
        self.velocity_duration.setSuffix(" s")
        default_accel = float(self.settings.value("serial485/accel_rad_s2", 1.0))
        self.acceleration = QDoubleSpinBox()
        self.acceleration.setRange(0.01, 20.0)
        self.acceleration.setValue(default_accel)
        self.acceleration.setSuffix(" rad/s²")
        self.deceleration = QDoubleSpinBox()
        self.deceleration.setRange(0.01, 20.0)
        self.deceleration.setValue(float(self.settings.value("serial485/decel_rad_s2", default_accel)))
        self.deceleration.setSuffix(" rad/s²")
        form.add_field("目标速度", self.velocity)
        form.add_field("运动时间", self.velocity_duration)
        form.add_field("加速度", self.acceleration)
        form.add_field("减速度", self.deceleration)
        layout.addWidget(form)
        actions = QHBoxLayout()
        actions.addStretch(1)
        execute = QPushButton("按速度转动")
        execute.setProperty("primary", True)
        execute.clicked.connect(self._run_velocity)
        self._operation_buttons.append(execute)
        actions.addWidget(execute)
        layout.addLayout(actions)
        return tab

    def _position_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 16, 12, 12)
        self.current_position = QLabel("尚未读取")
        self.current_position.setObjectName("Mono")
        form = FormSection()
        self.absolute_position = QDoubleSpinBox()
        self.absolute_position.setRange(-804.2477, 804.2477)
        self.absolute_position.setDecimals(4)
        self.absolute_position.setSuffix(" rad")
        form.add_field("当前位置", self.current_position)
        form.add_field("目标位置", self.absolute_position)
        layout.addWidget(form)
        actions = QHBoxLayout()
        read = QPushButton("读取当前位置")
        read.clicked.connect(lambda: self._request("read_position", require_authority=False))
        self._operation_buttons.append(read)
        move = QPushButton("运动到目标位置")
        move.setProperty("primary", True)
        move.clicked.connect(self._move_absolute)
        self._operation_buttons.append(move)
        actions.addWidget(read)
        actions.addStretch(1)
        actions.addWidget(move)
        layout.addLayout(actions)
        return tab

    def _angle_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 16, 12, 12)
        form = FormSection()
        self.relative_angle = QDoubleSpinBox()
        self.relative_angle.setRange(-360.0, 360.0)
        self.relative_angle.setDecimals(2)
        self.relative_angle.setSuffix(" °")
        form.add_field("相对转动角度", self.relative_angle)
        layout.addWidget(form)
        actions = QHBoxLayout()
        actions.addStretch(1)
        move = QPushButton("按角度转动")
        move.setProperty("primary", True)
        move.clicked.connect(self._move_relative)
        self._operation_buttons.append(move)
        actions.addWidget(move)
        layout.addLayout(actions)
        return tab

    def _fill_comm_ids(self) -> None:
        previous = self.comm_id.currentData() if self.comm_id.count() else None
        self.comm_id.clear()
        for node in self.state.evt.nodes:
            self.comm_id.addItem(f"0x{node.dev_id:02X} · {node.label}", node.dev_id)
        index = self.comm_id.findData(previous)
        self.comm_id.setCurrentIndex(index if index >= 0 else 0)

    def _evt_changed(self) -> None:
        self._fill_comm_ids()

    def _target_changed(self) -> None:
        self._status_readback_generation += 1
        self._readback_pending = False
        current = self.comm_id.currentData()
        if self._connected_comm_id != current:
            self._connected_comm_id = None
            self.connection_status.setText("连接：未连接")
            self.connection_status.setStyleSheet("")
            self.enable_status.setText("使能状态：未读取")
        if self._authority_comm_id != current:
            self._authority_comm_id = None
            self.authority_status.setText("控制权：未读取")
        self._sync_controls()

    def _serial_busy(self) -> bool:
        return self._readback_pending or any(self._busy_action_counts.values())

    def _sync_controls(self) -> None:
        busy = self._serial_busy()
        enabled = self._serial_open and not busy
        self.connect_motor.setEnabled(enabled)
        self.comm_id.setEnabled(not busy)
        for button in self._operation_buttons:
            button.setEnabled(enabled)
        connection_busy = any(
            self._busy_action_counts.get(action, 0) for action in ("serial485.open", "serial485.close")
        )
        self.stop_button.setEnabled(
            self._serial_open
            and not connection_busy
            and not self._busy_action_counts.get("serial485.stop", 0)
        )
        self.enable_button.setText(
            "使能中…" if self._busy_action_counts.get("serial485.enable", 0) else "使能"
        )

    def _connect_motor(self) -> None:
        if not self._require_serial():
            return
        if self._serial_busy():
            self.console.appendPlainText("[忽略] 485 正在执行其他操作，请等待完成")
            return
        comm_id = int(self.comm_id.currentData())
        self._pending_connect_comm_id = comm_id
        self.connection_status.setText("连接：读取中")
        self.connection_status.setStyleSheet("color:#B76B12; font-weight:600;")
        self.connect_motor.setEnabled(False)
        self.comm_id.setEnabled(False)
        self.state.request("serial485.connect", station_id=comm_id)
        self.console.appendPlainText(f"[请求] 0x{comm_id:02X} · 连接并读取状态")

    def _schedule_status_readback(self, comm_id: int, delay_ms: int, reason: str) -> None:
        self._status_readback_generation += 1
        generation = self._status_readback_generation
        self._readback_pending = True
        self._sync_controls()
        self.console.appendPlainText(
            f"[自动回读] 0x{comm_id:02X} · {reason}，{delay_ms / 1000:.1f} 秒后读取状态"
        )
        QTimer.singleShot(
            delay_ms,
            lambda: self._run_status_readback(comm_id, generation),
        )

    def _run_status_readback(self, comm_id: int, generation: int) -> None:
        if generation != self._status_readback_generation:
            return
        self._readback_pending = False
        if (
            not self._serial_open
            or self._pending_connect_comm_id is not None
            or self.comm_id.currentData() != comm_id
        ):
            self._sync_controls()
            return
        self._connect_motor()

    def _require_serial(self) -> bool:
        if self._serial_open:
            return True
        QMessageBox.warning(self, "485 未打开", "请先在顶部状态栏打开 485。")
        return False

    def _request_common(self, operation: str) -> None:
        if operation in {"take_control", "release_control"}:
            self._pending_authority = operation == "take_control"
        self._request(operation, require_authority=operation not in {"take_control", "release_control"})

    def _request(self, operation: str, *, require_authority: bool = True, **payload: object) -> None:
        if not self._require_serial():
            return
        if self._serial_busy() and operation != "stop":
            self.console.appendPlainText("[忽略] 485 正在执行其他操作，请等待完成")
            return
        comm_id = int(self.comm_id.currentData())
        if require_authority and self._authority_comm_id != comm_id:
            QMessageBox.warning(self, "尚未获取控制权", "请先为当前通信 ID 获取控制权。")
            return
        self.state.request(f"serial485.{operation}", station_id=comm_id, **payload)
        self.console.appendPlainText(f"[请求] 0x{comm_id:02X} · {operation}")

    def _run_velocity(self) -> None:
        if (
            QMessageBox.warning(
                self,
                "确认速度运动",
                f"电机将以 {self.velocity.value():.3f} rad/s 运动 "
                f"{self.velocity_duration.value():.1f} s，确认周围无人且机构无干涉。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._request(
            "set_velocity",
            rad_s=self.velocity.value(),
            duration_s=self.velocity_duration.value(),
            accel_rad_s2=self.acceleration.value(),
            decel_rad_s2=self.deceleration.value(),
        )

    def _move_absolute(self) -> None:
        if (
            QMessageBox.warning(
                self,
                "确认位置运动",
                f"电机将运动到绝对位置 {self.absolute_position.value():.4f} rad。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._request("move_absolute", position_rad=self.absolute_position.value())

    def _move_relative(self) -> None:
        if (
            QMessageBox.warning(
                self,
                "确认角度运动",
                f"电机将相对转动 {self.relative_angle.value():.2f}°。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._request("move_relative", angle_deg=self.relative_angle.value())

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action.startswith("serial485."):
            if event == "started":
                self._busy_action_counts[action] = self._busy_action_counts.get(action, 0) + 1
            elif event in {"succeeded", "failed", "cancelled"}:
                remaining = self._busy_action_counts.get(action, 0) - 1
                if remaining > 0:
                    self._busy_action_counts[action] = remaining
                else:
                    self._busy_action_counts.pop(action, None)
        if action == "serial485.open" and event == "succeeded":
            self._serial_open = True
        elif action == "serial485.close" and event in {"succeeded", "failed", "cancelled"}:
            self._serial_open = False
            self._status_readback_generation += 1
            self._readback_pending = False
            self._authority_comm_id = None
            self._connected_comm_id = None
            self._pending_connect_comm_id = None
            self.comm_id.setEnabled(True)
            self.connection_status.setText("连接：未连接")
            self.connection_status.setStyleSheet("")
            self.enable_status.setText("使能状态：未读取")
            self.enable_status.setStyleSheet("")
            self.authority_status.setText("控制权：未读取")
        if action in {"serial485.open", "serial485.close"}:
            self.connect_motor.setEnabled(self._serial_open)
        if not action.startswith("serial485."):
            return
        operation = action.removeprefix("serial485.")
        if operation == "enable" and event == "started":
            self.enable_status.setText("使能状态：正在执行 0x0006 → 0x0007 → 0x000F")
            self.enable_status.setStyleSheet("color:#B76B12; font-weight:600;")
        if operation == "connect" and self._pending_connect_comm_id is not None:
            if event == "started":
                self.connect_motor.setEnabled(False)
                self.connection_status.setText("连接：读取中")
            elif event == "succeeded" and isinstance(payload, dict):
                comm_id = int(payload.get("comm_id", self.comm_id.currentData()))
                parameters = payload.get("parameters", [])
                values = {
                    int(item.get("address", 0)): int(item.get("value", 0))
                    for item in parameters
                    if isinstance(item, dict) and item.get("value") is not None
                }
                self._connected_comm_id = comm_id
                self.connection_status.setText(f"连接：已连接 0x{comm_id:02X}")
                self.connection_status.setStyleSheet("color:#16845B; font-weight:600;")
                statusword = values.get(0x604100)
                self.enable_status.setText(
                    f"使能状态：{serial_servo_state(statusword)}（0x{statusword:04X}）"
                    if statusword is not None
                    else "使能状态：未返回"
                )
                self.enable_status.setStyleSheet(
                    "color:#16845B; font-weight:600;"
                    if statusword is not None and serial_servo_state(statusword) == "使能"
                    else ""
                )
                authority = values.get(0x200201)
                authority_name = {0: "485", 1: "CAN"}.get(authority, "未知")
                self.authority_status.setText(
                    f"控制权：{authority_name}（{authority}）" if authority is not None else "控制权：未返回"
                )
                self._authority_comm_id = comm_id if authority == 0 else None
                self.console.appendPlainText(
                    f"[连接] 0x{comm_id:02X} · {self.enable_status.text()} · {self.authority_status.text()}"
                )
                self._pending_connect_comm_id = None
                self.comm_id.setEnabled(True)
                self.connect_motor.setEnabled(self._serial_open)
            elif event in {"failed", "cancelled"}:
                self._connected_comm_id = None
                self._authority_comm_id = None
                self.connection_status.setText("连接：失败" if event == "failed" else "连接：已停止")
                self.connection_status.setStyleSheet("color:#C43F45; font-weight:600;")
                self.enable_status.setText("使能状态：未读取")
                self.authority_status.setText("控制权：未读取")
                self._pending_connect_comm_id = None
                self.comm_id.setEnabled(True)
                self.connect_motor.setEnabled(self._serial_open)
        if operation in {"take_control", "release_control"}:
            if event == "succeeded":
                comm_id = (
                    int(payload.get("comm_id", self.comm_id.currentData()))
                    if isinstance(payload, dict)
                    else int(self.comm_id.currentData())
                )
                self._authority_comm_id = comm_id if self._pending_authority else None
                self.authority_status.setText(
                    "控制权：485（0）" if self._authority_comm_id is not None else "控制权：CAN（1）"
                )
                self.connection_status.setText("连接：等待电机重启并自动读取")
                self.enable_status.setText("使能状态：等待自动读取")
                self._connected_comm_id = None
                self._pending_authority = False
                self._schedule_status_readback(comm_id, 2200, "控制权切换完成")
            elif event in {"failed", "cancelled"}:
                self._pending_authority = False
        if operation == "enable" and event == "succeeded" and isinstance(payload, dict):
            statusword = payload.get("statusword")
            if statusword is not None:
                statusword = int(statusword)
                self.enable_status.setText(
                    f"使能状态：{serial_servo_state(statusword)}（0x{statusword:04X}）"
                )
                self.enable_status.setStyleSheet("color:#16845B; font-weight:600;")
            else:
                comm_id = int(payload.get("comm_id", self.comm_id.currentData()))
                self.enable_status.setText("使能状态：正在自动确认")
                self._schedule_status_readback(comm_id, 350, "enable 完成")
        if operation in {"release_brake", "stop"} and event == "succeeded":
            comm_id = (
                int(payload.get("comm_id", self.comm_id.currentData()))
                if isinstance(payload, dict)
                else int(self.comm_id.currentData())
            )
            self.enable_status.setText("使能状态：正在自动确认")
            self._schedule_status_readback(comm_id, 350, f"{operation} 完成")
        if operation == "enable" and event in {"failed", "cancelled"}:
            self.enable_status.setText("使能状态：使能失败" if event == "failed" else "使能状态：已停止")
            self.enable_status.setStyleSheet("color:#C43F45; font-weight:600;")
        if operation == "read_position" and event == "succeeded" and isinstance(payload, dict):
            position = payload.get("position_rad", payload.get("position", "—"))
            self.current_position.setText(f"{position} rad")
        if event == "progress" and isinstance(payload, dict):
            message = str(payload.get("message", ""))
            if message:
                self.console.appendPlainText(message)
        elif event == "failed":
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            self.console.appendPlainText(f"[失败] {operation} · {error}")
        elif event == "cancelled":
            self.console.appendPlainText(f"[停止] {operation} 已取消并执行安全停止")
        elif event == "succeeded" and operation not in {"open", "close"}:
            self.console.appendPlainText(f"[完成] {operation}")
        self._sync_controls()


class CanControlPage(WorkbenchPage):
    """Unified single/group motor control for Orin and the local CAN adapter."""

    def __init__(
        self,
        state: ApplicationState,
        settings: SettingsStore | None = None,
    ) -> None:
        super().__init__()
        self.state = state
        self.settings = settings or SettingsStore()
        self._authority_target: tuple[str, object] | None = None
        self._pending_authority = False
        self._evt_variant = state.evt.variant
        self._connect_can_after_disconnect = False
        self._can_box_connection_busy = False
        self._can_box_pending_actions: set[str] = set()
        self._can_box_safely_stopped = False
        self._can_box_feedback = "CAN 盒待机"
        self._target_probe_pending = False
        self._target_probe_snapshot: tuple[str, tuple[str, object]] | None = None
        self._probe_refresh_requested = False
        self._last_probe_connected = False
        self._last_probe_status: int | None = None
        self._was_connected = False
        self._operation_buttons: dict[str, QPushButton] = {}
        self._operation_labels: dict[str, str] = {}

        self.layout.addWidget(PageHeader("电机控制 CAN", "选择 Orin 或 CAN 盒，可控制单电机或 D7 固定分组。"))
        common = Card("控制准备")
        self.control_card = common
        connection_grid = QGridLayout()
        connection_grid.setHorizontalSpacing(10)
        connection_grid.setVerticalSpacing(10)
        connection_grid.setColumnStretch(1, 1)
        connection_grid.setColumnStretch(2, 1)
        self.source = QComboBox()
        self.source.setMaximumWidth(160)
        self.source.addItem("Orin", ConnectionMode.ORIN_REMOTE.value)
        self.source.addItem("CAN 盒", ConnectionMode.PC_DIRECT.value)
        self.source.currentIndexChanged.connect(self._source_changed)
        self.can_channel_label = QLabel("物理通道")
        self.can_channel = QComboBox()
        self.can_channel.setMaximumWidth(100)
        self.can_channel.addItem("CH0", 0)
        self.can_channel.addItem("CH1", 1)
        saved_channel = int(self.settings.value("zlg/channel", 0))
        self.can_channel.setCurrentIndex(max(0, self.can_channel.findData(saved_channel)))
        self.can_channel.currentIndexChanged.connect(self._can_channel_changed)
        self.target = QComboBox()
        self.target.setMinimumWidth(0)
        self.target.setMinimumContentsLength(12)
        self.target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.target.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        add_motor_targets(self.target, state)
        self.target.currentIndexChanged.connect(self._target_changed)
        self.source_status = QLabel("未连接")
        self.source_status.setObjectName("Muted")
        self.probe_target = QPushButton("检测目标")
        self.probe_target.clicked.connect(self._probe_target)
        self.connect_can = QPushButton("打开 CAN 盒")
        self.connect_can.clicked.connect(self._toggle_can_box)
        connection_grid.addWidget(QLabel("通信来源"), 0, 0)
        connection_grid.addWidget(self.source, 0, 1, 1, 2)
        connection_grid.addWidget(self.connect_can, 0, 3)
        connection_grid.addWidget(self.can_channel_label, 1, 0)
        connection_grid.addWidget(self.can_channel, 1, 1)
        connection_grid.addWidget(self.source_status, 1, 2, 1, 2)
        connection_grid.addWidget(QLabel("控制目标"), 2, 0)
        connection_grid.addWidget(self.target, 2, 1, 1, 2)
        connection_grid.addWidget(self.probe_target, 2, 3)
        common.body.addLayout(connection_grid)

        self.target_status = QLabel("目标状态：未检测")
        self.target_status.setObjectName("Muted")
        self.target_status.setWordWrap(True)
        common.body.addWidget(self.target_status)

        detail_row = QHBoxLayout()
        self.authority_status = QLabel("控制权未获取")
        self.authority_status.setObjectName("Muted")
        detail_row.addWidget(self.authority_status)
        self.control_status = QLabel("CAN 盒待机")
        self.control_status.setObjectName("Muted")
        self.control_status.setWordWrap(True)
        detail_row.addWidget(self.control_status, 1)
        common.body.addLayout(detail_row)

        action_grid = QGridLayout()
        action_grid.setHorizontalSpacing(8)
        action_grid.setVerticalSpacing(8)
        for column in range(3):
            action_grid.setColumnStretch(column, 1)
        self.unlock_button = QPushButton("解除安全锁")
        self.unlock_button.clicked.connect(self._unlock)
        action_buttons = [self.unlock_button]
        for label, operation in (
            ("释放控制权", "release_control"),
            ("获取控制权", "take_control"),
            ("清除故障", "clear_errors"),
            ("失能", "disable"),
            ("使能", "enable"),
        ):
            button = QPushButton(label)
            if operation == "enable":
                button.setProperty("primary", True)
            if operation == "disable":
                button.setProperty("danger", True)
            button.clicked.connect(lambda _checked=False, selected=operation: self._request_common(selected))
            self._operation_buttons[operation] = button
            self._operation_labels[operation] = label
            action_buttons.append(button)
        self.stop_button = QPushButton("紧急停止")
        self.stop_button.setProperty("danger", True)
        self.stop_button.clicked.connect(lambda: self._request("emergency_stop", require_authority=False))
        self._operation_buttons["emergency_stop"] = self.stop_button
        self._operation_labels["emergency_stop"] = "紧急停止"
        action_buttons.append(self.stop_button)
        for index, button in enumerate(action_buttons):
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if button is self.stop_button:
                action_grid.addWidget(button, 2, 0, 1, 3)
            else:
                action_grid.addWidget(button, index // 3, index % 3)
        common.body.addLayout(action_grid)
        self.layout.addWidget(common)

        self.modes = QTabWidget()
        self.modes.addTab(self._speed_tab(), "速度控制")
        self.modes.addTab(self._position_tab(), "位置控制")
        self.modes.addTab(self._angle_tab(), "角度控制")
        self.layout.addWidget(self.modes)

        output = Card("CAN 操作记录")
        self.console = LogConsole()
        self.console.setMinimumHeight(150)
        output.body.addWidget(self.console)
        self.layout.addWidget(output)
        state.changed.connect(self._state_changed)
        state.task_event.connect(self._on_task_event)
        self._refresh_source()

    def _speed_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 16, 12, 12)
        form = FormSection()
        self.velocity = QDoubleSpinBox()
        self.velocity.setRange(-0.5, 0.5)
        self.velocity.setDecimals(3)
        self.velocity.setSuffix(" rad/s")
        self.velocity_duration = QDoubleSpinBox()
        self.velocity_duration.setRange(0.1, 600.0)
        self.velocity_duration.setValue(2.0)
        self.velocity_duration.setSuffix(" s")
        self.acceleration = QDoubleSpinBox()
        self.acceleration.setRange(0.01, 20.0)
        self.acceleration.setValue(float(self.settings.value("can/accel_rad_s2", 1.0)))
        self.acceleration.setSuffix(" rad/s²")
        self.deceleration = QDoubleSpinBox()
        self.deceleration.setRange(0.01, 20.0)
        self.deceleration.setValue(float(self.settings.value("can/decel_rad_s2", 1.0)))
        self.deceleration.setSuffix(" rad/s²")
        form.add_field("目标速度", self.velocity)
        form.add_field("运动时间", self.velocity_duration)
        form.add_field("加速度", self.acceleration)
        form.add_field("减速度", self.deceleration)
        layout.addWidget(form)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.velocity_button = QPushButton("按速度转动")
        self.velocity_button.setProperty("primary", True)
        self.velocity_button.clicked.connect(self._run_velocity)
        self._operation_buttons["set_velocity"] = self.velocity_button
        self._operation_labels["set_velocity"] = "按速度转动"
        actions.addWidget(self.velocity_button)
        layout.addLayout(actions)
        return tab

    def _position_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 16, 12, 12)
        form = FormSection()
        self.current_position = QLabel("尚未读取")
        self.current_position.setObjectName("Mono")
        self.absolute_position = QDoubleSpinBox()
        self.absolute_position.setRange(-5.0, 5.0)
        self.absolute_position.setDecimals(4)
        self.absolute_position.setSuffix(" rad")
        form.add_field("当前位置", self.current_position)
        form.add_field("目标位置", self.absolute_position)
        layout.addWidget(form)
        actions = QHBoxLayout()
        self.read_position_button = QPushButton("读取当前位置")
        self.read_position_button.clicked.connect(
            lambda: self._request("read_position", require_authority=False)
        )
        self.position_button = QPushButton("运动到目标位置")
        self.position_button.setProperty("primary", True)
        self.position_button.clicked.connect(self._move_absolute)
        self._operation_buttons["read_position"] = self.read_position_button
        self._operation_labels["read_position"] = "读取当前位置"
        self._operation_buttons["move_absolute"] = self.position_button
        self._operation_labels["move_absolute"] = "运动到目标位置"
        actions.addWidget(self.read_position_button)
        actions.addStretch(1)
        actions.addWidget(self.position_button)
        layout.addLayout(actions)
        return tab

    def _angle_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 16, 12, 12)
        form = FormSection()
        self.relative_angle = QDoubleSpinBox()
        self.relative_angle.setRange(-360.0, 360.0)
        self.relative_angle.setDecimals(2)
        self.relative_angle.setSuffix(" °")
        form.add_field("相对转动角度", self.relative_angle)
        layout.addWidget(form)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.angle_button = QPushButton("按角度转动")
        self.angle_button.setProperty("primary", True)
        self.angle_button.clicked.connect(self._move_relative)
        self._operation_buttons["move_relative"] = self.angle_button
        self._operation_labels["move_relative"] = "按角度转动"
        actions.addWidget(self.angle_button)
        layout.addLayout(actions)
        return tab

    def _selected_mode(self) -> ConnectionMode:
        return ConnectionMode(str(self.source.currentData()))

    def _source_changed(self) -> None:
        self._authority_target = None
        self.authority_status.setText("控制权未获取")
        self._reset_probe_status()
        self._refresh_source()
        if self._is_selected_source_connected():
            if self._target_probe_pending:
                self._probe_refresh_requested = True
            else:
                QTimer.singleShot(0, self._probe_target)

    def _can_channel_changed(self) -> None:
        self._reset_probe_status()
        self._refresh_source()

    def _selected_can_channel(self) -> int:
        value = self.can_channel.currentData()
        return int(value) if value is not None else 0

    def _state_changed(self) -> None:
        if self._evt_variant != self.state.evt.variant:
            selected = self.target.currentData()
            add_motor_targets(self.target, self.state)
            index = self.target.findData(selected)
            self.target.setCurrentIndex(index if index >= 0 else 0)
            self._evt_variant = self.state.evt.variant
        if self.state.link_state is not LinkState.CONNECTED:
            self._authority_target = None
        self._refresh_source()
        connected = self._is_selected_source_connected()
        if connected and not self._was_connected:
            QTimer.singleShot(0, self._probe_target)
        self._was_connected = connected

    def _refresh_source(self) -> None:
        mode = self._selected_mode()
        connected = self.state.connection_mode is mode and self.state.link_state is LinkState.CONNECTED
        connecting = self.state.connection_mode is mode and self.state.link_state is LinkState.CONNECTING
        source_name = (
            "Orin"
            if mode is ConnectionMode.ORIN_REMOTE
            else f"CAN 盒 CH{self._selected_can_channel()}"
        )
        self.source_status.setText(
            f"{source_name} 连接中…"
            if connecting
            else f"{source_name} 已连接"
            if connected
            else f"{source_name} 未连接"
        )
        self.probe_target.setEnabled(connected and not self._target_probe_pending)
        self.can_channel_label.setVisible(mode is ConnectionMode.PC_DIRECT)
        self.can_channel.setVisible(mode is ConnectionMode.PC_DIRECT)
        self.connect_can.setVisible(mode is ConnectionMode.PC_DIRECT)
        self.connect_can.setText(
            f"关闭 CH{self._selected_can_channel()}"
            if connected
            else f"打开 CH{self._selected_can_channel()}"
        )
        self._update_target_presentation()
        self._sync_can_box_controls()

    def _is_group_target(self) -> bool:
        target = self.target.currentData()
        return isinstance(target, tuple) and len(target) == 2 and target[0] == "group"

    def _reset_probe_status(self) -> None:
        self._last_probe_connected = False
        self._last_probe_status = None
        self.target_status.setText(
            "广播应答：未扫描"
            if self._selected_mode() is ConnectionMode.PC_DIRECT and self._is_group_target()
            else "节点状态：未检测"
            if not self._is_group_target()
            else "目标状态：未检测"
        )
        self.target_status.setStyleSheet("")

    def _update_target_presentation(self) -> None:
        is_local = self._selected_mode() is ConnectionMode.PC_DIRECT
        is_group = self._is_group_target()
        self.authority_status.setVisible(not is_local)
        self.control_status.setVisible(is_local and not is_group)

    def _set_single_target_status(self, *, connected: bool, status: int | None) -> None:
        self._last_probe_connected = connected
        self._last_probe_status = status
        if status == 0x42:
            enable_text = "使能（0x42）"
        elif status in {0x00, 0x40}:
            enable_text = f"失能（0x{status:02X}）"
        elif status is None:
            enable_text = "未知"
        else:
            enable_text = f"未知（0x{status:02X}）"
        authority = "CAN（1）" if self._authority_target == self.target.currentData() else "未获取"
        self.target_status.setText(
            f"节点状态：{'已连接' if connected else '未连接'} · "
            f"使能：{enable_text} · 控制权：{authority}"
        )
        self.target_status.setStyleSheet(
            "color:#16845B; font-weight:700;" if connected else "color:#C43F45; font-weight:700;"
        )

    def _is_selected_source_connected(self) -> bool:
        mode = self._selected_mode()
        return self.state.connection_mode is mode and self.state.link_state is LinkState.CONNECTED

    def _probe_target(self) -> None:
        if self._target_probe_pending or not self._is_selected_source_connected():
            return
        target = self.target.currentData()
        if not isinstance(target, tuple) or len(target) != 2:
            return
        self._target_probe_pending = True
        self._target_probe_snapshot = (self._selected_mode().value, target)
        self._probe_refresh_requested = False
        self.target_status.setText(
            "广播应答：扫描中…"
            if self._selected_mode() is ConnectionMode.PC_DIRECT and self._is_group_target()
            else "节点状态：检测中…"
            if not self._is_group_target()
            else "目标状态：检测中…"
        )
        self.target_status.setStyleSheet("color:#B76B12; font-weight:600;")
        self.probe_target.setEnabled(False)
        self.state.request(
            "motor.probe",
            source=self._selected_mode().value,
            target=self._target_payload(),
        )
        self.console.appendPlainText(
            f"[检测] {self.target.currentText()} · 正在读取真实电机回包"
        )

    def _sync_can_box_controls(self) -> None:
        for operation, button in self._operation_buttons.items():
            button.setText(self._operation_labels[operation])

        if self._selected_mode() is not ConnectionMode.PC_DIRECT:
            self.source.setEnabled(True)
            self.target.setEnabled(True)
            self.unlock_button.setEnabled(True)
            self.modes.setEnabled(True)
            for button in self._operation_buttons.values():
                button.setEnabled(True)
            self._operation_buttons["release_control"].setToolTip("")
            return

        connected = (
            self.state.connection_mode is ConnectionMode.PC_DIRECT
            and self.state.link_state is LinkState.CONNECTED
        )
        connection_busy = self._can_box_connection_busy or (
            self.state.connection_mode is ConnectionMode.PC_DIRECT
            and self.state.link_state is LinkState.CONNECTING
        )
        busy = bool(self._can_box_pending_actions)
        read_only_operations = {
            "take_control",
            "release_control",
            "clear_errors",
            "enable",
            "set_velocity",
            "move_absolute",
            "move_relative",
        }
        self.source.setEnabled(not connection_busy and not busy)
        self.target.setEnabled(not connection_busy and not busy)
        self.can_channel.setEnabled(not connected and not connection_busy and not busy)
        self.connect_can.setEnabled(not connection_busy and not busy)
        self.unlock_button.setEnabled(False)
        self.unlock_button.setToolTip("CAN 盒直控已暂停，避免中断 EtherCAT")
        self.modes.setEnabled(False)

        for operation, button in self._operation_buttons.items():
            button.setEnabled(connected and not busy and operation not in read_only_operations)
            if operation in read_only_operations:
                button.setToolTip("CAN 盒直控已暂停：此前切换控制源会中断 EtherCAT")
            else:
                button.setToolTip("")
            if f"motor.{operation}" in self._can_box_pending_actions:
                button.setText(self._can_box_busy_label(operation))

        # Local CAN has no verified release-control command. Emergency stop stays
        # available while another local operation is in flight.
        self._operation_buttons["release_control"].setEnabled(False)
        self._operation_buttons["release_control"].setToolTip(
            "CAN 盒直控已暂停：此前切换控制源会中断 EtherCAT"
        )
        self._operation_buttons["release_control"].setToolTip("CAN 盒暂不支持已验证的释放控制权协议")
        emergency_pending = "motor.emergency_stop" in self._can_box_pending_actions
        self.stop_button.setEnabled(connected and not emergency_pending)

        if connection_busy:
            status = "CAN 盒连接处理中…"
        elif emergency_pending:
            status = "正在安全停止…"
        elif "失败" in self._can_box_feedback or "安全保护" in self._can_box_feedback:
            status = self._can_box_feedback
        elif self._can_box_safely_stopped and busy:
            status = "已安全停止，等待运动任务结束"
        elif busy or connected:
            status = self._can_box_feedback
        else:
            status = "CAN 盒未连接"
        self.control_status.setText(status)
        self.control_status.setStyleSheet(
            "color:#C43F45; font-weight:600;"
            if "失败" in status or "安全保护" in status
            else "color:#16845B; font-weight:600;"
            if "已安全停止" in status or "已完成" in status
            else "color:#B76B12; font-weight:600;"
            if "正在" in status or "处理中" in status
            else ""
        )

    @staticmethod
    def _can_box_busy_label(operation: str) -> str:
        return {
            "take_control": "获取中…",
            "release_control": "释放中…",
            "clear_errors": "清除中…",
            "disable": "失能中…",
            "enable": "使能中…",
            "read_position": "读取中…",
            "set_velocity": "运动中…",
            "move_absolute": "运动中…",
            "move_relative": "运动中…",
            "emergency_stop": "停止中…",
        }.get(operation, "执行中…")

    @staticmethod
    def _can_box_status_text(operation: str) -> str:
        return {
            "take_control": "正在获取控制权…",
            "clear_errors": "正在清除故障…",
            "disable": "正在安全失能…",
            "enable": "正在安全使能…",
            "read_position": "正在读取位置…",
            "set_velocity": "速度运动中…",
            "move_absolute": "位置运动指令发送中…",
            "move_relative": "角度运动指令发送中…",
            "emergency_stop": "正在安全停止…",
        }.get(operation, "CAN 盒操作执行中…")

    def _toggle_can_box(self) -> None:
        if self._can_box_connection_busy or self._can_box_pending_actions:
            return
        connected = (
            self.state.connection_mode is ConnectionMode.PC_DIRECT
            and self.state.link_state is LinkState.CONNECTED
        )
        if connected:
            self._can_box_connection_busy = True
            self._can_box_feedback = "正在关闭 CAN 盒…"
            self._sync_can_box_controls()
            self.state.request("connection.disconnect")
            return
        if self.state.link_state is LinkState.CONNECTED:
            self._connect_can_after_disconnect = True
            self._can_box_connection_busy = True
            self._can_box_feedback = "正在切换到 CAN 盒…"
            self._sync_can_box_controls()
            self.state.request("connection.disconnect")
            return
        self._begin_can_box_connect()

    def _begin_can_box_connect(self) -> None:
        self._can_box_connection_busy = True
        self._can_box_feedback = "正在打开 CAN 盒…"
        self.state.set_connection_mode(ConnectionMode.PC_DIRECT)
        self.state.set_link_state(LinkState.CONNECTING)
        self._sync_can_box_controls()
        self.state.request(
            "connection.connect",
            mode=ConnectionMode.PC_DIRECT.value,
            evt=self.state.evt.variant,
            channel=self._selected_can_channel(),
        )

    def _require_connection(self) -> bool:
        mode = self._selected_mode()
        if self.state.connection_mode is mode and self.state.link_state is LinkState.CONNECTED:
            return True
        message = "请先在顶部状态栏连接 Orin。" if mode is ConnectionMode.ORIN_REMOTE else "请先打开 CAN 盒。"
        QMessageBox.warning(self, "通信未连接", message)
        return False

    def _unlock(self) -> None:
        if not self._require_connection():
            return
        if (
            QMessageBox.warning(
                self,
                "解除安全锁",
                "确认电机已架空或机构已可靠支撑，运动范围内无人。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.state.unlock()

    def _target_payload(self) -> dict[str, object]:
        kind, value = self.target.currentData()
        return {str(kind): value}

    def _target_changed(self) -> None:
        if self._authority_target != self.target.currentData():
            self.authority_status.setText("控制权未获取")
        self._reset_probe_status()
        self._update_target_presentation()
        if not self._is_selected_source_connected():
            return
        if self._target_probe_pending:
            self._probe_refresh_requested = True
        else:
            QTimer.singleShot(0, self._probe_target)

    def _request_common(self, operation: str) -> None:
        if operation in {"take_control", "release_control"}:
            self._pending_authority = operation == "take_control"
        accepted = self._request(
            operation,
            require_authority=operation not in {"take_control", "release_control"},
        )
        if not accepted and operation in {"take_control", "release_control"}:
            self._pending_authority = False

    def _request(self, operation: str, *, require_authority: bool = True, **payload: object) -> bool:
        if not self._require_connection():
            return False
        if require_authority and self._authority_target != self.target.currentData():
            QMessageBox.warning(self, "尚未获取控制权", "请先为当前目标获取控制权。")
            return False
        action = f"motor.{operation}"
        if self._selected_mode() is ConnectionMode.PC_DIRECT:
            if operation == "release_control":
                QMessageBox.information(
                    self,
                    "CAN 盒暂不支持",
                    "当前 CAN 盒协议没有经过验证的释放控制权指令。",
                )
                return False
            if action in self._can_box_pending_actions:
                return False
            if self._can_box_pending_actions and operation != "emergency_stop":
                return False
            if operation != "emergency_stop":
                self._can_box_safely_stopped = False
            self._can_box_pending_actions.add(action)
            self._can_box_feedback = self._can_box_status_text(operation)
            self._sync_can_box_controls()
        self.state.request(
            action,
            source=self._selected_mode().value,
            target=self._target_payload(),
            **payload,
        )
        self.console.appendPlainText(f"[请求] {self.target.currentText()} · {operation}")
        return True

    def _run_velocity(self) -> None:
        if (
            QMessageBox.warning(
                self,
                "确认速度运动",
                f"目标将以 {self.velocity.value():.3f} rad/s 运动 {self.velocity_duration.value():.1f} s。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._request(
            "set_velocity",
            rad_s=self.velocity.value(),
            duration_s=self.velocity_duration.value(),
            accel_rad_s2=self.acceleration.value(),
            decel_rad_s2=self.deceleration.value(),
        )

    def _move_absolute(self) -> None:
        if (
            QMessageBox.warning(
                self,
                "确认位置运动",
                f"目标将运动到绝对位置 {self.absolute_position.value():.4f} rad。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self._request("move_absolute", position_rad=self.absolute_position.value())

    def _move_relative(self) -> None:
        if (
            QMessageBox.warning(
                self,
                "确认角度运动",
                f"目标将相对转动 {self.relative_angle.value():.2f}°。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self._request("move_relative", angle_deg=self.relative_angle.value())

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action == "motor.probe":
            if event == "progress" and isinstance(payload, dict):
                current_snapshot = (self._selected_mode().value, self.target.currentData())
                if self._target_probe_snapshot != current_snapshot:
                    return
                message = str(payload.get("message", "")).strip()
                if message:
                    self.target_status.setText(
                        f"广播应答：{message}"
                        if self._selected_mode() is ConnectionMode.PC_DIRECT
                        and self._is_group_target()
                        else f"节点状态：{message}"
                        if not self._is_group_target()
                        else f"目标状态：{message}"
                    )
                return
            if event not in {"succeeded", "failed", "cancelled"}:
                return

            requested_snapshot = self._target_probe_snapshot
            current_target = self.target.currentData()
            current_snapshot = (self._selected_mode().value, current_target)
            result_snapshot = requested_snapshot
            if event == "succeeded" and isinstance(payload, dict):
                result_target = payload.get("target")
                if isinstance(result_target, dict) and len(result_target) == 1:
                    result_snapshot = (
                        str(payload.get("source", "")),
                        next(iter(result_target.items())),
                    )

            self._target_probe_pending = False
            self._target_probe_snapshot = None
            stale = result_snapshot != current_snapshot
            if stale:
                self._reset_probe_status()
                self.console.appendPlainText("[检测] 目标已切换，已忽略上一目标的检测结果")
            elif event == "succeeded" and isinstance(payload, dict):
                total = int(payload.get("total", 0))
                online = [int(value) for value in payload.get("online", [])]
                missing = [int(value) for value in payload.get("missing", [])]
                response_ids = sorted({int(value) for value in payload.get("response_ids", [])})
                is_local_group = (
                    self._selected_mode() is ConnectionMode.PC_DIRECT
                    and self._is_group_target()
                )
                if is_local_group:
                    ids_text = "、".join(f"0x{device_id:02X}" for device_id in response_ids)
                    self.target_status.setText(
                        f"广播应答：{ids_text}" if ids_text else "广播应答：未扫描到节点"
                    )
                    self.target_status.setStyleSheet(
                        "color:#16845B; font-weight:700;"
                        if response_ids
                        else "color:#C43F45; font-weight:700;"
                    )
                    self.console.appendPlainText(
                        f"[广播扫描] {self.target.currentText()} · "
                        f"{ids_text if ids_text else '未扫描到节点'}"
                    )
                elif not self._is_group_target():
                    logic_id = int(current_target[1])
                    node = next(
                        (item for item in self.state.evt.nodes if item.logic_id == logic_id),
                        None,
                    )
                    connected = logic_id in online or bool(
                        node is not None and node.dev_id in response_ids
                    )
                    status_values = payload.get("statuses", {})
                    raw_status = (
                        status_values.get(str(logic_id))
                        if isinstance(status_values, dict)
                        else None
                    )
                    status = int(raw_status) if raw_status is not None else None
                    self._set_single_target_status(connected=connected, status=status)
                    self.console.appendPlainText(
                        f"[检测] {self.target.currentText()} · "
                        f"{'已连接' if connected else '未连接'}"
                    )
                elif total > 0 and not missing and len(online) == total:
                    self.target_status.setText(f"目标状态：全部在线 {len(online)}/{total}")
                    self.target_status.setStyleSheet("color:#16845B; font-weight:700;")
                    self.console.appendPlainText(
                        f"[检测] {self.target.currentText()} · 全部在线 {len(online)}/{total}"
                    )
                else:
                    missing_text = "、".join(str(logic_id) for logic_id in missing) or "未知"
                    self.target_status.setText(
                        f"目标状态：在线 {len(online)}/{total} · 缺失逻辑 ID {missing_text}"
                    )
                    self.target_status.setStyleSheet("color:#C43F45; font-weight:700;")
                    self.console.appendPlainText(
                        f"[检测] {self.target.currentText()} · 在线 {len(online)}/{total}"
                        f" · 缺失逻辑 ID {missing_text}"
                    )
            elif event == "failed":
                error = payload.get("error", "检测失败") if isinstance(payload, dict) else "检测失败"
                prefix = (
                    "广播应答"
                    if self._selected_mode() is ConnectionMode.PC_DIRECT and self._is_group_target()
                    else "节点状态"
                    if not self._is_group_target()
                    else "目标状态"
                )
                self.target_status.setText(f"{prefix}：检测失败 · {error}")
                self.target_status.setStyleSheet("color:#C43F45; font-weight:700;")
                self.console.appendPlainText(f"[检测失败] {error}")
            else:
                prefix = "广播应答" if self._is_group_target() else "节点状态"
                self.target_status.setText(f"{prefix}：检测已停止")
                self.target_status.setStyleSheet("color:#C43F45; font-weight:700;")

            refresh = self._probe_refresh_requested or stale
            self._probe_refresh_requested = False
            self._refresh_source()
            if refresh and self._is_selected_source_connected():
                QTimer.singleShot(0, self._probe_target)
            return

        if action == "motor.local_can_safety" and event == "failed":
            error = payload.get("error", "CAN 电机安全保护已触发") if isinstance(payload, dict) else str(payload)
            self._can_box_safely_stopped = True
            self._can_box_feedback = f"安全保护：{error}"
            self._sync_can_box_controls()
        if action == "connection.disconnect" and event == "succeeded" and self._connect_can_after_disconnect:
            self._connect_can_after_disconnect = False
            self._begin_can_box_connect()
            return
        if action == "connection.disconnect" and event in {"failed", "cancelled"}:
            self._connect_can_after_disconnect = False
        if action in {"connection.connect", "connection.disconnect"} and (
            self._can_box_connection_busy or self.state.connection_mode is ConnectionMode.PC_DIRECT
        ):
            if event == "started":
                self._can_box_connection_busy = True
            elif event in {"succeeded", "failed", "cancelled"}:
                self._can_box_connection_busy = False
                if event == "succeeded":
                    self._can_box_feedback = (
                        "CAN 盒已就绪" if action == "connection.connect" else "CAN 盒已关闭"
                    )
                elif event == "failed":
                    error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
                    self._can_box_feedback = f"CAN 盒连接失败：{error}"
                else:
                    self._can_box_feedback = "CAN 盒连接操作已取消"
            self._refresh_source()

        if action in self._can_box_pending_actions:
            operation = action.removeprefix("motor.")
            if event == "started":
                self._can_box_feedback = self._can_box_status_text(operation)
            elif event == "progress" and isinstance(payload, dict):
                message = str(payload.get("message", "")).strip()
                if message:
                    self._can_box_feedback = message
                    self.console.appendPlainText(f"[进度] {operation} · {message}")
            elif event in {"succeeded", "failed", "cancelled"}:
                self._can_box_pending_actions.discard(action)
                if event == "succeeded" and operation == "emergency_stop":
                    self._can_box_safely_stopped = True
                    self._can_box_feedback = "已安全停止"
                elif (
                    event == "succeeded"
                    and operation == "set_velocity"
                    and not self._can_box_safely_stopped
                ):
                    self._can_box_safely_stopped = True
                    self._can_box_feedback = "速度运动完成，已校验运行反馈并安全失能"
                elif event == "succeeded" and not self._can_box_safely_stopped:
                    self._can_box_feedback = f"已完成：{self._operation_labels[operation]}"
                elif event == "failed":
                    error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
                    self._can_box_feedback = f"操作失败：{error}"
                elif event == "cancelled" and not self._can_box_safely_stopped:
                    self._can_box_feedback = "操作已取消"
            self._sync_can_box_controls()
        if action in {"motor.take_control", "motor.release_control"}:
            if event == "succeeded":
                self._authority_target = self.target.currentData() if self._pending_authority else None
                self.authority_status.setText(
                    "控制权：CAN（1）" if self._authority_target is not None else "控制权：已释放"
                )
                if not self._is_group_target():
                    self._set_single_target_status(
                        connected=self._last_probe_connected,
                        status=self._last_probe_status,
                    )
            elif event in {"failed", "cancelled"}:
                self._pending_authority = False
        if action == "motor.read_position" and event == "succeeded" and isinstance(payload, dict):
            value = payload.get("position_rad", payload.get("positions", "—"))
            self.current_position.setText(f"{value} rad")
        if (
            action in {"motor.enable", "motor.disable", "motor.emergency_stop", "motor.set_velocity"}
            and event == "succeeded"
            and not self._is_group_target()
        ):
            status: int | None = None
            if action == "motor.enable" and isinstance(payload, dict):
                logic_id = str(self.target.currentData()[1])
                status_values = payload.get("statuses", {})
                if isinstance(status_values, dict) and logic_id in status_values:
                    status = int(status_values[logic_id])
            elif action in {"motor.disable", "motor.emergency_stop", "motor.set_velocity"}:
                status = 0x40
            self._set_single_target_status(connected=True, status=status)
        if action.startswith("motor.") and event == "failed":
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            self.console.appendPlainText(f"[失败] {action.removeprefix('motor.')} · {error}")
        elif action.startswith("motor.") and event == "succeeded":
            operation = action.removeprefix("motor.")
            if operation == "set_velocity" and isinstance(payload, dict):
                statuses = payload.get("statuses", {})
                positions = payload.get("positions", {})
                status_text = ", ".join(
                    f"{logic_id}=0x{int(status):02X}"
                    for logic_id, status in dict(statuses).items()
                )
                position_text = ", ".join(
                    f"{logic_id}={float(position):.4f} rad"
                    for logic_id, position in dict(positions).items()
                )
                details = " · ".join(
                    item
                    for item in (
                        f"运行状态 {status_text}" if status_text else "",
                        f"最终位置 {position_text}" if position_text else "",
                        "已安全失能",
                    )
                    if item
                )
                self.console.appendPlainText(f"[完成] {operation} · {details}")
            else:
                self.console.appendPlainText(f"[完成] {operation}")


class ParameterPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(
            PageHeader("电机参数", "按白名单读取和写入参数；保存到 Flash 与写入 RAM 分离。")
        )
        self.layout.addWidget(FunctionConnectionBar(state, ((ConnectionMode.ORIN_REMOTE, "Orin"),)))
        target_card = Card("目标与参数")
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("目标"))
        self.target = QComboBox()
        add_motor_targets(self.target, state)
        target_row.addWidget(self.target, 1)
        read = QPushButton("读取参数")
        read.clicked.connect(lambda: self._request("read"))
        target_row.addWidget(read)
        target_card.body.addLayout(target_row)
        self.table = QTableWidget(5, 5)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.table.setHorizontalHeaderLabels(["参数", "当前值", "新值", "单位", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        rows = [
            ("communication_timeout_ms", "—", "", "ms", "未读取"),
            ("permission", "—", "", "", "未读取"),
            ("alarm_mask", "—", "", "bitmask", "未读取"),
            ("max_speed_rpm", "—", "", "rpm", "未读取"),
            ("function_switch_mask", "—", "", "bitmask", "未读取"),
        ]
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column != 2:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)
        target_card.body.addWidget(self.table)
        actions = QHBoxLayout()
        write = QPushButton("写入 RAM 并回读")
        write.clicked.connect(lambda: self._request("write"))
        save = QPushButton("保存到 Flash")
        save.setProperty("danger", True)
        save.clicked.connect(self._save)
        actions.addStretch(1)
        actions.addWidget(write)
        actions.addWidget(save)
        target_card.body.addLayout(actions)
        self.layout.addWidget(target_card)
        state.task_event.connect(self._on_task_event)

    def _payload(self) -> dict[str, object]:
        kind, value = self.target.currentData()
        values = {}
        for row in range(self.table.rowCount()):
            entered = self.table.item(row, 2).text().strip()
            if entered:
                values[self.table.item(row, 0).text()] = entered
        return {"target": {kind: value}, "values": values}

    def _request(self, operation: str) -> None:
        if not require_motion_ready(self, self.state):
            return
        self.state.request(f"motor.param_{operation}", **self._payload())

    def _save(self) -> None:
        if not require_motion_ready(self, self.state):
            return
        if (
            QMessageBox.question(self, "保存到 Flash", "确认把已写入并回读成功的参数保存到 Flash？")
            == QMessageBox.StandardButton.Yes
        ):
            self.state.request("motor.param_save", **self._payload())

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if not action.startswith("motor.param_"):
            return
        if event == "failed":
            error = payload.get("error", "失败") if isinstance(payload, dict) else "失败"
            for row in range(self.table.rowCount()):
                self.table.item(row, 4).setText(str(error))
            return
        if event != "succeeded" or not isinstance(payload, dict):
            return
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 0).text()
            if action == "motor.param_read" and name in payload:
                value = payload[name]
                if isinstance(value, dict):
                    value = value.get("value", value.get("data", value))
                self.table.item(row, 1).setText(str(value))
                self.table.item(row, 4).setText("已读取")
            elif action == "motor.param_write" and name in payload.get("written", []):
                self.table.item(row, 4).setText("已写入 RAM")
            elif action == "motor.param_save":
                self.table.item(row, 4).setText("已保存 Flash")


class ZeroCalibrationPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(PageHeader("电机标零", "用手将各关节转到图示零位，核对后再执行准备与写入。"))
        self.layout.addWidget(
            InlineMessage(
                "标定会改变电机零位。请卸载负载或可靠支撑机构，并让无关人员离开运动范围。", "danger"
            )
        )
        work_row = QHBoxLayout()
        pose_card = Card("D7 零位参考", "现场姿态应与图示一致；重点核对双臂、躯干和底盘朝向。")
        self.pose_image = QLabel()
        self.pose_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pose_image.setMinimumSize(260, 350)
        self.pose_image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        pose_path = Path(__file__).resolve().parents[2] / "resources" / "d7-zero-pose.png"
        pose = QPixmap(str(pose_path))
        if pose.isNull():
            self.pose_image.setText("零位参考图加载失败")
        else:
            self.pose_image.setPixmap(
                pose.scaled(
                    340,
                    430,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        pose_card.body.addWidget(self.pose_image)
        work_row.addWidget(pose_card, 2)

        card = Card("标定目标", "先人工对位，再准备标定；只有准备成功后才允许提交零位。")
        form = FormSection()
        self.target_mode = QComboBox()
        self.target_mode.addItems(["单电机", "多个固定分组"])
        self.target_mode.currentIndexChanged.connect(self._target_mode_changed)
        form.add_field("标定范围", self.target_mode)
        card.body.addWidget(form)
        self.target_stack = QStackedWidget()
        self.target_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.target_stack.setMaximumHeight(92)
        self.target = QComboBox()
        add_motor_targets(self.target, state, include_groups=False)
        self.target_stack.addWidget(self.target)
        group_panel = QWidget()
        group_layout = QGridLayout(group_panel)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setHorizontalSpacing(18)
        group_layout.setVerticalSpacing(10)
        self.group_checks: dict[str, QCheckBox] = {}
        for index, (group, ids) in enumerate(state.evt.fixed_groups.items()):
            checkbox = QCheckBox(f"{group_label(group)}（{len(ids)} 个电机）")
            self.group_checks[group] = checkbox
            group_layout.addWidget(checkbox, index // 2, index % 2)
        self.target_stack.addWidget(group_panel)
        card.body.addWidget(self.target_stack)
        self.mechanical_confirm = QCheckBox("机械位置已人工对准，电机当前处于失能状态")
        self.prepare_confirm = QCheckBox("我已核对设备 ID、总线和关节名称")
        card.body.addWidget(self.mechanical_confirm)
        card.body.addWidget(self.prepare_confirm)
        actions = QHBoxLayout()
        self.prepare = QPushButton("准备标定")
        self.prepare.clicked.connect(self._prepare)
        self.commit = QPushButton("提交零位")
        self.commit.setProperty("danger", True)
        self.commit.setEnabled(False)
        self.commit.clicked.connect(self._commit)
        actions.addStretch(1)
        actions.addWidget(self.prepare)
        actions.addWidget(self.commit)
        card.body.addLayout(actions)
        work_row.addWidget(card, 3)
        self.layout.addLayout(work_row)
        state.task_event.connect(self._on_task_event)

    def _target_mode_changed(self, index: int) -> None:
        self.target_stack.setCurrentIndex(index)

    def _target_payload(self) -> dict[str, object]:
        if self.target_mode.currentIndex() == 0:
            _kind, logic_id = self.target.currentData()
            return {"motor": int(logic_id)}
        ids = sorted(
            {
                logic_id
                for group, checkbox in self.group_checks.items()
                if checkbox.isChecked()
                for logic_id in self.state.evt.fixed_groups.get(group, ())
            }
        )
        return {"motors": ids}

    def _target_text(self) -> str:
        if self.target_mode.currentIndex() == 0:
            return self.target.currentText()
        return "、".join(
            group_label(group) for group, checkbox in self.group_checks.items() if checkbox.isChecked()
        )

    def _prepare(self) -> None:
        if not require_motion_ready(self, self.state):
            return
        if not self.mechanical_confirm.isChecked() or not self.prepare_confirm.isChecked():
            QMessageBox.warning(self, "确认未完成", "请完成两项现场确认。")
            return
        target = self._target_payload()
        if target.get("motors") == []:
            QMessageBox.warning(self, "未选择分组", "请至少选择一个需要标定的固定分组。")
            return
        self.state.request("motor.zero_prepare", target=target)
        self.prepare.setEnabled(False)

    def _commit(self) -> None:
        if (
            QMessageBox.warning(
                self,
                "最后确认",
                "提交后将写入新的零位。确认继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.state.request("motor.zero_commit", target=self._target_payload())
        self.commit.setEnabled(False)

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action == "motor.zero_prepare":
            if event == "succeeded":
                self.commit.setEnabled(True)
                self.state.log("零位", f"标定准备完成: {self._target_text()}", "warning")
            elif event in {"failed", "cancelled"}:
                self.prepare.setEnabled(True)
        elif action == "motor.zero_commit" and event in {"succeeded", "failed", "cancelled"}:
            self.prepare.setEnabled(True)
            self.commit.setEnabled(False)


class LongTestPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(
            PageHeader("电机长测", "固定分组执行位置或速度循环；停止、异常和掉线都会触发零速与失能。")
        )
        self.layout.addWidget(FunctionConnectionBar(state, ((ConnectionMode.ORIN_REMOTE, "Orin"),)))
        card = Card("循环计划")
        form = FormSection()
        self.target = QComboBox()
        for group, ids in state.evt.fixed_groups.items():
            self.target.addItem(f"{group_label(group)} ({len(ids)})", group)
        self.mode = QComboBox()
        self.mode.addItem("位置往返", "position")
        self.mode.addItem("速度正反转", "velocity")
        self.cycles = QSpinBox()
        self.cycles.setRange(1, 100000)
        self.cycles.setValue(100)
        self.dwell = QDoubleSpinBox()
        self.dwell.setRange(0.1, 3600.0)
        self.dwell.setValue(2.0)
        self.dwell.setSuffix(" s")
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.01, 0.5)
        self.speed.setValue(0.1)
        self.speed.setSuffix(" rad/s")
        form.add_field("固定分组", self.target)
        form.add_field("测试模式", self.mode)
        form.add_field("循环次数", self.cycles)
        form.add_field("单阶段时间", self.dwell)
        form.add_field("速度上限", self.speed)
        card.body.addWidget(form)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        card.body.addWidget(self.progress)
        actions = QHBoxLayout()
        self.stop = QPushButton("停止并失能")
        self.stop.setProperty("danger", True)
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self._stop)
        self.start = QPushButton("开始长测")
        self.start.setProperty("primary", True)
        self.start.clicked.connect(self._start)
        actions.addStretch(1)
        actions.addWidget(self.stop)
        actions.addWidget(self.start)
        card.body.addLayout(actions)
        self.layout.addWidget(card)
        logs = Card("长测日志")
        self.console = LogConsole()
        logs.body.addWidget(self.console)
        self.layout.addWidget(logs)
        state.task_event.connect(self._on_task_event)

    def _start(self) -> None:
        if not require_motion_ready(self, self.state):
            return
        self.state.request(
            "motor.long_test_start",
            group=self.target.currentData(),
            mode=self.mode.currentData(),
            cycles=self.cycles.value(),
            dwell_s=self.dwell.value(),
            max_rad_s=self.speed.value(),
        )
        self.start.setEnabled(False)
        self.stop.setEnabled(True)

    def _stop(self) -> None:
        self.state.request("motor.long_test_cancel")
        self.stop.setEnabled(False)

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action != "motor.long_test_start":
            return
        if event == "progress" and isinstance(payload, dict):
            self.progress.setValue(int(payload.get("progress", 0)))
            self.console.appendPlainText(str(payload.get("message", "")))
        elif event in {"succeeded", "failed", "cancelled"}:
            self.start.setEnabled(True)
            self.stop.setEnabled(False)
            if event == "succeeded":
                self.progress.setValue(100)
                self.console.appendPlainText("[完成] 长测计划执行完成")
            elif event == "failed" and isinstance(payload, dict):
                self.console.appendPlainText(f"[失败] {payload.get('error', '未知错误')}")
            else:
                self.console.appendPlainText("[停止] 已零速并失能目标组")
