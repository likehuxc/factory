from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
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
from d7_factory_studio.core.evt import (
    CAN_REGION_LABELS,
    CAN_REGIONS,
    EvtConfigError,
    evt_can_mapping,
    load_builtin_evt,
    remap_evt_can,
)
from d7_factory_studio.settings_store import SettingsStore
from d7_factory_studio.ui.controls import D7ComboBox as QComboBox
from d7_factory_studio.ui.controls import D7SpinBox as QSpinBox
from d7_factory_studio.ui.pages.base import InlineMessage, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, PageHeader


class SettingsPage(WorkbenchPage):
    def __init__(self, state: ApplicationState, settings: SettingsStore) -> None:
        super().__init__()
        self.state = state
        self.settings = settings
        self._load_saved_can_mappings()
        self.layout.addWidget(
            PageHeader("设置", "管理 PC CAN、Orin SSH、文件路径和高风险操作；切换关键配置会恢复安全锁。")
        )
        tabs = QTabWidget()
        tabs.addTab(self._pc_tab(), "PC CAN")
        tabs.addTab(self._can_mapping_tab(), "CAN 映射")
        tabs.addTab(self._ssh_tab(), "Orin SSH")
        tabs.addTab(self._diagnostics_tab(), "网络诊断")
        tabs.addTab(self._paths_tab(), "文件与报告")
        tabs.addTab(self._safety_tab(), "安全")
        tabs.addTab(self._about_tab(), "关于")
        self.layout.addWidget(tabs)
        state.task_event.connect(self._on_task_event)

    def _pc_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card("USBCANFD-200U")
        form = QFormLayout()
        self.device_index = QSpinBox()
        self.device_index.setRange(0, 15)
        self.device_index.setValue(int(self.settings.value("zlg/device_index", 0)))
        self.channel_index = QSpinBox()
        self.channel_index.setRange(0, 7)
        self.channel_index.setValue(int(self.settings.value("zlg/channel", 0)))
        self.dll_path = QLineEdit(str(self.settings.value("zlg/dll_path", "自动扫描 64 位 ControlCANFD.dll")))
        self.dll_path.setReadOnly(True)
        form.addRow("设备类型", QLabel("41 · USBCANFD-200U"))
        form.addRow("设备序号", self.device_index)
        form.addRow("通道", self.channel_index)
        form.addRow("驱动库", self.dll_path)
        card.body.addLayout(form)
        scan = QPushButton("重新扫描并验证 DLL")
        scan.clicked.connect(lambda: self.state.request("settings.zlg_scan"))
        save = QPushButton("保存 PC CAN 设置")
        save.setProperty("primary", True)
        save.clicked.connect(self._save_pc)
        actions = QHBoxLayout()
        actions.addWidget(scan)
        actions.addStretch(1)
        actions.addWidget(save)
        card.body.addLayout(actions)
        layout.addWidget(card)
        layout.addStretch(1)
        return tab

    def _can_mapping_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.addWidget(
            InlineMessage(
                "四个电机区域可分别选择 Orin CAN 通道。保存并应用后会断开当前连接并恢复安全锁。",
                "info",
            )
        )
        card = Card("D7 电机 CAN 映射", "EVT1 与 EVT2 分别保存；升级 CAN 保持使用预设专用通道。")
        form = QFormLayout()
        self.mapping_profile = QComboBox()
        self.mapping_profile.addItems(["EVT2", "EVT1"])
        self.mapping_profile.currentTextChanged.connect(self._load_mapping_profile)
        form.addRow("配置方案", self.mapping_profile)
        self.region_channels: dict[str, QComboBox] = {}
        for region in CAN_REGIONS:
            combo = QComboBox()
            self.region_channels[region] = combo
            form.addRow(CAN_REGION_LABELS[region], combo)
        self.ota_channel = QLabel("—")
        self.ota_channel.setObjectName("Mono")
        form.addRow("升级专用通道", self.ota_channel)
        card.body.addLayout(form)
        actions = QHBoxLayout()
        restore = QPushButton("恢复当前 EVT 预设")
        restore.clicked.connect(self._restore_mapping_preset)
        save = QPushButton("保存并应用映射")
        save.setProperty("primary", True)
        save.clicked.connect(self._save_can_mapping)
        actions.addWidget(restore)
        actions.addStretch(1)
        actions.addWidget(save)
        card.body.addLayout(actions)
        layout.addWidget(card)
        layout.addStretch(1)
        self.mapping_profile.setCurrentText(self.state.evt.variant)
        self._load_mapping_profile(self.mapping_profile.currentText())
        return tab

    def _ssh_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card("Orin 连接")
        form = QFormLayout()
        self.host = QLineEdit(str(self.settings.value("ssh/host", "192.168.1.100")))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(self.settings.value("ssh/port", 22)))
        self.username = QLineEdit(str(self.settings.value("ssh/username", "pudu")))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        saved_password = self.settings.ssh_password(self.host.text(), self.username.text())
        if saved_password:
            self.password.setText(saved_password)
        self.known_host = QLineEdit(str(self.settings.value("ssh/fingerprint", "")))
        self.known_host.setPlaceholderText("首次连接后确认并保存主机指纹")
        self.agent_binary = QLineEdit(str(self.settings.value("ssh/agent_binary", "")))
        self.agent_binary.setPlaceholderText("高级覆盖：默认使用软件内置 Agent")
        self.agent_config = QLineEdit(str(self.settings.value("ssh/agent_config", "")))
        self.agent_config.setPlaceholderText("高级覆盖：留空时根据当前 EVT 自动生成")
        self.agent_library_dir = QLineEdit(
            str(self.settings.value("ssh/agent_library_dir", ""))
        )
        self.agent_library_dir.setPlaceholderText("高级覆盖：默认使用 Agent 配套依赖")
        form.addRow("主机", self.host)
        form.addRow("端口", self.port)
        form.addRow("用户名", self.username)
        form.addRow("密码", self.password)
        form.addRow("主机指纹", self.known_host)
        form.addRow("自定义 Agent", self._file_picker(self.agent_binary, "选择 ARM64 agent"))
        form.addRow("自定义 YAML", self._file_picker(self.agent_config, "选择 D7 agent 配置"))
        form.addRow("自定义依赖库", self._dir_picker(self.agent_library_dir))
        card.body.addLayout(form)
        actions = QHBoxLayout()
        test = QPushButton("测试连接")
        test.clicked.connect(self._test_ssh)
        save = QPushButton("保存到凭据管理器")
        save.setProperty("primary", True)
        save.clicked.connect(self._save_ssh)
        actions.addWidget(test)
        actions.addStretch(1)
        actions.addWidget(save)
        card.body.addLayout(actions)
        layout.addWidget(card)
        layout.addStretch(1)
        return tab

    def _paths_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card("本地目录")
        self.reports = QLineEdit(str(self.settings.report_directory))
        self.downloads = QLineEdit(
            str(self.settings.value("paths/downloads", Path.home() / "Downloads" / "D7-logs"))
        )
        for label, editor in (
            ("报告目录", self.reports),
            ("设备日志目录", self.downloads),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(editor, 1)
            choose = QPushButton("选择")
            choose.clicked.connect(lambda _checked=False, field=editor: self._choose_dir(field))
            row.addWidget(choose)
            card.body.addLayout(row)
        save = QPushButton("保存目录")
        save.setProperty("primary", True)
        save.clicked.connect(self._save_paths)
        card.body.addWidget(save)
        layout.addWidget(card)
        layout.addStretch(1)
        return tab

    def _diagnostics_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card(
            "ADB 网络转发",
            "仅在诊断页明确确认后使用；会修改 RK3588 路由/iptables 与 Orin 默认路由。",
        )
        form = QFormLayout()
        self.adb_path = QLineEdit(str(self.settings.value("diagnostics/adb_path", "")))
        self.adb_serial = QLineEdit(str(self.settings.value("diagnostics/adb_serial", "")))
        self.rk_wlan = QLineEdit(str(self.settings.value("diagnostics/rk_wlan", "wlan0")))
        self.rk_ethernet = QLineEdit(str(self.settings.value("diagnostics/rk_ethernet", "eth0")))
        self.orin_ethernet = QLineEdit(
            str(self.settings.value("diagnostics/orin_ethernet", "eth0"))
        )
        self.orin_ip = QLineEdit(str(self.settings.value("diagnostics/orin_ip", "10.254.254.1")))
        self.forward_port = QSpinBox()
        self.forward_port.setRange(1, 65535)
        self.forward_port.setValue(int(self.settings.value("diagnostics/forward_port", 22)))
        form.addRow("ADB", self._file_picker(self.adb_path, "选择 adb.exe"))
        form.addRow("ADB serial", self.adb_serial)
        form.addRow("RK Wi-Fi 接口", self.rk_wlan)
        form.addRow("RK 有线接口", self.rk_ethernet)
        form.addRow("Orin 有线接口", self.orin_ethernet)
        form.addRow("Orin IP", self.orin_ip)
        form.addRow("转发端口", self.forward_port)
        card.body.addLayout(form)
        save = QPushButton("保存网络诊断设置")
        save.setProperty("primary", True)
        save.clicked.connect(self._save_diagnostics)
        card.body.addWidget(save)
        layout.addWidget(card)
        layout.addStretch(1)
        return tab

    def _safety_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.addWidget(
            InlineMessage(
                "安全策略不能被永久关闭：每次连接、故障、掉线、通道或 EVT 切换都会重新锁定。", "warning"
            )
        )
        card = Card("高风险操作")
        self.allow_dmesg_clear = QCheckBox("允许诊断页显示 dmesg -C 操作（执行时仍需二次确认）")
        self.allow_raw_can = QCheckBox("允许整机页发送原始 CAN 帧（执行时仍需二次确认）")
        self.allow_dmesg_clear.setChecked(
            self.settings.bool_value("safety/allow_dmesg_clear")
        )
        self.allow_raw_can.setChecked(self.settings.bool_value("safety/allow_raw_can"))
        card.body.addWidget(self.allow_dmesg_clear)
        card.body.addWidget(self.allow_raw_can)
        save = QPushButton("保存安全选项")
        save.clicked.connect(self._save_safety)
        card.body.addWidget(save)
        layout.addWidget(card)
        layout.addStretch(1)
        return tab

    def _about_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card("D7 Factory Studio", "D7 生产、调试与售后一体化工作台")
        card.body.addWidget(QLabel("版本 0.1.0"))
        card.body.addWidget(QLabel("Python 3.12–3.14 x64 · PySide6 · Qt Widgets"))
        card.body.addWidget(QLabel("仅支持 D7；不加载 D5/D9/D5W 配置。"))
        layout.addWidget(card)
        return tab

    def _save_pc(self) -> None:
        self.settings.set_value("zlg/device_index", self.device_index.value())
        self.settings.set_value("zlg/channel", self.channel_index.value())
        self.state.lock("PC CAN 设置已更新，安全锁已恢复")

    def _load_saved_can_mappings(self) -> None:
        for variant in ("EVT1", "EVT2"):
            defaults = evt_can_mapping(load_builtin_evt(variant))
            mapping = {
                region: str(
                    self.settings.value(f"can_mapping/{variant}/{region}", defaults[region])
                ).lower()
                for region in CAN_REGIONS
            }
            try:
                remap_evt_can(load_builtin_evt(variant), mapping)
            except EvtConfigError:
                mapping = defaults
            self.state.register_evt_can_mapping(variant, mapping)

    def _load_mapping_profile(self, variant: str) -> None:
        if not hasattr(self, "region_channels") or not variant:
            return
        config = load_builtin_evt(variant)
        defaults = evt_can_mapping(config)
        ota_bus = next(item.name for item in config.interfaces.values() if item.role == "ota")
        self.ota_channel.setText(ota_bus.upper())
        available = [f"can{index}" for index in range(8) if f"can{index}" != ota_bus]
        for region, combo in self.region_channels.items():
            saved = str(
                self.settings.value(f"can_mapping/{variant}/{region}", defaults[region])
            ).lower()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems([bus.upper() for bus in available])
            combo.setCurrentText(saved.upper() if saved in available else defaults[region].upper())
            combo.blockSignals(False)

    def _restore_mapping_preset(self) -> None:
        variant = self.mapping_profile.currentText()
        defaults = evt_can_mapping(load_builtin_evt(variant))
        for region, combo in self.region_channels.items():
            combo.setCurrentText(defaults[region].upper())

    def _save_can_mapping(self) -> None:
        variant = self.mapping_profile.currentText()
        mapping = {
            region: combo.currentText().lower() for region, combo in self.region_channels.items()
        }
        try:
            remap_evt_can(load_builtin_evt(variant), mapping)
        except EvtConfigError as exc:
            QMessageBox.warning(self, "CAN 映射无效", str(exc))
            return
        for region, bus in mapping.items():
            self.settings.set_value(f"can_mapping/{variant}/{region}", bus)
        self.state.register_evt_can_mapping(variant, mapping)
        QMessageBox.information(self, "CAN 映射已保存", f"{variant} 的四个电机区域通道已保存。")

    def _test_ssh(self) -> None:
        self.state.request(
            "settings.ssh_test",
            host=self.host.text(),
            port=self.port.value(),
            username=self.username.text(),
            password=self.password.text(),
            fingerprint=self.known_host.text(),
        )

    def _save_ssh(self) -> None:
        if not self.host.text().strip() or not self.username.text().strip():
            QMessageBox.warning(self, "SSH 设置无效", "主机和用户名不能为空。")
            return
        self.settings.set_value("ssh/host", self.host.text().strip())
        self.settings.set_value("ssh/port", self.port.value())
        self.settings.set_value("ssh/username", self.username.text().strip())
        self.settings.set_value("ssh/fingerprint", self.known_host.text().strip())
        self.settings.set_value("ssh/agent_binary", self.agent_binary.text().strip())
        self.settings.set_value("ssh/agent_config", self.agent_config.text().strip())
        self.settings.set_value("ssh/agent_library_dir", self.agent_library_dir.text().strip())
        if self.password.text():
            try:
                self.settings.set_ssh_password(
                    self.host.text().strip(), self.username.text().strip(), self.password.text()
                )
            except Exception as exc:  # keyring surfaces backend-specific failures
                QMessageBox.critical(self, "凭据保存失败", str(exc))
                return
        self.state.lock("SSH 设置已更新，安全锁已恢复")

    def _choose_dir(self, editor: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择目录", editor.text())
        if path:
            editor.setText(path)

    def _file_picker(self, editor: QLineEdit, caption: str) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        choose = QPushButton("选择")
        choose.clicked.connect(
            lambda: self._choose_file(editor, caption)
        )
        layout.addWidget(editor, 1)
        layout.addWidget(choose)
        return container

    def _dir_picker(self, editor: QLineEdit) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        choose = QPushButton("选择")
        choose.clicked.connect(lambda: self._choose_dir(editor))
        layout.addWidget(editor, 1)
        layout.addWidget(choose)
        return container

    def _choose_file(self, editor: QLineEdit, caption: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, caption, editor.text())
        if path:
            editor.setText(path)

    def _save_paths(self) -> None:
        self.settings.set_value("paths/reports", self.reports.text())
        self.settings.set_value("paths/downloads", self.downloads.text())

    def _save_diagnostics(self) -> None:
        values = {
            "diagnostics/adb_path": self.adb_path.text().strip(),
            "diagnostics/adb_serial": self.adb_serial.text().strip(),
            "diagnostics/rk_wlan": self.rk_wlan.text().strip(),
            "diagnostics/rk_ethernet": self.rk_ethernet.text().strip(),
            "diagnostics/orin_ethernet": self.orin_ethernet.text().strip(),
            "diagnostics/orin_ip": self.orin_ip.text().strip(),
            "diagnostics/forward_port": self.forward_port.value(),
        }
        for key, value in values.items():
            self.settings.set_value(key, value)
        self.state.lock("网络诊断设置已更新，安全锁已恢复")

    def _save_safety(self) -> None:
        self.settings.set_value("safety/allow_dmesg_clear", self.allow_dmesg_clear.isChecked())
        self.settings.set_value("safety/allow_raw_can", self.allow_raw_can.isChecked())

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action == "settings.zlg_scan" and event == "succeeded":
            self.dll_path.setText(str(payload))
            QMessageBox.information(self, "驱动验证通过", f"已选择 64 位 ControlCANFD.dll：\n{payload}")
        elif action == "settings.ssh_test" and event == "succeeded" and isinstance(payload, dict):
            if payload.get("requires_confirmation") == "true":
                fingerprint = str(payload.get("fingerprint", ""))
                if (
                    QMessageBox.question(
                        self,
                        "确认 SSH 主机指纹",
                        f"首次连接 {payload.get('host', '')}。请与设备侧核对后确认：\n\n{fingerprint}\n\n"
                        "确认后仍需保存 SSH 设置并重新测试。",
                    )
                    == QMessageBox.StandardButton.Yes
                ):
                    self.known_host.setText(fingerprint)
            else:
                QMessageBox.information(
                    self, "SSH 连接成功", f"远端主机：{payload.get('hostname', '')}"
                )
        elif action.startswith("settings.") and event == "failed":
            error = payload.get("error", "未知错误") if isinstance(payload, dict) else "未知错误"
            QMessageBox.critical(self, "设置检查失败", str(error))
