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
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.settings_store import SettingsStore
from d7_factory_studio.ui.pages.base import InlineMessage, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, PageHeader


class SettingsPage(WorkbenchPage):
    def __init__(self, state: ApplicationState, settings: SettingsStore) -> None:
        super().__init__()
        self.state = state
        self.settings = settings
        self.layout.addWidget(
            PageHeader("设置", "管理 PC CAN、Orin SSH、文件路径和高风险操作；切换关键配置会恢复安全锁。")
        )
        tabs = QTabWidget()
        tabs.addTab(self._pc_tab(), "PC CAN")
        tabs.addTab(self._ssh_tab(), "Orin SSH")
        tabs.addTab(self._paths_tab(), "文件与报告")
        tabs.addTab(self._safety_tab(), "安全")
        tabs.addTab(self._about_tab(), "关于")
        self.layout.addWidget(tabs)

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
        form.addRow("主机", self.host)
        form.addRow("端口", self.port)
        form.addRow("用户名", self.username)
        form.addRow("密码", self.password)
        form.addRow("主机指纹", self.known_host)
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
            bool(
                self.settings.value(
                    "safety/allow_dmesg_clear",
                    False,
                )
            )
        )
        self.allow_raw_can.setChecked(bool(self.settings.value("safety/allow_raw_can", False)))
        card.body.addWidget(self.allow_dmesg_clear)
        card.body.addWidget(self.allow_raw_can)
        save = QPushButton("保存安全选项")
        save.clicked.connect(self._save_safety)
        card.body.addWidget(save)
        layout.addWidget(card)
        return tab

    def _about_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 12, 0, 0)
        card = Card("D7 Factory Studio", "D7 生产、调试与售后一体化工作台")
        card.body.addWidget(QLabel("版本 0.1.0"))
        card.body.addWidget(QLabel("Python 3.12 x64 · PySide6 · Qt Widgets"))
        card.body.addWidget(QLabel("仅支持 D7；不加载 D5/D9/D5W 配置。"))
        layout.addWidget(card)
        return tab

    def _save_pc(self) -> None:
        self.settings.set_value("zlg/device_index", self.device_index.value())
        self.settings.set_value("zlg/channel", self.channel_index.value())
        self.state.lock("PC CAN 设置已更新，安全锁已恢复")

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

    def _save_paths(self) -> None:
        self.settings.set_value("paths/reports", self.reports.text())
        self.settings.set_value("paths/downloads", self.downloads.text())

    def _save_safety(self) -> None:
        self.settings.set_value("safety/allow_dmesg_clear", self.allow_dmesg_clear.isChecked())
        self.settings.set_value("safety/allow_raw_can", self.allow_raw_can.isChecked())
