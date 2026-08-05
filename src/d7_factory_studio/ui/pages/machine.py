from __future__ import annotations

import csv
from datetime import datetime

from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import CanFrame, LinkState
from d7_factory_studio.protocols.machine_info import MACHINE_INFO_FIELDS
from d7_factory_studio.ui.pages.base import FormSection, LogConsole, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, PageHeader

LIGHT_MODES = (
    ("熄灭", 0),
    ("常亮", 1),
    ("闪烁后熄灭", 2),
    ("呼吸", 3),
    ("闪烁后常亮", 4),
    ("自定义 RGB", 5),
    ("彩虹渐变", 6),
    ("紧急红灯", 7),
    ("向左跑马", 8),
    ("向右流水", 9),
    ("待机呼吸", 10),
    ("关机", 11),
)
LIGHT_COLORS = (
    ("黑色", 0, (0, 0, 0)),
    ("白色", 1, (255, 255, 255)),
    ("红色", 2, (255, 0, 0)),
    ("绿色", 3, (0, 255, 0)),
    ("蓝色", 4, (0, 0, 255)),
    ("橙色", 5, (255, 165, 0)),
    ("黄色", 6, (255, 255, 0)),
    ("天蓝", 7, (0, 102, 255)),
)


class MachinePage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(
            PageHeader("整机控制", "集中查看电池、灯光、MachineInfo，并保留原始 CAN 控制台。")
        )
        tabs = QTabWidget()
        tabs.addTab(self._overview_tab(), "电池与整机")
        tabs.addTab(self._light_tab(), "灯光控制")
        tabs.addTab(self._console_tab(), "CAN 控制台")
        self.layout.addWidget(tabs)
        state.task_event.connect(self._on_task_event)

    def _overview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        battery = Card(
            "电池节点模拟",
            "周期发送电压 0x02、电流 0x0E 和 SOC 0x81，用于验证 Orin 的电池接收链路。",
        )
        battery_form = QGridLayout()
        self.battery_id = QComboBox()
        self.battery_id.addItem("电池 1 · 0x41", (0x41,))
        self.battery_id.addItem("电池 2 · 0x42", (0x42,))
        self.battery_id.addItem("两块电池", (0x41, 0x42))
        self.battery_interval = QSpinBox()
        self.battery_interval.setRange(100, 60_000)
        self.battery_interval.setValue(1000)
        self.battery_interval.setSuffix(" ms")
        self.battery_voltage = QDoubleSpinBox()
        self.battery_voltage.setRange(0, 100)
        self.battery_voltage.setDecimals(2)
        self.battery_voltage.setValue(52.0)
        self.battery_voltage.setSuffix(" V")
        self.battery_current = QDoubleSpinBox()
        self.battery_current.setRange(-100, 100)
        self.battery_current.setDecimals(2)
        self.battery_current.setValue(-1.0)
        self.battery_current.setSuffix(" A")
        self.battery_soc = QSpinBox()
        self.battery_soc.setRange(0, 100)
        self.battery_soc.setValue(80)
        self.battery_soc.setSuffix(" %")
        for row, (label, widget) in enumerate(
            (
                ("模拟节点", self.battery_id),
                ("上报周期", self.battery_interval),
                ("电压", self.battery_voltage),
                ("电流", self.battery_current),
                ("容量 / SOC", self.battery_soc),
            )
        ):
            battery_form.addWidget(QLabel(label), row, 0)
            battery_form.addWidget(widget, row, 1)
        battery.body.addLayout(battery_form)
        battery_actions = QHBoxLayout()
        self.battery_start = QPushButton("开始周期发送")
        self.battery_start.setProperty("primary", True)
        self.battery_start.clicked.connect(self._start_battery_simulation)
        self.battery_stop = QPushButton("停止")
        self.battery_stop.setProperty("danger", True)
        self.battery_stop.setEnabled(False)
        self.battery_stop.clicked.connect(
            lambda: self.state.request("machine.battery_simulation_stop")
        )
        self.battery_status = QLabel("已停止")
        self.battery_status.setObjectName("Muted")
        battery_actions.addWidget(self.battery_start)
        battery_actions.addWidget(self.battery_stop)
        battery_actions.addStretch(1)
        battery_actions.addWidget(self.battery_status)
        battery.body.addLayout(battery_actions)
        layout.addWidget(battery)

        info = Card("MachineInfo")
        self.info_table = QTableWidget(len(MACHINE_INFO_FIELDS), 4)
        self.info_table.setHorizontalHeaderLabels(["字段", "Slot", "类型", "值"])
        self.info_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.info_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.info_table.verticalHeader().setVisible(False)
        self.info_table.setMinimumHeight(260)
        for row, field in enumerate(MACHINE_INFO_FIELDS):
            for column, value in enumerate((field.key, field.slot, field.type_label, "未读取")):
                self.info_table.setItem(row, column, QTableWidgetItem(str(value)))
        info.body.addWidget(self.info_table)
        refresh = QPushButton("刷新 MachineInfo")
        refresh.clicked.connect(lambda: self.state.request("machine.info_read"))
        info.body.addWidget(refresh)
        layout.addWidget(info)
        return tab

    def _light_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card("Orin 灯控", "保留目标 ID、灯带、12 种模式、颜色、次数和周期参数。")
        grid = QGridLayout()
        self.light_can_id = QLineEdit("0x08")
        self.light_strip = QComboBox()
        for strip_id in range(200, 204):
            self.light_strip.addItem(f"灯带 {strip_id}", (strip_id,))
        self.light_strip.addItem("全部灯带", tuple(range(200, 204)))
        self.light_mode = QComboBox()
        for name, value in LIGHT_MODES:
            self.light_mode.addItem(name, value)
        self.light_color = QComboBox()
        for name, color_id, rgb in LIGHT_COLORS:
            self.light_color.addItem(name, (color_id, rgb))
        self.light_color.setCurrentText("红色")
        self.light_count = QSpinBox()
        self.light_count.setRange(0, 255)
        self.light_count.setValue(1)
        self.light_cycle = QSpinBox()
        self.light_cycle.setRange(0, 65535)
        self.light_cycle.setValue(500)
        self.light_cycle.setSuffix(" ms")
        self._custom_rgb = (255, 0, 0)
        custom_color = QPushButton("选择自定义 RGB")
        custom_color.clicked.connect(self._choose_light_color)
        grid.addWidget(QLabel("目标 ID"), 0, 0)
        grid.addWidget(self.light_can_id, 0, 1)
        grid.addWidget(QLabel("灯带"), 0, 2)
        grid.addWidget(self.light_strip, 0, 3)
        grid.addWidget(QLabel("模式"), 1, 0)
        grid.addWidget(self.light_mode, 1, 1)
        grid.addWidget(QLabel("颜色"), 1, 2)
        grid.addWidget(self.light_color, 1, 3)
        grid.addWidget(QLabel("次数"), 2, 0)
        grid.addWidget(self.light_count, 2, 1)
        grid.addWidget(QLabel("周期"), 2, 2)
        grid.addWidget(self.light_cycle, 2, 3)
        grid.addWidget(custom_color, 3, 3)
        card.body.addLayout(grid)
        send = QPushButton("发送灯控")
        send.setProperty("primary", True)
        send.clicked.connect(self._send_light_command)
        card.body.addWidget(send)
        layout.addWidget(card)
        pattern = Card("整机快捷灯效")
        self.pattern = QComboBox()
        self.pattern.addItems(["常亮", "呼吸", "流水", "告警闪烁", "全部关闭"])
        apply_pattern = QPushButton("应用灯效")
        apply_pattern.setProperty("primary", True)
        apply_pattern.clicked.connect(self._apply_light_pattern)
        pattern.body.addWidget(self.pattern)
        pattern.body.addWidget(apply_pattern)
        layout.addWidget(pattern)
        layout.addStretch(1)
        return tab

    def _console_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card("原始 CAN 发送")
        form = FormSection()
        self.can_id = QLineEdit("0x000")
        self.can_data = QLineEdit()
        self.can_data.setPlaceholderText("十六进制字节，例如 01 02 A0 FF")
        self.frame_type = QComboBox()
        self.frame_type.addItem("CAN FD + BRS", "fd_brs")
        self.frame_type.addItem("CAN FD", "fd")
        self.frame_type.addItem("Classic CAN", "classic")
        form.add_field("CAN ID", self.can_id)
        form.add_field("数据", self.can_data)
        form.add_field("帧类型", self.frame_type)
        card.body.addWidget(form)
        send = QPushButton("发送原始帧")
        send.setProperty("danger", True)
        send.clicked.connect(self._send_raw)
        card.body.addWidget(send)
        layout.addWidget(card)
        receive = Card("收发记录")
        monitor = QHBoxLayout()
        self.can_filter = QLineEdit()
        self.can_filter.setPlaceholderText("筛选 CAN ID 或数据，例如 0x41 / 82 02")
        self.can_filter.textChanged.connect(self._render_can_records)
        self.pause_capture = QCheckBox("暂停显示")
        self.monitor_start = QPushButton("开始监听")
        self.monitor_start.clicked.connect(self._start_can_monitor)
        self.monitor_stop = QPushButton("停止监听")
        self.monitor_stop.setEnabled(False)
        self.monitor_stop.clicked.connect(lambda: self.state.request("machine.can_monitor_stop"))
        export = QPushButton("导出 CSV")
        export.clicked.connect(self._export_can_records)
        monitor.addWidget(self.can_filter, 1)
        monitor.addWidget(self.pause_capture)
        monitor.addWidget(self.monitor_start)
        monitor.addWidget(self.monitor_stop)
        monitor.addWidget(export)
        receive.body.addLayout(monitor)
        self.console = LogConsole()
        self.can_records: list[tuple[str, str, CanFrame]] = []
        receive.body.addWidget(self.console)
        layout.addWidget(receive)
        return tab

    def _ready(self) -> bool:
        if self.state.link_state is not LinkState.CONNECTED:
            QMessageBox.warning(self, "设备未连接", "请先连接设备。")
            return False
        return True

    def _start_battery_simulation(self) -> None:
        if not self._ready():
            return
        self.state.request(
            "machine.battery_simulation_start",
            target_ids=list(self.battery_id.currentData()),
            interval_ms=self.battery_interval.value(),
            voltage_v=self.battery_voltage.value(),
            current_a=self.battery_current.value(),
            soc=self.battery_soc.value(),
        )

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action == "machine.battery_simulation_start":
            if event == "started":
                self.battery_start.setEnabled(False)
                self.battery_stop.setEnabled(True)
                self.battery_status.setText(f"运行中 · {self.battery_interval.value()} ms")
            elif event == "progress" and isinstance(payload, dict):
                self.battery_status.setText(str(payload.get("message", "运行中")))
            elif event in {"succeeded", "failed", "cancelled"}:
                self.battery_start.setEnabled(True)
                self.battery_stop.setEnabled(False)
                self.battery_status.setText("已停止" if event != "failed" else "异常停止")
        elif action == "machine.can_send" and event == "succeeded":
            if isinstance(payload, dict) and isinstance(payload.get("frame"), CanFrame):
                self._record_can_frame("TX", payload["frame"])
            else:
                self.console.appendPlainText("[TX] 原始 CAN 帧发送成功")
        elif action == "machine.can_send" and event == "failed":
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            self.console.appendPlainText(f"[拒绝] {error}")
        elif action == "machine.info_read" and event == "succeeded" and isinstance(payload, dict):
            values = payload.get("values", {})
            for row, field in enumerate(MACHINE_INFO_FIELDS):
                self.info_table.item(row, 3).setText(str(values.get(field.key, "未响应")))
        elif action == "machine.can_frame" and event == "event" and isinstance(payload, CanFrame):
            self._record_can_frame("RX", payload)
        elif action == "machine.can_monitor_start":
            if event == "started":
                self.monitor_start.setEnabled(False)
                self.monitor_stop.setEnabled(True)
            elif event in {"succeeded", "failed", "cancelled"}:
                self.monitor_start.setEnabled(True)
                self.monitor_stop.setEnabled(False)

    def _start_can_monitor(self) -> None:
        if self._ready():
            self.state.request("machine.can_monitor_start")

    def _record_can_frame(self, direction: str, frame: CanFrame) -> None:
        self.can_records.append((datetime.now().isoformat(timespec="milliseconds"), direction, frame))
        if not self.pause_capture.isChecked():
            self._render_can_records()

    def _render_can_records(self) -> None:
        query = self.can_filter.text().strip().lower() if hasattr(self, "can_filter") else ""
        lines = []
        for timestamp, direction, frame in self.can_records:
            text = (
                f"{timestamp[11:]} {direction} 0x{frame.arbitration_id:03X} "
                f"[{len(frame.data):02d}] {frame.data.hex(' ').upper()}"
            )
            if not query or query in text.lower():
                lines.append(text)
        self.console.setPlainText("\n".join(lines[-5000:]))
        self.console.verticalScrollBar().setValue(self.console.verticalScrollBar().maximum())

    def _export_can_records(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出 CAN 收发记录", "d7-can-records.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(("time", "direction", "can_id", "length", "frame_type", "data"))
            for timestamp, direction, frame in self.can_records:
                writer.writerow(
                    (
                        timestamp,
                        direction,
                        f"0x{frame.arbitration_id:X}",
                        len(frame.data),
                        "CAN FD" if frame.is_fd else "Classic CAN",
                        frame.data.hex(" ").upper(),
                    )
                )

    def _choose_light_color(self) -> None:
        color = QColorDialog.getColor(parent=self, title="选择自定义 RGB")
        if color.isValid():
            self._custom_rgb = (color.red(), color.green(), color.blue())

    def _send_light_command(self) -> None:
        if not self._ready():
            return
        try:
            can_id = int(self.light_can_id.text(), 0)
            if not 0 <= can_id <= 0x7FF:
                raise ValueError("目标 ID 必须在 0x000–0x7FF")
        except ValueError as exc:
            QMessageBox.warning(self, "灯控参数无效", str(exc))
            return
        color_id, palette_rgb = self.light_color.currentData()
        rgb = self._custom_rgb if self.light_mode.currentData() == 5 else palette_rgb
        self.state.request(
            "machine.light_command",
            can_id=can_id,
            strip_ids=list(self.light_strip.currentData()),
            color_id=color_id,
            mode=self.light_mode.currentData(),
            count=self.light_count.value(),
            cycle_ms=self.light_cycle.value(),
            rgb=list(rgb),
        )

    def _apply_light_pattern(self) -> None:
        if self._ready():
            self.state.request("machine.light_pattern", pattern=self.pattern.currentText())

    def _send_raw(self) -> None:
        if not self._ready():
            return
        try:
            can_id = int(self.can_id.text(), 0)
            data = bytes.fromhex(self.can_data.text())
            if not 0 <= can_id <= 0x7FF:
                raise ValueError("CAN ID 超出 11 位范围")
            maximum = 8 if self.frame_type.currentData() == "classic" else 64
            if len(data) > maximum:
                raise ValueError(f"当前帧最多 {maximum} 字节")
        except ValueError as exc:
            QMessageBox.warning(self, "原始帧无效", str(exc))
            return
        if (
            QMessageBox.question(self, "发送原始 CAN", f"确认向 0x{can_id:03X} 发送 {len(data)} 字节？")
            == QMessageBox.StandardButton.Yes
        ):
            self.state.request(
                "machine.can_send", arbitration_id=can_id, data=data, frame_type=self.frame_type.currentData()
            )
