from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import ConnectionMode, LinkState
from d7_factory_studio.settings_store import SettingsStore, ssh_fingerprint_key
from d7_factory_studio.ui.controls import D7ComboBox as QComboBox
from d7_factory_studio.ui.icons import lucide_icon
from d7_factory_studio.ui.theme import COLORS


KNOWN_RS485_USB_IDS = frozenset({(0x1A86, 0x55D3)})


def clear_layout(layout: QVBoxLayout | QHBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


class Card(QFrame):
    def __init__(self, title: str = "", caption: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 18)
        self.body.setSpacing(12)
        if title:
            title_label = QLabel(title)
            title_label.setObjectName("SectionTitle")
            self.body.addWidget(title_label)
        if caption:
            caption_label = QLabel(caption)
            caption_label.setObjectName("Muted")
            caption_label.setWordWrap(True)
            self.body.addWidget(caption_label)


class PageHeader(QWidget):
    def __init__(self, title: str, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(4)
        title_label = QLabel(title)
        title_label.setObjectName("PageTitle")
        caption_label = QLabel(caption)
        caption_label.setObjectName("PageCaption")
        caption_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(caption_label)


class Metric(QWidget):
    def __init__(
        self, label: str, value: str, accent: str = "#246BFD", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setMinimumWidth(130)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        label_widget = QLabel(label)
        label_widget.setObjectName("Muted")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("MetricValue")
        self.value_label.setStyleSheet(f"color: {accent};")
        layout.addWidget(label_widget)
        layout.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class PrimaryNavButton(QToolButton):
    def __init__(self, text: str, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.icon_name = icon_name
        self.setText(text)
        self.setCheckable(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setIconSize(self.iconSize().expandedTo(self.iconSize()))
        self.setFixedSize(76, 68)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggled.connect(self._update_appearance)
        self._update_appearance(False)

    def _update_appearance(self, checked: bool) -> None:
        color = "#FFFFFF" if checked else "#9DAEC4"
        self.setIcon(lucide_icon(self.icon_name, color, 21))
        background = COLORS["primary"] if checked else "transparent"
        hover = COLORS["primary"] if checked else COLORS["sidebar_hover"]
        self.setStyleSheet(
            f"QToolButton {{color:{color}; background:{background}; border:none; border-radius:10px; "
            "font-size:14px; font-weight:600; padding:5px 3px;}"
            f"QToolButton:hover {{background:{hover}; color:white;}}"
        )


class SecondaryNavButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(42)
        self.toggled.connect(self._update_appearance)
        self._update_appearance(False)

    def _update_appearance(self, checked: bool) -> None:
        if checked:
            self.setStyleSheet(
                f"QPushButton {{text-align:left; padding-left:14px; color:{COLORS['primary']}; "
                f"background:{COLORS['primary_soft']}; border:none; border-radius:8px; font-weight:700;}}"
            )
        else:
            self.setStyleSheet(
                f"QPushButton {{text-align:left; padding-left:14px; color:{COLORS['muted']}; "
                "background:transparent; border:none; border-radius:8px; font-weight:600;}"
                f"QPushButton:hover {{background:{COLORS['surface_muted']}; color:{COLORS['ink']};}}"
            )


class SegmentedControl(QWidget):
    changed = Signal(str)

    def __init__(self, items: Iterable[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background:{COLORS['surface_muted']}; border-radius:8px;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, (key, text) in enumerate(items):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setProperty("segmentKey", key)
            button.setMinimumHeight(34)
            button.setStyleSheet(
                f"QPushButton {{border:none; min-height:30px; padding:0 12px; color:{COLORS['muted']}; "
                "background:transparent; border-radius:6px;}"
                f"QPushButton:checked {{color:{COLORS['ink']}; background:white; font-weight:700;}}"
            )
            self.group.addButton(button)
            layout.addWidget(button)
            if index == 0:
                button.setChecked(True)
        self.group.buttonClicked.connect(self._clicked)

    def _clicked(self, button: QAbstractButton) -> None:
        self.changed.emit(str(button.property("segmentKey")))

    def set_value(self, key: str) -> None:
        for button in self.group.buttons():
            if button.property("segmentKey") == key:
                button.setChecked(True)
                return


class StatusPill(QLabel):
    def set_status(self, text: str, tone: str) -> None:
        colors = {
            "neutral": (COLORS["surface_muted"], COLORS["muted"]),
            "primary": (COLORS["primary_soft"], COLORS["primary"]),
            "success": ("#E7F6EF", COLORS["success"]),
            "warning": ("#FFF2DE", COLORS["warning"]),
            "danger": ("#FDECEC", COLORS["danger"]),
        }
        background, foreground = colors[tone]
        self.setText(text)
        self.setStyleSheet(
            f"color:{foreground}; background:{background}; border-radius:9px; "
            "padding:4px 9px; font-size:14px; font-weight:700;"
        )


class StatusLight(QWidget):
    """Compact status indicator for persistent workstation connections."""

    _COLORS = {
        "neutral": "#9BA8B8",
        "success": COLORS["success"],
        "warning": COLORS["warning"],
        "danger": COLORS["danger"],
    }

    def __init__(self, text: str = "未连接", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusLight")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.dot = QLabel()
        self.dot.setObjectName("StatusDot")
        self.dot.setFixedSize(12, 12)
        self.text_label = QLabel(text)
        self.text_label.setObjectName("StatusText")
        layout.addWidget(self.dot)
        layout.addWidget(self.text_label)
        self.set_status(text, "neutral")

    def set_status(self, text: str, tone: str) -> None:
        color = self._COLORS[tone]
        self.setProperty("statusTone", tone)
        self.dot.setStyleSheet(f"background:{color}; border:none; border-radius:6px;")
        self.text_label.setText(text)
        self.text_label.setStyleSheet(f"color:{color};")


class StatusRail(QWidget):
    def __init__(
        self,
        state: ApplicationState,
        settings: SettingsStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.settings = settings or SettingsStore()
        self._orin_action = ""
        self._pending_orin_host = ""
        self._serial_open = False
        self._serial_busy = False
        self._serial_fault = False
        self._serial_removed = False
        self._serial_open_port = ""
        self.setObjectName("TopRail")
        self.setFixedHeight(72)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        evt_label = QLabel("机型")
        evt_label.setObjectName("RailLabel")
        layout.addWidget(evt_label)
        self.evt_combo = QComboBox()
        self.evt_combo.addItems(["EVT2", "EVT1"])
        self.evt_combo.setToolTip("切换整机 CAN 拓扑；切换后自动断开")
        self.evt_combo.setFixedWidth(92)
        self.evt_combo.currentTextChanged.connect(self._evt_changed)
        layout.addWidget(self.evt_combo)
        layout.addStretch(1)

        self.network_forward_button = QPushButton()
        self.network_forward_button.setProperty("danger", True)
        self.network_forward_button.setIcon(lucide_icon("route", COLORS["danger"]))
        self.network_forward_button.setAccessibleName("网络转发")
        self.network_forward_button.setToolTip("配置 RK3588 与 Orin 的路由和 iptables")
        self.network_forward_button.setFixedWidth(44)
        self.network_forward_button.clicked.connect(self._configure_network_forwarding)
        layout.addWidget(self.network_forward_button)
        layout.addSpacing(8)

        self.orin_host = QComboBox()
        self.orin_host.setObjectName("OrinHost")
        self.orin_host.setEditable(True)
        self.orin_host.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.orin_host.lineEdit().setPlaceholderText("Orin IP")
        self.orin_host.setToolTip("Orin SSH 主机地址")
        self.orin_host.setFixedWidth(144)
        self._load_orin_hosts()
        layout.addWidget(self.orin_host)
        self.orin_status = StatusLight("Orin 未连接")
        self.orin_status.setFixedWidth(106)
        layout.addWidget(self.orin_status)
        self.orin_button = QPushButton("连接")
        self.orin_button.setObjectName("OrinButton")
        self.orin_button.setProperty("primary", True)
        self.orin_button.setFixedWidth(64)
        self.orin_button.clicked.connect(self._toggle_orin)
        layout.addWidget(self.orin_button)

        divider = QFrame()
        divider.setObjectName("RailDivider")
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFixedWidth(14)
        layout.addWidget(divider)

        self.serial_port = QComboBox()
        self.serial_port.setObjectName("SerialPort")
        self.serial_port.setEditable(True)
        self.serial_port.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.serial_port.setToolTip("485 转换器串口")
        self.serial_port.setFixedWidth(92)
        self._load_serial_ports()
        layout.addWidget(self.serial_port)
        self.serial_scan_button = QPushButton()
        self.serial_scan_button.setObjectName("SerialScanButton")
        self.serial_scan_button.setIcon(lucide_icon("refresh-cw", COLORS["muted"]))
        self.serial_scan_button.setAccessibleName("重新扫描串口")
        self.serial_scan_button.setToolTip("重新扫描 485 串口")
        self.serial_scan_button.setFixedWidth(42)
        self.serial_scan_button.clicked.connect(self._scan_serial_ports)
        layout.addWidget(self.serial_scan_button)
        self.serial_status = StatusLight("485 未打开")
        self.serial_status.setFixedWidth(96)
        layout.addWidget(self.serial_status)
        self.serial_button = QPushButton("打开")
        self.serial_button.setObjectName("SerialButton")
        self.serial_button.setFixedWidth(64)
        self.serial_button.clicked.connect(self._toggle_serial)
        layout.addWidget(self.serial_button)

        state.changed.connect(self.refresh)
        state.task_event.connect(self._on_task_event)
        self._serial_presence_timer = QTimer(self)
        self._serial_presence_timer.setInterval(1000)
        self._serial_presence_timer.timeout.connect(self._poll_serial_presence)
        self._serial_presence_timer.start()
        self.refresh()

    def _evt_changed(self, value: str) -> None:
        if value:
            self.state.set_evt(value)

    def _load_orin_hosts(self) -> None:
        current = str(self.settings.value("ssh/host", "192.168.1.100")).strip()
        stored = self.settings.value("ssh/host_history", [])
        if isinstance(stored, str):
            history = [stored]
        elif isinstance(stored, (list, tuple)):
            history = [str(item).strip() for item in stored]
        else:
            history = []
        hosts = list(dict.fromkeys(item for item in (current, *history) if item))
        self.orin_host.addItems(hosts)
        self.orin_host.setCurrentText(current)

    def _remember_orin_host(self, host: str) -> None:
        history = [
            self.orin_host.itemText(index).strip()
            for index in range(self.orin_host.count())
            if self.orin_host.itemText(index).strip() != host
        ]
        hosts = [host, *history][:8]
        self.settings.set_value("ssh/host", host)
        self.settings.set_value("ssh/host_history", hosts)
        self.orin_host.blockSignals(True)
        self.orin_host.clear()
        self.orin_host.addItems(hosts)
        self.orin_host.setCurrentText(host)
        self.orin_host.blockSignals(False)

    def _configure_network_forwarding(self) -> None:
        answer = QMessageBox.question(
            self,
            "配置网络转发",
            "此操作将修改 RK3588/Orin 的路由和 iptables 规则。\n确认继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.state.request("diagnostics.network_forward_configure")

    def _toggle_orin(self) -> None:
        orin_connected = (
            self.state.connection_mode is ConnectionMode.ORIN_REMOTE
            and self.state.link_state is LinkState.CONNECTED
        )
        if self.state.link_state is LinkState.CONNECTING or self._orin_action:
            return
        if orin_connected:
            self._orin_action = "disconnect"
            self.state.request("connection.disconnect")
            self.refresh()
            return
        host = self.orin_host.currentText().strip()
        if not host:
            QMessageBox.warning(self, "Orin 地址为空", "请输入 Orin IP 后再连接。")
            return
        self._remember_orin_host(host)
        if self.state.link_state is LinkState.CONNECTED:
            self._pending_orin_host = host
            self._orin_action = "switch"
            self.state.request("connection.disconnect")
            self.refresh()
            return
        self._begin_orin_connect(host)

    def _begin_orin_connect(self, host: str) -> None:
        if self.state.link_state is LinkState.FAULT:
            self.state.set_link_state(LinkState.DISCONNECTED)
        self.state.set_connection_mode(ConnectionMode.ORIN_REMOTE)
        self.state.set_link_state(LinkState.CONNECTING)
        self.state.request(
            "connection.connect",
            mode=ConnectionMode.ORIN_REMOTE.value,
            host=host,
            evt=self.state.evt.variant,
        )

    @staticmethod
    def _serial_port_sort_key(port: str) -> tuple[int, int, str]:
        normalized = port.strip().upper()
        if normalized.startswith("COM") and normalized[3:].isdigit():
            return 0, int(normalized[3:]), normalized
        return 1, 0, normalized

    @staticmethod
    def _is_rs485_port(port: object) -> bool:
        usb_id = (getattr(port, "vid", None), getattr(port, "pid", None))
        if usb_id in KNOWN_RS485_USB_IDS:
            return True
        details = " ".join(
            str(getattr(port, field, "") or "")
            for field in ("description", "manufacturer", "product", "interface")
        )
        normalized = "".join(character for character in details.casefold() if character.isalnum())
        return "rs485" in normalized

    def _load_serial_ports(self) -> None:
        saved_port = str(self.settings.value("serial485/port", "")).strip()
        selected_port = self.serial_port.currentText().strip() or saved_port
        ports = sorted(
            {
                str(port.device).strip()
                for port in list_ports.comports()
                if str(port.device).strip() and self._is_rs485_port(port)
            },
            key=self._serial_port_sort_key,
        )
        self.serial_port.blockSignals(True)
        self.serial_port.clear()
        self.serial_port.addItems(ports)
        if selected_port in ports:
            self.serial_port.setCurrentText(selected_port)
        elif ports:
            self.serial_port.setCurrentIndex(0)
        else:
            self.serial_port.setEditText("")
        self.serial_port.blockSignals(False)
        self.serial_port.setToolTip(f"检测到 {len(ports)} 个 485 串口")

    def _scan_serial_ports(self) -> None:
        self._load_serial_ports()
        self._poll_serial_presence()
        if self.serial_port.count() and self.serial_port.isEnabled():
            self.serial_port.showPopup()
        elif not self.serial_port.count():
            QMessageBox.warning(self, "未发现串口", "没有检测到可用的 485 串口，请检查连接。")

    def _poll_serial_presence(self) -> None:
        available = {
            str(port.device).strip().casefold()
            for port in list_ports.comports()
            if str(port.device).strip()
        }
        active_port = self._serial_open_port.strip()
        if self._serial_removed:
            if active_port and active_port.casefold() in available:
                self._serial_removed = False
                self._serial_fault = False
                self._serial_open_port = ""
                self._load_serial_ports()
                self._refresh_serial()
            return
        if (
            not self._serial_open
            or self._serial_busy
            or not active_port
            or active_port.casefold() in available
        ):
            return
        self._serial_removed = True
        self._serial_fault = True
        self._serial_busy = True
        self._load_serial_ports()
        self.state.request("serial485.close", reason="device_removed")
        self._refresh_serial()

    def _toggle_serial(self) -> None:
        if self._serial_busy:
            return
        if self._serial_open:
            self._serial_busy = True
            self._serial_fault = False
            self.serial_status.setToolTip("")
            self.state.request("serial485.close")
            self._refresh_serial()
            return
        port = self.serial_port.currentText().strip()
        if not port:
            QMessageBox.warning(self, "串口为空", "请选择或输入 485 转换器的 COM 口。")
            return
        self.serial_port.setCurrentText(port)
        self.settings.set_value("serial485/port", port)
        self._serial_removed = False
        self._serial_open_port = port
        try:
            baud = int(self.settings.value("serial485/baud", 115200))
        except (TypeError, ValueError):
            baud = 115200
        self._serial_busy = True
        self._serial_fault = False
        self.serial_status.setToolTip("")
        self.state.request("serial485.open", port=port, baud=baud)
        self._refresh_serial()

    def _on_task_event(self, action: str, event: str, payload: object) -> None:
        if action in {"connection.connect", "connection.disconnect"}:
            self._handle_orin_event(action, event, payload)
        if action in {"serial485.open", "serial485.close"}:
            self._handle_serial_event(action, event, payload)

    def _handle_orin_event(self, action: str, event: str, payload: object) -> None:
        if (
            self.state.connection_mode is not ConnectionMode.ORIN_REMOTE
            and self._orin_action != "switch"
        ):
            self.refresh()
            return
        if event == "started":
            if self._orin_action != "switch":
                self._orin_action = "connect" if action == "connection.connect" else "disconnect"
        elif event in {"succeeded", "failed", "cancelled"}:
            if action == "connection.disconnect" and event == "succeeded" and self._orin_action == "switch":
                host = self._pending_orin_host
                self._pending_orin_host = ""
                self._orin_action = ""
                self._begin_orin_connect(host)
                return
            self._orin_action = ""
            self._pending_orin_host = ""
        if event == "failed" and isinstance(payload, dict):
            error = str(payload.get("error", "连接失败"))
            self.orin_status.setToolTip(error)
            marker = "的 SSH 主机指纹尚未确认: "
            if action == "connection.connect" and marker in error:
                fingerprint = error.split(marker, 1)[1].strip()
                host = self.orin_host.currentText().strip()
                if (
                    QMessageBox.question(
                        self,
                        "确认 Orin 主机指纹",
                        f"首次连接 {host}，请核对并确认主机指纹：\n\n{fingerprint}\n\n"
                        "确认后将记录该指纹并自动重新连接。",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    == QMessageBox.StandardButton.Yes
                ):
                    self.settings.set_value(ssh_fingerprint_key(host), fingerprint)
                    self.state.set_link_state(LinkState.DISCONNECTED)
                    self._begin_orin_connect(host)
                    return
        elif event == "succeeded":
            self.orin_status.setToolTip("")
            if action == "connection.connect" and self.state.connection_mode is ConnectionMode.ORIN_REMOTE:
                QMessageBox.information(self, "Orin 已连接", "Orin 连接成功。")
        self.refresh()

    def _handle_serial_event(self, action: str, event: str, payload: object) -> None:
        if event in {"started", "progress"}:
            self._serial_busy = True
            self._serial_fault = False
        elif event == "succeeded":
            self._serial_busy = False
            self._serial_open = action == "serial485.open"
            if self._serial_open:
                self._serial_removed = False
                self._serial_fault = False
                self._serial_open_port = self.serial_port.currentText().strip()
            else:
                self._serial_fault = self._serial_removed
                if not self._serial_removed:
                    self._serial_open_port = ""
            self.serial_status.setToolTip("")
            if self._serial_open:
                QMessageBox.information(self, "485 已打开", "485 通信已打开。")
        elif event == "failed":
            self._serial_busy = False
            self._serial_fault = True
            self._serial_open = action == "serial485.close" and not self._serial_removed
            if action == "serial485.open":
                self._serial_removed = False
                self._serial_open_port = ""
            if isinstance(payload, dict):
                self.serial_status.setToolTip(str(payload.get("error", "485 通信故障")))
        elif event == "cancelled":
            self._serial_busy = False
            self._serial_fault = self._serial_removed
            self._serial_open = action == "serial485.close" and not self._serial_removed
        self._refresh_serial()

    def _refresh_serial(self) -> None:
        if self._serial_removed:
            self.serial_status.set_status("485 已拔出", "danger")
            self.serial_status.setToolTip("当前打开的 485 转换器已从电脑拔出")
        elif self._serial_busy:
            text = "485 关闭中" if self._serial_open else "485 打开中"
            self.serial_status.set_status(text, "warning")
        elif self._serial_fault:
            self.serial_status.set_status("485 故障", "danger")
        elif self._serial_open:
            self.serial_status.set_status("485 已打开", "success")
        else:
            self.serial_status.set_status("485 未打开", "neutral")
        self.serial_button.setText("关闭" if self._serial_open else "打开")
        self.serial_button.setEnabled(not self._serial_busy)
        can_select_port = not self._serial_busy and not self._serial_open
        self.serial_port.setEnabled(can_select_port)
        self.serial_scan_button.setEnabled(not self._serial_busy)

    def refresh(self) -> None:
        if self.evt_combo.currentText() != self.state.evt.variant:
            self.evt_combo.blockSignals(True)
            self.evt_combo.setCurrentText(self.state.evt.variant)
            self.evt_combo.blockSignals(False)
        orin_link_state = (
            self.state.link_state
            if self.state.connection_mode is ConnectionMode.ORIN_REMOTE
            else LinkState.DISCONNECTED
        )
        if self._orin_action in {"disconnect", "switch"}:
            orin_text, tone = "Orin 断开中", "warning"
        else:
            link_map = {
                LinkState.DISCONNECTED: ("Orin 未连接", "neutral"),
                LinkState.CONNECTING: ("Orin 连接中", "warning"),
                LinkState.CONNECTED: ("Orin 已连接", "success"),
                LinkState.FAULT: ("Orin 失败", "danger"),
            }
            orin_text, tone = link_map[orin_link_state]
        self.orin_status.set_status(orin_text, tone)
        connected = orin_link_state is LinkState.CONNECTED
        self.orin_button.setText("断开" if connected else "连接")
        self.orin_button.setEnabled(
            orin_link_state is not LinkState.CONNECTING and not self._orin_action
        )
        self.orin_host.setEnabled(
            orin_link_state is not LinkState.CONNECTING and not connected
        )
        self._refresh_serial()


class FunctionConnectionBar(QFrame):
    def __init__(
        self,
        state: ApplicationState,
        modes: Iterable[tuple[ConnectionMode, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self._modes = tuple(modes)
        if not self._modes:
            raise ValueError("至少需要一种通信方式")
        self._selected_mode = self._modes[0][0]
        self._connect_after_disconnect = False
        self.setObjectName("FunctionConnectionBar")
        self.setStyleSheet(
            f"QFrame#FunctionConnectionBar {{background:{COLORS['surface_muted']}; "
            f"border:1px solid {COLORS['line']}; border-radius:8px;}}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 10, 8)
        layout.setSpacing(10)

        label = QLabel("通信方式")
        label.setObjectName("Muted")
        layout.addWidget(label)
        if len(self._modes) > 1:
            self.selector = SegmentedControl(
                [(mode.value, text) for mode, text in self._modes]
            )
            self.selector.changed.connect(self._selection_changed)
            layout.addWidget(self.selector)
        else:
            self.selector = None
            method = QLabel(self._modes[0][1])
            method.setStyleSheet(f"color:{COLORS['ink']}; font-weight:700;")
            layout.addWidget(method)

        self.status = StatusPill()
        layout.addWidget(self.status)
        layout.addStretch(1)
        self.connect_button = QPushButton()
        self.connect_button.setProperty("primary", True)
        self.connect_button.setIcon(lucide_icon("plug", "#FFFFFF", 17))
        self.connect_button.clicked.connect(self._toggle_connection)
        layout.addWidget(self.connect_button)

        state.changed.connect(self.refresh)
        state.task_event.connect(self._on_task_event)
        self.refresh()

    def _selection_changed(self, value: str) -> None:
        self._selected_mode = ConnectionMode(value)
        self.refresh()

    def _toggle_connection(self) -> None:
        if self.state.link_state is LinkState.CONNECTING:
            return
        connected_to_selection = (
            self.state.link_state is LinkState.CONNECTED
            and self.state.connection_mode is self._selected_mode
        )
        if connected_to_selection:
            self.state.request("connection.disconnect")
            return
        if self.state.link_state is LinkState.CONNECTED:
            self._connect_after_disconnect = True
            self.state.set_connection_mode(self._selected_mode)
            return
        self._begin_connect()

    def _begin_connect(self) -> None:
        self.state.set_connection_mode(self._selected_mode)
        self.state.set_link_state(LinkState.CONNECTING)
        self.state.request(
            "connection.connect",
            mode=self._selected_mode.value,
            evt=self.state.evt.variant,
        )

    def _on_task_event(self, action: str, event: str, _payload: object) -> None:
        if (
            action == "connection.disconnect"
            and event == "succeeded"
            and self._connect_after_disconnect
        ):
            self._connect_after_disconnect = False
            self._begin_connect()
        elif action == "connection.disconnect" and event in {"failed", "cancelled"}:
            self._connect_after_disconnect = False

    def refresh(self) -> None:
        if self.selector is not None:
            self.selector.set_value(self._selected_mode.value)
        is_selected_link = self.state.connection_mode is self._selected_mode
        link_state = self.state.link_state if is_selected_link else LinkState.DISCONNECTED
        link_map = {
            LinkState.DISCONNECTED: ("未连接", "neutral"),
            LinkState.CONNECTING: ("连接中", "primary"),
            LinkState.CONNECTED: ("已连接", "success"),
            LinkState.FAULT: ("连接故障", "danger"),
        }
        self.status.set_status(*link_map[link_state])
        self.connect_button.setEnabled(link_state is not LinkState.CONNECTING)
        self.connect_button.setText("断开" if link_state is LinkState.CONNECTED else "连接")
