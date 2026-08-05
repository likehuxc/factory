from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import LinkState
from d7_factory_studio.ui.pages.base import FormSection, InlineMessage, LogConsole, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, Metric, PageHeader


def add_motor_targets(combo: QComboBox, state: ApplicationState, include_groups: bool = True) -> None:
    combo.clear()
    if include_groups:
        for group, ids in state.evt.fixed_groups.items():
            combo.addItem(f"分组 · {group.replace('_', ' ')} ({len(ids)})", ("group", group))
        combo.insertSeparator(combo.count())
    for node in state.evt.nodes:
        combo.addItem(
            f"{node.logic_id:02d} · {node.label} · {node.bus.upper()} / 0x{node.dev_id:02X}",
            ("motor", node.logic_id),
        )


def require_motion_ready(widget: QWidget, state: ApplicationState) -> bool:
    if state.link_state is not LinkState.CONNECTED:
        QMessageBox.warning(widget, "设备未连接", "请先在顶部状态轨连接设备。")
        return False
    if state.safety_locked:
        QMessageBox.warning(widget, "运动已锁定", "请确认现场安全后，在顶部状态轨解除本次会话的安全锁。")
        return False
    return True


class NodeOverviewPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(
            PageHeader("电机节点总览", "按当前 EVT 拓扑查看 30 个 D7 节点、总线归属与实时状态。")
        )
        metrics_card = Card()
        metrics = QHBoxLayout()
        self.total = Metric("配置节点", "30")
        self.online = Metric("在线", "0", "#21875A")
        self.faults = Metric("故障", "0", "#D94747")
        self.unlocked = Metric("运动权限", "已锁定", "#C67A16")
        for item in (self.total, self.online, self.faults, self.unlocked):
            metrics.addWidget(item)
        metrics.addStretch(1)
        metrics_card.body.addLayout(metrics)
        self.layout.addWidget(metrics_card)

        table_card = Card("节点目录", "状态列由 PC CAN 或 Orin agent 的状态流更新。")
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["逻辑 ID", "节点", "设备 ID", "总线", "分组", "模式", "状态", "故障"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table_card.body.addWidget(self.table)
        self.layout.addWidget(table_card)
        state.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self.total.set_value(str(len(self.state.evt.nodes)))
        self.online.set_value(str(self.state.online_nodes))
        self.unlocked.set_value("已锁定" if self.state.safety_locked else "已解锁")
        self.table.setRowCount(0)
        for node in self.state.evt.nodes:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                str(node.logic_id),
                node.label,
                f"0x{node.dev_id:02X}",
                node.bus.upper(),
                node.group.replace("_", " "),
                "—",
                "未连接",
                "—",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {0, 2, 3}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)


class CanMotionPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(
            PageHeader("CAN 运动控制", "统一控制单电机或固定分组；速度台架上限固定为 ±0.5 rad/s。")
        )
        self.layout.addWidget(
            InlineMessage("模式切换会先失能并读回校验；切换完成后保持失能，需要重新使能。", "warning")
        )

        row = QHBoxLayout()
        target_card = Card("控制目标")
        target_form = FormSection()
        self.target = QComboBox()
        add_motor_targets(self.target, state)
        self.mode = QComboBox()
        self.mode.addItem("位置模式", "position")
        self.mode.addItem("速度模式", "velocity")
        target_form.add_field("电机或固定分组", self.target)
        target_form.add_field("工作模式", self.mode)
        target_card.body.addWidget(target_form)
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
        self.velocity.setRange(-0.5, 0.5)
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

    def _evt_changed(self) -> None:
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
        self.state.request(f"motor.{operation}", target=self._target_payload())
        self.state.log("电机", f"已请求{operation}: {self.target.currentText()}")

    def _set_mode(self) -> None:
        if not require_motion_ready(self, self.state):
            return
        self.state.request("motor.set_mode", target=self._target_payload(), mode=self.mode.currentData())
        self.state.lock("模式切换后已恢复安全锁")

    def _execute(self) -> None:
        if not require_motion_ready(self, self.state):
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
        self.state.lock("紧急停止已触发，安全锁已恢复")


class Serial485Page(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
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
        scan = QPushButton("扫描 0–255")
        scan.setProperty("primary", True)
        scan.clicked.connect(lambda: self._request("scan"))
        actions.addWidget(refresh)
        actions.addStretch(1)
        actions.addWidget(scan)
        connection.body.addLayout(actions)
        row.addWidget(connection)

        identity = Card("电机身份")
        identity_form = FormSection()
        self.station_id = QSpinBox()
        self.station_id.setRange(0, 255)
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
        motion_form.addRow("速度", self.speed)
        motion_form.addRow("相对角度", self.relative)
        motion.body.addLayout(motion_form)
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
        self.layout.addWidget(motion)

        output = Card("485 日志")
        self.console = LogConsole()
        output.body.addWidget(self.console)
        self.layout.addWidget(output)
        self.refresh_ports()

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

    def _request(self, operation: str, motion: bool = False) -> None:
        if motion and not require_motion_ready(self, self.state):
            return
        if self.port.currentData() is None and self.port.currentText() == "未发现串口":
            QMessageBox.warning(self, "无可用串口", "请连接 485 转换器后刷新串口。")
            return
        self.state.request(
            f"serial485.{operation}",
            port=self.port.currentText(),
            baud=int(self.baud.currentText()),
            station_id=self.station_id.value(),
            new_id=self.new_id.value(),
            rad_s=self.speed.value(),
            angle_deg=self.relative.value(),
        )
        self.console.appendPlainText(f"[请求] {operation}")


class ParameterPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(
            PageHeader("电机参数", "按白名单读取和写入参数；保存到 Flash 与写入 RAM 分离。")
        )
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
        self.table.setHorizontalHeaderLabels(["参数", "当前值", "新值", "单位", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        rows = [
            ("position_kp", "—", "", "", "未读取"),
            ("velocity_kp", "—", "", "", "未读取"),
            ("current_limit", "—", "", "A", "未读取"),
            ("temperature_limit", "—", "", "°C", "未读取"),
            ("zero_offset", "—", "", "rad", "未读取"),
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

    def _payload(self) -> dict[str, object]:
        kind, value = self.target.currentData()
        values = {}
        for row in range(self.table.rowCount()):
            entered = self.table.item(row, 2).text().strip()
            if entered:
                values[self.table.item(row, 0).text()] = entered
        return {"target": {kind: value}, "values": values}

    def _request(self, operation: str) -> None:
        if self.state.link_state is not LinkState.CONNECTED:
            QMessageBox.warning(self, "设备未连接", "请先连接设备。")
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


class ZeroCalibrationPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(
            PageHeader("零位标定", "采用准备与提交两阶段流程，提交前电机必须失能且机械位置已确认。")
        )
        self.layout.addWidget(
            InlineMessage(
                "标定会改变电机零位。请卸载负载或可靠支撑机构，并让无关人员离开运动范围。", "danger"
            )
        )
        card = Card("标定目标")
        form = FormSection()
        self.target = QComboBox()
        add_motor_targets(self.target, state, include_groups=False)
        form.add_field("单电机", self.target)
        card.body.addWidget(form)
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
        self.layout.addWidget(card)

    def _target_payload(self) -> dict[str, int]:
        _kind, logic_id = self.target.currentData()
        return {"motor": int(logic_id)}

    def _prepare(self) -> None:
        if not require_motion_ready(self, self.state):
            return
        if not self.mechanical_confirm.isChecked() or not self.prepare_confirm.isChecked():
            QMessageBox.warning(self, "确认未完成", "请完成两项现场确认。")
            return
        self.state.request("motor.zero_prepare", target=self._target_payload())
        self.commit.setEnabled(True)
        self.state.log("零位", f"标定准备完成: {self.target.currentText()}", "warning")

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
        self.state.lock("零位标定完成，安全锁已恢复")


class LongTestPage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.state = state
        self.layout.addWidget(
            PageHeader("电机长测", "固定分组执行位置或速度循环；停止、异常和掉线都会触发零速与失能。")
        )
        card = Card("循环计划")
        form = FormSection()
        self.target = QComboBox()
        for group, ids in state.evt.fixed_groups.items():
            self.target.addItem(f"{group.replace('_', ' ')} ({len(ids)})", group)
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
        self.state.lock("长测已停止，安全锁已恢复")
        self.start.setEnabled(True)
        self.stop.setEnabled(False)
