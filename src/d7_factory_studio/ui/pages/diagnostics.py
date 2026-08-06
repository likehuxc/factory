from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import LinkState
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
from d7_factory_studio.ui.pages.base import FormSection, InlineMessage, LogConsole, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, PageHeader, clear_layout


class DiagnosticsPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self._topology_signature: tuple[object, ...] | None = None
        self.layout.addWidget(
            PageHeader("链路诊断", "从网络路径到 SocketCAN、压测、节点与位时序，保留每一步原始证据。")
        )
        self.layout.addWidget(
            InlineMessage("dmesg -C 默认禁用。取消任务会终止远程进程组，不允许残留 candump 或 cangen。")
        )

        tabs = QTabWidget()
        tabs.addTab(self._stress_tab(), "CAN 压测")
        tabs.addTab(self._network_tab(), "网络与 SocketCAN")
        tabs.addTab(self._nodes_tab(), "节点与广播")
        tabs.addTab(self._timing_tab(), "位时序 / TDC")
        self.layout.addWidget(tabs)
        state.changed.connect(self._rebuild_interfaces)
        state.task_event.connect(self._on_task_event)
        self._rebuild_interfaces()

    def _stress_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(14)
        setup = Card("测试计划")
        form = FormSection()
        self.profile = QComboBox()
        self.profile.addItem("快速检查", "quick")
        self.profile.addItem("标准测试", "standard")
        self.profile.addItem("长时间测试", "long")
        self.profile.addItem("自定义", "custom")
        self.duration = QSpinBox()
        self.duration.setRange(1, 86400)
        self.duration.setValue(60)
        self.duration.setSuffix(" s")
        self.packet_gap = QSpinBox()
        self.packet_gap.setRange(0, 1000)
        self.packet_gap.setValue(1)
        self.packet_gap.setSuffix(" ms")
        form.add_field("测试配置", self.profile)
        form.add_field("自定义时长", self.duration)
        form.add_field("发送间隔", self.packet_gap)
        setup.body.addWidget(form)
        setup.body.addWidget(QLabel("测试接口"))
        self.interface_checks_widget = QWidget()
        self.interface_checks = QHBoxLayout(self.interface_checks_widget)
        self.interface_checks.setContentsMargins(0, 0, 0, 0)
        self.interface_checks.setSpacing(8)
        setup.body.addWidget(self.interface_checks_widget)
        evidence = QHBoxLayout()
        self.capture_dmesg = QCheckBox("采集 dmesg 增量")
        self.capture_dmesg.setChecked(True)
        self.capture_candump = QCheckBox("采集 candump 原始帧")
        self.capture_candump.setChecked(True)
        self.clear_dmesg = QCheckBox("执行前清空 dmesg（高风险）")
        evidence.addWidget(self.capture_dmesg)
        evidence.addWidget(self.capture_candump)
        evidence.addWidget(self.clear_dmesg)
        evidence.addStretch(1)
        setup.body.addLayout(evidence)
        actions = QHBoxLayout()
        self.diag_stop = QPushButton("停止")
        self.diag_stop.setProperty("danger", True)
        self.diag_stop.setEnabled(False)
        self.diag_stop.clicked.connect(self._stop_stress)
        self.diag_start = QPushButton("开始诊断")
        self.diag_start.setProperty("primary", True)
        self.diag_start.clicked.connect(self._start_stress)
        actions.addStretch(1)
        actions.addWidget(self.diag_stop)
        actions.addWidget(self.diag_start)
        setup.body.addLayout(actions)
        layout.addWidget(setup)

        results = Card("实时证据")
        self.diag_progress = QProgressBar()
        self.diag_progress.setRange(0, 100)
        results.body.addWidget(self.diag_progress)
        self.diag_console = LogConsole()
        results.body.addWidget(self.diag_console)
        layout.addWidget(results)
        return tab

    def _network_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        network = Card("SSH / ADB 网络路径", "探测 RK3588、Orin 路由、IP forwarding 和转发规则。")
        probe = QPushButton("只读探测网络路径")
        probe.clicked.connect(lambda: self.state.request("diagnostics.network_probe"))
        configure = QPushButton("配置 ADB 网络转发")
        configure.clicked.connect(self._configure_forwarding)
        network.body.addWidget(probe)
        network.body.addWidget(configure)
        network.body.addStretch(1)
        layout.addWidget(network)

        socketcan = Card(
            "SocketCAN", "读取 /sys/class/net 与 ip -details -statistics，不根据接口名猜测类型。"
        )
        scan = QPushButton("探测 CAN 接口")
        scan.setProperty("primary", True)
        scan.clicked.connect(lambda: self.state.request("diagnostics.socketcan_probe"))
        apply_config = QPushButton("按 EVT 配置并读回")
        apply_config.clicked.connect(lambda: self.state.request("diagnostics.socketcan_configure"))
        socketcan.body.addWidget(scan)
        socketcan.body.addWidget(apply_config)
        socketcan.body.addStretch(1)
        layout.addWidget(socketcan)
        return tab

    def _nodes_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card("节点参数与广播", "先选择 CAN 通道，再对该通道内的节点读取参数或监听广播应答。")
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("测试 CAN"))
        self.node_interface = QComboBox()
        self.node_interface.setMinimumWidth(220)
        self.node_interface.currentIndexChanged.connect(self._rebuild_node_table)
        toolbar.addWidget(self.node_interface)
        self.node_count = QLabel("0 个节点")
        self.node_count.setObjectName("Muted")
        toolbar.addWidget(self.node_count)
        toolbar.addStretch(1)
        select_all = QPushButton("全选")
        select_all.clicked.connect(lambda: self._set_all_nodes(True))
        clear_all = QPushButton("清空")
        clear_all.clicked.connect(lambda: self._set_all_nodes(False))
        toolbar.addWidget(select_all)
        toolbar.addWidget(clear_all)
        card.body.addLayout(toolbar)
        self.node_table = QTableWidget(0, 6)
        self.node_table.setHorizontalHeaderLabels(["选择", "逻辑 ID", "节点", "设备 ID", "总线", "结果"])
        header = self.node_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.node_table.setColumnWidth(0, 76)
        self.node_table.setMinimumHeight(410)
        card.body.addWidget(self.node_table)
        actions = QHBoxLayout()
        read = QPushButton("读取所选参数")
        read.clicked.connect(self._read_nodes)
        write = QPushButton("写入并回读")
        write.clicked.connect(self._write_nodes)
        self.broadcast = QPushButton("监听广播应答")
        self.broadcast.setProperty("primary", True)
        self.broadcast.clicked.connect(self._run_broadcast)
        actions.addWidget(read)
        actions.addWidget(write)
        actions.addStretch(1)
        actions.addWidget(self.broadcast)
        card.body.addLayout(actions)
        layout.addWidget(card)
        return tab

    def _timing_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        calculator = Card("位时序计算")
        form = FormSection()
        self.clock_mhz = QDoubleSpinBox()
        self.clock_mhz.setRange(1, 1000)
        self.clock_mhz.setValue(40)
        self.clock_mhz.setSuffix(" MHz")
        self.sample_point = QDoubleSpinBox()
        self.sample_point.setRange(50, 95)
        self.sample_point.setValue(80)
        self.sample_point.setSuffix(" %")
        form.add_field("控制器时钟", self.clock_mhz)
        form.add_field("目标采样点", self.sample_point)
        calculator.body.addWidget(form)
        calculate = QPushButton("计算候选")
        calculate.clicked.connect(
            lambda: self.state.request(
                "diagnostics.timing_calculate",
                clock_mhz=self.clock_mhz.value(),
                sample_point=self.sample_point.value(),
            )
        )
        calculator.body.addWidget(calculate)
        layout.addWidget(calculator)
        tdc = Card("TDC / TDCR", "计算只给出建议，不会隐式写入 Orin 控制器。")
        tdc_form = FormSection()
        self.tdc_interface = QComboBox()
        tdc_form.add_field("写入 CAN", self.tdc_interface)
        tdc.body.addWidget(tdc_form)
        calculate_tdc = QPushButton("计算 TDC 建议")
        calculate_tdc.clicked.connect(
            lambda: self.state.request(
                "diagnostics.tdc_calculate",
                clock_mhz=self.clock_mhz.value(),
                sample_point=self.sample_point.value(),
            )
        )
        apply_tdc = QPushButton("显式写入并读回")
        apply_tdc.clicked.connect(self._apply_tdc)
        tdc.body.addWidget(calculate_tdc)
        tdc.body.addWidget(apply_tdc)
        tdc.body.addStretch(1)
        layout.addWidget(tdc)
        return tab

    def _rebuild_interfaces(self) -> None:
        signature = (
            self.state.evt.variant,
            tuple((name, item.mode.value) for name, item in self.state.evt.interfaces.items()),
            tuple((node.logic_id, node.bus) for node in self.state.evt.nodes),
        )
        if signature == self._topology_signature:
            return
        self._topology_signature = signature
        clear_layout(self.interface_checks)
        for name, interface in self.state.evt.interfaces.items():
            button = QPushButton(f"{name.upper()} · {'FD' if interface.mode.value == 'fd' else 'Classic'}")
            button.setCheckable(True)
            button.setChecked(True)
            button.setProperty("choice", True)
            button.setProperty("interface", name)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.interface_checks.addWidget(button)
        self.interface_checks.addStretch(1)
        self._fill_interface_combo(self.node_interface, nodes_only=True)
        self._fill_interface_combo(self.tdc_interface, fd_only=True)
        self._rebuild_node_table()

    def _fill_interface_combo(
        self, combo: QComboBox, *, nodes_only: bool = False, fd_only: bool = False
    ) -> None:
        previous = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for name, interface in self.state.evt.interfaces.items():
            node_count = len(self.state.evt.nodes_for_bus(name))
            if nodes_only and not node_count:
                continue
            if fd_only and interface.mode.value != "fd":
                continue
            mode = "CAN FD" if interface.mode.value == "fd" else "Classic CAN"
            combo.addItem(f"{name.upper()} · {mode} · {node_count} 节点", name)
        index = combo.findData(previous)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _rebuild_node_table(self) -> None:
        if not hasattr(self, "node_table"):
            return
        interface = self.node_interface.currentData()
        nodes = self.state.evt.nodes_for_bus(str(interface)) if interface else ()
        self.node_table.setRowCount(0)
        for node in nodes:
            row = self.node_table.rowCount()
            self.node_table.insertRow(row)
            selected = QCheckBox()
            selected.setProperty("logic_id", node.logic_id)
            self.node_table.setCellWidget(row, 0, selected)
            for column, value in enumerate(
                (node.logic_id, node.label, f"0x{node.dev_id:02X}", node.bus.upper(), "未测试"), 1
            ):
                item = QTableWidgetItem(str(value))
                if column in {1, 3, 4}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.node_table.setItem(row, column, item)
        self.node_count.setText(f"{len(nodes)} 个节点")
        self.broadcast.setText(f"监听 {str(interface).upper()} 广播应答" if interface else "监听广播应答")

    def _set_all_nodes(self, checked: bool) -> None:
        for row in range(self.node_table.rowCount()):
            widget = self.node_table.cellWidget(row, 0)
            if isinstance(widget, QCheckBox):
                widget.setChecked(checked)

    def _selected_interfaces(self) -> list[str]:
        result = []
        for index in range(self.interface_checks.count()):
            widget = self.interface_checks.itemAt(index).widget()
            if isinstance(widget, QPushButton) and widget.isChecked():
                result.append(str(widget.property("interface")))
        return result

    def _selected_nodes(self) -> list[int]:
        return [
            int(self.node_table.item(row, 1).text())
            for row in range(self.node_table.rowCount())
            if isinstance(self.node_table.cellWidget(row, 0), QCheckBox)
            and self.node_table.cellWidget(row, 0).isChecked()
        ]

    def _read_nodes(self) -> None:
        nodes = self._selected_nodes()
        if not nodes:
            QMessageBox.warning(self, "未选择节点", "请先选择至少一个需要读取参数的节点。")
            return
        self.state.request("diagnostics.node_param_read", logic_ids=nodes)

    def _run_broadcast(self) -> None:
        interface = self.node_interface.currentData()
        if not interface:
            QMessageBox.warning(self, "未选择 CAN", "请选择需要监听广播应答的 CAN 通道。")
            return
        for row in range(self.node_table.rowCount()):
            self.node_table.item(row, 5).setText("测试中…")
        self.state.request("diagnostics.broadcast", interface=str(interface))

    def _start_stress(self) -> None:
        if self.state.link_state is not LinkState.CONNECTED:
            QMessageBox.warning(self, "设备未连接", "诊断需要先连接 Orin。")
            return
        interfaces = self._selected_interfaces()
        if not interfaces:
            QMessageBox.warning(self, "未选择接口", "至少选择一个 CAN 接口。")
            return
        if (
            self.clear_dmesg.isChecked()
            and QMessageBox.warning(
                self,
                "清空 dmesg",
                "此操作会删除设备内核现场证据。确认先采集完整 dmesg 后再清空？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.state.request(
            "diagnostics.stress_start",
            interfaces=interfaces,
            profile=self.profile.currentData(),
            duration_s=self.duration.value(),
            gap_ms=self.packet_gap.value(),
            capture_dmesg=self.capture_dmesg.isChecked(),
            capture_candump=self.capture_candump.isChecked(),
            clear_dmesg=self.clear_dmesg.isChecked(),
        )
        self.diag_start.setEnabled(False)
        self.diag_stop.setEnabled(True)

    def _stop_stress(self) -> None:
        self.state.request("diagnostics.stress_cancel")
        self.diag_start.setEnabled(True)
        self.diag_stop.setEnabled(False)

    def _configure_forwarding(self) -> None:
        if (
            QMessageBox.question(self, "配置网络转发", "将修改 RK3588/Orin 路由和 iptables。确认继续？")
            == QMessageBox.StandardButton.Yes
        ):
            self.state.request("diagnostics.network_forward_configure")

    def _write_nodes(self) -> None:
        nodes = self._selected_nodes()
        if not nodes:
            QMessageBox.warning(self, "未选择节点", "请先选择至少一个需要写入并回读的节点。")
            return
        if (
            QMessageBox.question(self, "写入节点参数", f"确认写入并回读 {len(nodes)} 个节点？")
            == QMessageBox.StandardButton.Yes
        ):
            self.state.request("diagnostics.node_param_write", logic_ids=nodes)

    def _apply_tdc(self) -> None:
        if (
            QMessageBox.question(self, "写入 TDC", "仅 Orin mttcan 支持该操作。确认按计算结果写入并读回？")
            == QMessageBox.StandardButton.Yes
        ):
            self.state.request(
                "diagnostics.tdc_apply",
                interface=str(self.tdc_interface.currentData()),
                clock_mhz=self.clock_mhz.value(),
                sample_point=self.sample_point.value(),
            )

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if not action.startswith("diagnostics."):
            return
        if action == "diagnostics.broadcast":
            running = event == "started"
            if event in {"started", "succeeded", "failed", "cancelled"}:
                self.node_interface.setEnabled(not running)
                self.broadcast.setEnabled(not running)
        if event == "progress" and isinstance(payload, dict):
            self.diag_progress.setValue(int(payload.get("progress", 0)))
            message = str(payload.get("message", ""))
            if message:
                self.diag_console.appendPlainText(message)
        elif event == "failed":
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            self.diag_console.appendPlainText(f"[失败] {action}: {error}")
        elif event == "cancelled":
            self.diag_console.appendPlainText(f"[停止] {action} 已取消，远程进程组已清理")
        elif event == "succeeded":
            if action == "diagnostics.stress_start" and isinstance(payload, dict):
                bundle = payload.get("bundle", {})
                report_path = bundle.get("report_html", "") if isinstance(bundle, dict) else ""
                self.diag_console.appendPlainText(f"[完成] 报告：{report_path}")
                self.diag_progress.setValue(100)
            elif action in {"diagnostics.timing_calculate", "diagnostics.tdc_calculate"}:
                self.diag_console.appendPlainText(f"[计算结果] {payload}")
            elif action in {"diagnostics.node_param_read", "diagnostics.node_param_write"} and isinstance(
                payload, dict
            ):
                verdicts = {
                    int(item["logic_id"]): str(item.get("verdict", "完成"))
                    for item in payload.get("nodes", [])
                    if isinstance(item, dict) and "logic_id" in item
                }
                for row in range(self.node_table.rowCount()):
                    logic_id = int(self.node_table.item(row, 1).text())
                    if logic_id in verdicts:
                        self.node_table.item(row, 5).setText(verdicts[logic_id])
                self.diag_console.appendPlainText(f"[完成] {action}")
            elif action == "diagnostics.broadcast" and isinstance(payload, dict):
                responses = {int(value) for value in payload.get("responses", {})}
                for row in range(self.node_table.rowCount()):
                    logic_id = int(self.node_table.item(row, 1).text())
                    self.node_table.item(row, 5).setText("已应答" if logic_id in responses else "无应答")
                self.diag_console.appendPlainText(
                    f"[广播] {str(payload.get('interface', '')).upper()} · "
                    f"{len(responses)}/{self.node_table.rowCount()} 节点应答"
                )
            else:
                self.diag_console.appendPlainText(f"[完成] {action}")
        if action == "diagnostics.stress_start" and event in {"succeeded", "failed", "cancelled"}:
            self.diag_start.setEnabled(True)
            self.diag_stop.setEnabled(False)
