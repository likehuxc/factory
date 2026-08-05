from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import LinkState
from d7_factory_studio.ui.pages.base import FormSection, LogConsole, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, Metric, PageHeader


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

    def _overview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        battery = Card("电池状态")
        metrics = QHBoxLayout()
        self.soc = Metric("SOC", "—", "#21875A")
        self.voltage = Metric("总电压", "—")
        self.current = Metric("电流", "—", "#C67A16")
        self.temperature = Metric("最高温度", "—", "#D94747")
        for metric in (self.soc, self.voltage, self.current, self.temperature):
            metrics.addWidget(metric)
        metrics.addStretch(1)
        battery.body.addLayout(metrics)
        battery_actions = QHBoxLayout()
        self.battery_id = QComboBox()
        self.battery_id.setEditable(True)
        self.battery_id.addItems(["0x42", "0x41", "0x43"])
        query = QPushButton("读取电池")
        query.setProperty("primary", True)
        query.clicked.connect(self._query_battery)
        battery_actions.addWidget(QLabel("电池 ID"))
        battery_actions.addWidget(self.battery_id)
        battery_actions.addStretch(1)
        battery_actions.addWidget(query)
        battery.body.addLayout(battery_actions)
        layout.addWidget(battery)

        info = Card("MachineInfo")
        grid = QGridLayout()
        labels = ["机器人 SN", "硬件版本", "软件版本", "EVT", "Orin", "运行时间"]
        self.info_values: dict[str, QLabel] = {}
        for index, name in enumerate(labels):
            title = QLabel(name)
            title.setObjectName("Muted")
            value = QLabel("—")
            value.setObjectName("Mono")
            self.info_values[name] = value
            grid.addWidget(title, index // 3 * 2, index % 3)
            grid.addWidget(value, index // 3 * 2 + 1, index % 3)
        info.body.addLayout(grid)
        refresh = QPushButton("刷新 MachineInfo")
        refresh.clicked.connect(lambda: self.state.request("machine.info_read"))
        info.body.addWidget(refresh)
        layout.addWidget(info)
        return tab

    def _light_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        zones = ["头部灯", "胸前灯", "左臂灯", "右臂灯", "底盘灯"]
        card = Card("灯光快捷控制")
        grid = QGridLayout()
        for index, zone in enumerate(zones):
            on = QPushButton(f"{zone} · 开")
            off = QPushButton(f"{zone} · 关")
            on.clicked.connect(lambda _checked=False, name=zone: self._light(name, True))
            off.clicked.connect(lambda _checked=False, name=zone: self._light(name, False))
            grid.addWidget(on, index, 0)
            grid.addWidget(off, index, 1)
        card.body.addLayout(grid)
        layout.addWidget(card)
        pattern = Card("整机灯效")
        self.pattern = QComboBox()
        self.pattern.addItems(["常亮", "呼吸", "流水", "告警闪烁", "全部关闭"])
        apply_pattern = QPushButton("应用灯效")
        apply_pattern.setProperty("primary", True)
        apply_pattern.clicked.connect(
            lambda: self.state.request("machine.light_pattern", pattern=self.pattern.currentText())
        )
        pattern.body.addWidget(self.pattern)
        pattern.body.addWidget(apply_pattern)
        pattern.body.addStretch(1)
        layout.addWidget(pattern)
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
        self.console = LogConsole()
        receive.body.addWidget(self.console)
        layout.addWidget(receive)
        return tab

    def _ready(self) -> bool:
        if self.state.link_state is not LinkState.CONNECTED:
            QMessageBox.warning(self, "设备未连接", "请先连接设备。")
            return False
        return True

    def _query_battery(self) -> None:
        if not self._ready():
            return
        try:
            target_id = int(self.battery_id.currentText(), 0)
            if not 0 <= target_id <= 0x7FF:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "电池 ID 无效", "请输入 0x000–0x7FF 的 11 位 CAN ID。")
            return
        self.state.request("machine.battery_read", target_id=target_id)

    def _light(self, zone: str, enabled: bool) -> None:
        if self._ready():
            self.state.request("machine.light_set", zone=zone, enabled=enabled)

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
