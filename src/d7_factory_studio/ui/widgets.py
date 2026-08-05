from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import ConnectionMode, LinkState
from d7_factory_studio.ui.icons import lucide_icon
from d7_factory_studio.ui.theme import COLORS


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
        self.setFixedSize(76, 64)
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
            "font-size:11px; font-weight:600; padding:5px 3px;}"
            f"QToolButton:hover {{background:{hover}; color:white;}}"
        )


class SecondaryNavButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(38)
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
            button.setMinimumHeight(30)
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
            "padding:4px 9px; font-size:11px; font-weight:700;"
        )


class StatusRail(QWidget):
    connect_requested = Signal()

    def __init__(self, state: ApplicationState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.setObjectName("TopRail")
        self.setFixedHeight(66)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 10, 22, 10)
        layout.setSpacing(12)

        self.mode = SegmentedControl(
            [(ConnectionMode.PC_DIRECT.value, "PC 直连"), (ConnectionMode.ORIN_REMOTE.value, "Orin 远程")]
        )
        self.mode.changed.connect(lambda value: state.set_connection_mode(ConnectionMode(value)))
        layout.addWidget(self.mode)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet(f"color:{COLORS['line']};")
        layout.addWidget(divider)

        self.evt_combo = QComboBox()
        self.evt_combo.addItems(["EVT2", "EVT1"])
        self.evt_combo.setToolTip("切换整机 CAN 拓扑；切换后自动断开并锁定")
        self.evt_combo.currentTextChanged.connect(self._evt_changed)
        layout.addWidget(self.evt_combo)

        self.interface_combo = QComboBox()
        self.interface_combo.currentTextChanged.connect(self._interface_changed)
        layout.addWidget(self.interface_combo)

        self.link_pill = StatusPill()
        self.lock_pill = StatusPill()
        self.nodes_pill = StatusPill()
        layout.addWidget(self.link_pill)
        layout.addWidget(self.nodes_pill)
        layout.addWidget(self.lock_pill)
        layout.addStretch(1)

        self.lock_button = QPushButton("解除安全锁")
        self.lock_button.setIcon(lucide_icon("unlock", COLORS["warning"], 17))
        self.lock_button.clicked.connect(self._toggle_lock)
        layout.addWidget(self.lock_button)

        self.connect_button = QPushButton("连接设备")
        self.connect_button.setProperty("primary", True)
        self.connect_button.setIcon(lucide_icon("plug", "#FFFFFF", 17))
        self.connect_button.clicked.connect(self.connect_requested.emit)
        layout.addWidget(self.connect_button)

        state.changed.connect(self.refresh)
        self.refresh()

    def _evt_changed(self, value: str) -> None:
        if value:
            self.state.set_evt(value)

    def _interface_changed(self, value: str) -> None:
        if value and value in self.state.evt.interfaces:
            self.state.set_interface(value)

    def _toggle_lock(self) -> None:
        if self.state.safety_locked:
            try:
                self.state.unlock()
            except RuntimeError as exc:
                self.state.log("安全", str(exc), "error")
        else:
            self.state.lock()

    def refresh(self) -> None:
        self.mode.set_value(self.state.connection_mode.value)
        if self.evt_combo.currentText() != self.state.evt.variant:
            self.evt_combo.blockSignals(True)
            self.evt_combo.setCurrentText(self.state.evt.variant)
            self.evt_combo.blockSignals(False)
        current_interfaces = list(self.state.evt.interfaces)
        listed = [self.interface_combo.itemText(index) for index in range(self.interface_combo.count())]
        if listed != current_interfaces:
            self.interface_combo.blockSignals(True)
            self.interface_combo.clear()
            self.interface_combo.addItems(current_interfaces)
            self.interface_combo.setCurrentText(self.state.active_interface)
            self.interface_combo.blockSignals(False)

        link_map = {
            LinkState.DISCONNECTED: ("● 未连接", "neutral"),
            LinkState.CONNECTING: ("● 连接中", "primary"),
            LinkState.CONNECTED: ("● 已连接", "success"),
            LinkState.FAULT: ("● 连接故障", "danger"),
        }
        self.link_pill.set_status(*link_map[self.state.link_state])
        self.nodes_pill.set_status(f"节点 {self.state.online_nodes}/{len(self.state.evt.nodes)}", "neutral")
        self.lock_pill.set_status(
            "已锁定" if self.state.safety_locked else "已解锁",
            "warning" if self.state.safety_locked else "danger",
        )
        connected = self.state.link_state is LinkState.CONNECTED
        self.connect_button.setText("断开连接" if connected else "连接设备")
        self.lock_button.setText("解除安全锁" if self.state.safety_locked else "恢复安全锁")
        self.lock_button.setEnabled(connected)
