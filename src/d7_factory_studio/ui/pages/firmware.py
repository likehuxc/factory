from __future__ import annotations

from pathlib import Path

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
from d7_factory_studio.ui.pages.base import FormSection, InlineMessage, LogConsole, WorkbenchPage
from d7_factory_studio.ui.widgets import Card, PageHeader


class FirmwareTargetPanel(QWidget):
    def __init__(self, state: ApplicationState, target: str) -> None:
        super().__init__()
        self.state = state
        self.target = target
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(14)

        settings = Card("升级设置")
        form = FormSection()
        self.target_id = QLineEdit("0x18" if target == "pmu" else "0x42")
        self.target_id.setPlaceholderText("11 位 CAN ID，例如 0x42")
        self.iap_id = QLineEdit("0x7FF")
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
        form.add_field("目标 CAN ID", self.target_id)
        form.add_field("IAP CAN ID", self.iap_id)
        form.add_field("固件文件", file_row)
        settings.body.addWidget(form)
        if target == "battery":
            quick = QHBoxLayout()
            quick.addWidget(QLabel("快速选择"))
            for value in ("0x41", "0x42", "0x43"):
                button = QPushButton(value)
                button.clicked.connect(
                    lambda _checked=False, selected=value: self.target_id.setText(selected)
                )
                quick.addWidget(button)
            quick.addStretch(1)
            settings.body.addLayout(quick)
        layout.addWidget(settings)

        status = Card("升级任务")
        self.message = InlineMessage("选择固件后先执行校验；连接设备前不会发送任何 CAN 帧。")
        status.body.addWidget(self.message)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        status.body.addWidget(self.progress)
        actions = QHBoxLayout()
        validate = QPushButton("校验固件")
        validate.clicked.connect(self._validate)
        self.start = QPushButton("开始升级")
        self.start.setProperty("primary", True)
        self.start.clicked.connect(self._start)
        self.stop = QPushButton("停止")
        self.stop.setProperty("danger", True)
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self._stop)
        actions.addWidget(validate)
        actions.addStretch(1)
        actions.addWidget(self.stop)
        actions.addWidget(self.start)
        status.body.addLayout(actions)
        layout.addWidget(status)

        logs = Card("升级日志")
        self.console = LogConsole()
        logs.body.addWidget(self.console)
        layout.addWidget(logs)
        layout.addStretch(1)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 D7 固件", "", "固件文件 (*.bin *.hex);;所有文件 (*)"
        )
        if path:
            self.file_path.setText(path)
            self._validate()

    def _validate_can_id(self, text: str) -> int:
        value = int(text.strip(), 0)
        if not 0 <= value <= 0x7FF:
            raise ValueError("CAN ID 必须在 0x000–0x7FF")
        return value

    def _validate(self) -> bool:
        try:
            target_id = self._validate_can_id(self.target_id.text())
            iap_id = self._validate_can_id(self.iap_id.text())
            path = Path(self.file_path.text())
            if not path.is_file():
                raise ValueError("请先选择存在的固件文件")
            size = path.stat().st_size
            if size == 0:
                raise ValueError("固件文件为空")
        except (ValueError, OSError) as exc:
            self.message.set_text(str(exc))
            self.console.appendPlainText(f"[校验失败] {exc}")
            return False
        self.message.set_text(
            f"校验通过 · {path.name} · {size:,} bytes · 目标 0x{target_id:X} / IAP 0x{iap_id:X}"
        )
        self.console.appendPlainText(f"[校验通过] {path} ({size} bytes)")
        return True

    def _start(self) -> None:
        if not self._validate():
            return
        if self.state.link_state.value != "connected":
            QMessageBox.warning(self, "设备未连接", "请先在顶部状态轨连接设备。")
            return
        payload = {
            "target": self.target,
            "firmware": self.file_path.text(),
            "target_id": self._validate_can_id(self.target_id.text()),
            "iap_id": self._validate_can_id(self.iap_id.text()),
        }
        self.state.request("firmware.start", **payload)
        self.state.log("升级", f"已请求开始{'PMU' if self.target == 'pmu' else '电池'}升级")
        self.start.setEnabled(False)
        self.stop.setEnabled(True)

    def _stop(self) -> None:
        self.state.request("firmware.cancel", target=self.target)
        self.state.log("升级", "已请求停止升级", "warning")
        self.stop.setEnabled(False)
        self.start.setEnabled(True)


class FirmwarePage(WorkbenchPage):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__()
        self.layout.addWidget(
            PageHeader("固件升级", "统一升级 D7 PMU 与电池固件，保留校验、重试、停止和完整任务记录。")
        )
        tabs = QTabWidget()
        tabs.addTab(FirmwareTargetPanel(state, "pmu"), "PMU 升级")
        tabs.addTab(FirmwareTargetPanel(state, "battery"), "电池升级")
        self.layout.addWidget(tabs)
        self.layout.addStretch(1)
