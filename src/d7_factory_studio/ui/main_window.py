from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.coordinator import ApplicationCoordinator
from d7_factory_studio.core.models import LinkState
from d7_factory_studio.settings_store import SettingsStore
from d7_factory_studio.ui.pages.diagnostics import DiagnosticsPage
from d7_factory_studio.ui.pages.firmware import FirmwarePage
from d7_factory_studio.ui.pages.home import HomePage
from d7_factory_studio.ui.pages.logs import LogsPage
from d7_factory_studio.ui.pages.machine import MachinePage
from d7_factory_studio.ui.pages.motor import (
    CanMotionPage,
    LongTestPage,
    NodeOverviewPage,
    ParameterPage,
    Serial485Page,
    ZeroCalibrationPage,
)
from d7_factory_studio.ui.pages.settings import SettingsPage
from d7_factory_studio.ui.widgets import PrimaryNavButton, SecondaryNavButton, StatusRail


class MainWindow(QMainWindow):
    PRIMARY_ITEMS = (
        ("home", "首页", "home"),
        ("firmware", "升级", "upgrade"),
        ("motor", "电机", "motor"),
        ("diagnostics", "诊断", "diagnostics"),
        ("machine", "整机", "machine"),
        ("logs", "日志", "logs"),
        ("settings", "设置", "settings"),
    )
    MOTOR_ITEMS = (
        ("节点总览", "motor.nodes"),
        ("CAN 运动", "motor.can"),
        ("485 控制", "motor.485"),
        ("参数", "motor.parameters"),
        ("零位", "motor.zero"),
        ("长测", "motor.long_test"),
    )

    def __init__(self, state: ApplicationState | None = None) -> None:
        super().__init__()
        self.state = state or ApplicationState()
        self.settings = SettingsStore()
        self.setWindowTitle("D7 Factory Studio")
        self.resize(1600, 940)
        self.setMinimumSize(QSize(1180, 720))
        self._build_ui()
        self.coordinator = ApplicationCoordinator(self.state, self.settings, self)
        self.state.log("系统", "D7 Factory Studio 已启动")

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        primary = QWidget()
        primary.setObjectName("PrimarySidebar")
        primary.setFixedWidth(92)
        primary_layout = QVBoxLayout(primary)
        primary_layout.setContentsMargins(8, 16, 8, 12)
        primary_layout.setSpacing(5)
        brand = QLabel("D7")
        brand.setObjectName("BrandMark")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption = QLabel("FACTORY\nSTUDIO")
        caption.setObjectName("BrandCaption")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        primary_layout.addWidget(brand)
        primary_layout.addWidget(caption)
        primary_layout.addSpacing(15)
        self.primary_group = QButtonGroup(self)
        self.primary_group.setExclusive(True)
        self.primary_buttons: dict[str, PrimaryNavButton] = {}
        for index, (key, label, icon) in enumerate(self.PRIMARY_ITEMS):
            if key == "settings":
                primary_layout.addStretch(1)
            button = PrimaryNavButton(label, icon)
            button.setProperty("pageKey", key)
            button.clicked.connect(lambda _checked=False, selected=key: self.select_primary(selected))
            self.primary_group.addButton(button, index)
            self.primary_buttons[key] = button
            primary_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
        root_layout.addWidget(primary)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        root_layout.addWidget(right, 1)

        self.status_rail = StatusRail(self.state)
        self.status_rail.connect_requested.connect(self._toggle_connection)
        right_layout.addWidget(self.status_rail)

        workspace = QWidget()
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        right_layout.addWidget(workspace, 1)

        self.secondary = QWidget()
        self.secondary.setObjectName("SecondarySidebar")
        self.secondary.setFixedWidth(202)
        secondary_layout = QVBoxLayout(self.secondary)
        secondary_layout.setContentsMargins(14, 22, 14, 18)
        secondary_layout.setSpacing(5)
        module_title = QLabel("电机工作台")
        module_title.setObjectName("SectionTitle")
        module_caption = QLabel("D7 ACTUATORS")
        module_caption.setObjectName("BrandCaption")
        module_caption.setStyleSheet("color:#7D8BA0;")
        secondary_layout.addWidget(module_title)
        secondary_layout.addWidget(module_caption)
        secondary_layout.addSpacing(12)
        self.secondary_group = QButtonGroup(self)
        self.secondary_group.setExclusive(True)
        self.secondary_buttons: dict[str, SecondaryNavButton] = {}
        for index, (label, key) in enumerate(self.MOTOR_ITEMS):
            button = SecondaryNavButton(label)
            button.clicked.connect(lambda _checked=False, selected=key: self._show_page(selected))
            self.secondary_group.addButton(button, index)
            self.secondary_buttons[key] = button
            secondary_layout.addWidget(button)
        secondary_layout.addStretch(1)
        safety_note = QLabel("运动指令受顶部\n会话安全锁保护")
        safety_note.setObjectName("Muted")
        safety_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        secondary_layout.addWidget(safety_note)
        workspace_layout.addWidget(self.secondary)

        self.pages = QStackedWidget()
        workspace_layout.addWidget(self.pages, 1)
        self.page_indexes: dict[str, int] = {}
        home = HomePage(self.state)
        home.navigate_requested.connect(self.select_primary)
        page_instances = (
            ("home", home),
            ("firmware", FirmwarePage(self.state)),
            ("motor.nodes", NodeOverviewPage(self.state)),
            ("motor.can", CanMotionPage(self.state)),
            ("motor.485", Serial485Page(self.state)),
            ("motor.parameters", ParameterPage(self.state)),
            ("motor.zero", ZeroCalibrationPage(self.state)),
            ("motor.long_test", LongTestPage(self.state)),
            ("diagnostics", DiagnosticsPage(self.state)),
            ("machine", MachinePage(self.state)),
            ("logs", LogsPage(self.state, self.settings)),
            ("settings", SettingsPage(self.state, self.settings)),
        )
        for key, page in page_instances:
            self.page_indexes[key] = self.pages.addWidget(page)
        self.select_primary("home")

    def select_primary(self, key: str) -> None:
        if key not in self.primary_buttons:
            return
        self.primary_buttons[key].setChecked(True)
        is_motor = key == "motor"
        self.secondary.setVisible(is_motor)
        if is_motor:
            self.secondary_buttons["motor.nodes"].setChecked(True)
            self._show_page("motor.nodes")
        else:
            self._show_page(key)

    def _show_page(self, key: str) -> None:
        index = self.page_indexes.get(key)
        if index is not None:
            self.pages.setCurrentIndex(index)

    def _toggle_connection(self) -> None:
        if self.state.link_state is LinkState.CONNECTED:
            self.state.request("connection.disconnect")
            return
        if self.state.link_state is LinkState.CONNECTING:
            return
        self.state.set_link_state(LinkState.CONNECTING)
        self.state.request(
            "connection.connect",
            mode=self.state.connection_mode.value,
            evt=self.state.evt.variant,
            interface=self.state.active_interface,
        )

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.state.lock("应用退出，已请求所有运动目标停止并失能")
        self.coordinator.shutdown()
        super().closeEvent(event)
