from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import LinkState
from d7_factory_studio.ui.pages.base import WorkbenchPage
from d7_factory_studio.ui.theme import COLORS
from d7_factory_studio.ui.widgets import Card, Metric, PageHeader


class HomePage(WorkbenchPage):
    navigate_requested = Signal(str)

    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(
            PageHeader("D7 功能工作台", "用一条清晰的设备链路组织升级、电机、诊断和整机任务。")
        )

        overview = Card()
        metrics = QHBoxLayout()
        metrics.setSpacing(32)
        self.connection_metric = Metric("连接链路", "未连接", COLORS["primary"])
        self.nodes_metric = Metric("在线节点", "0 / 30", COLORS["teal"])
        self.evt_metric = Metric("整机配置", "EVT2", COLORS["warning"])
        self.lock_metric = Metric("运动安全", "已锁定", COLORS["danger"])
        for metric in (self.connection_metric, self.nodes_metric, self.evt_metric, self.lock_metric):
            metrics.addWidget(metric)
        metrics.addStretch(1)
        overview.body.addLayout(metrics)
        self.layout.addWidget(overview)

        middle = QHBoxLayout()
        middle.setSpacing(16)
        bus_card = Card("D7 总线状态轨", "总线名称和节点目录来自当前 EVT YAML。")
        self.bus_table = QTableWidget(0, 5)
        self.bus_table.setHorizontalHeaderLabels(["接口", "角色", "类型", "节点", "状态"])
        self.bus_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.bus_table.verticalHeader().setVisible(False)
        self.bus_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.bus_table.setMinimumHeight(260)
        bus_card.body.addWidget(self.bus_table)
        middle.addWidget(bus_card, 3)

        quick_card = Card("快捷操作", "所有硬件操作都会遵守当前连接通道和安全锁。")
        grid = QGridLayout()
        actions = [
            ("升级 PMU", "firmware"),
            ("CAN 电机", "motor"),
            ("链路诊断", "diagnostics"),
            ("下载日志", "logs"),
        ]
        for index, (label, page) in enumerate(actions):
            button = QPushButton(label)
            button.setMinimumHeight(48)
            button.clicked.connect(lambda _checked=False, key=page: self.navigate_requested.emit(key))
            grid.addWidget(button, index // 2, index % 2)
        quick_card.body.addLayout(grid)
        quick_card.body.addStretch(1)
        middle.addWidget(quick_card, 2)
        self.layout.addLayout(middle)

        activity_card = Card("最近活动", "当前会话内的连接、配置、安全和任务事件。")
        self.activity_table = QTableWidget(0, 3)
        self.activity_table.setHorizontalHeaderLabels(["时间", "来源", "内容"])
        self.activity_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.activity_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.activity_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.activity_table.verticalHeader().setVisible(False)
        self.activity_table.setAlternatingRowColors(True)
        self.activity_table.setMinimumHeight(190)
        activity_card.body.addWidget(self.activity_table)
        self.layout.addWidget(activity_card)

        state.changed.connect(self.refresh)
        state.activity_added.connect(self.add_activity)
        self.refresh()

    def refresh(self) -> None:
        link_text = {
            LinkState.DISCONNECTED: "未连接",
            LinkState.CONNECTING: "连接中",
            LinkState.CONNECTED: "已连接",
            LinkState.FAULT: "故障",
        }[self.state.link_state]
        mode = "PC" if self.state.connection_mode.value == "pc_direct" else "Orin"
        self.connection_metric.set_value(f"{mode} · {link_text}")
        self.nodes_metric.set_value(f"{self.state.online_nodes} / {len(self.state.evt.nodes)}")
        self.evt_metric.set_value(self.state.evt.variant)
        self.lock_metric.set_value("已锁定" if self.state.safety_locked else "已解锁")
        self.bus_table.setRowCount(0)
        for name, interface in self.state.evt.interfaces.items():
            row = self.bus_table.rowCount()
            self.bus_table.insertRow(row)
            values = [
                name.upper(),
                interface.role.replace("_", " "),
                "CAN FD" if interface.mode.value == "fd" else "Classic CAN",
                str(len(self.state.evt.nodes_for_bus(name))),
                "在线" if self.state.link_state is LinkState.CONNECTED else "未连接",
            ]
            for column, value in enumerate(values):
                self.bus_table.setItem(row, column, QTableWidgetItem(value))

    def add_activity(self, timestamp: str, source: str, payload: str) -> None:
        _level, _, message = payload.partition("|")
        self.activity_table.insertRow(0)
        self.activity_table.setItem(0, 0, QTableWidgetItem(timestamp))
        self.activity_table.setItem(0, 1, QTableWidgetItem(source))
        self.activity_table.setItem(0, 2, QTableWidgetItem(message))
        if self.activity_table.rowCount() > 100:
            self.activity_table.removeRow(100)
