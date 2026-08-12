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
from d7_factory_studio.settings_store import SettingsStore
from d7_factory_studio.ui.pages.diagnostics import LinkTestPage, NodeTestPage
from d7_factory_studio.ui.pages.firmware import FirmwarePage
from d7_factory_studio.ui.pages.logs import LogsPage
from d7_factory_studio.ui.pages.machine import MachinePage
from d7_factory_studio.ui.pages.motor import (
    CanControlPage,
    MotorIdPage,
    Serial485ControlPage,
    ZeroCalibrationPage,
)
from d7_factory_studio.ui.pages.settings import SettingsPage
from d7_factory_studio.ui.widgets import SecondaryNavButton, StatusRail


class MainWindow(QMainWindow):
    CALIBRATION_ITEMS = (
        ("motor.id", "1  电机 ID 写入"),
        ("diagnostics.link", "2  链路测试"),
        ("diagnostics.nodes", "3  电机节点测试"),
        ("motor.zero", "4  电机标零"),
    )
    TOOL_ITEMS = (
        ("motor.485", "电机控制 485"),
        ("motor.can", "电机控制 CAN"),
        ("firmware", "OTA 升级"),
        ("machine", "整机工具"),
        ("logs", "设备日志"),
        ("settings", "设置"),
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

        sidebar = QWidget()
        sidebar.setObjectName("SecondarySidebar")
        sidebar.setFixedWidth(228)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 18, 16, 16)
        sidebar_layout.setSpacing(5)

        brand_row = QHBoxLayout()
        brand = QLabel()
        brand.setObjectName("BrandMark")
        brand.setPixmap(self.windowIcon().pixmap(QSize(42, 42)))
        brand.setFixedSize(46, 46)
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_text = QLabel("D7 产线工作台")
        brand_text.setObjectName("SectionTitle")
        brand_row.addWidget(brand)
        brand_row.addWidget(brand_text, 1)
        sidebar_layout.addLayout(brand_row)
        sidebar_layout.addSpacing(18)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, SecondaryNavButton] = {}
        self._add_nav_section(sidebar_layout, "电机标定步骤", self.CALIBRATION_ITEMS)
        sidebar_layout.addSpacing(18)
        self._add_nav_section(sidebar_layout, "其它工具", self.TOOL_ITEMS)
        sidebar_layout.addStretch(1)
        root_layout.addWidget(sidebar)
        self.sidebar = sidebar

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        root_layout.addWidget(right, 1)

        self.status_rail = StatusRail(self.state, self.settings)
        right_layout.addWidget(self.status_rail)
        self.pages = QStackedWidget()
        right_layout.addWidget(self.pages, 1)

        page_instances = (
            ("motor.id", MotorIdPage(self.state, self.settings)),
            ("diagnostics.link", LinkTestPage(self.state)),
            ("diagnostics.nodes", NodeTestPage(self.state)),
            ("motor.zero", ZeroCalibrationPage(self.state)),
            ("motor.485", Serial485ControlPage(self.state, self.settings)),
            ("motor.can", CanControlPage(self.state, self.settings)),
            ("firmware", FirmwarePage(self.state)),
            ("machine", MachinePage(self.state)),
            ("logs", LogsPage(self.state, self.settings)),
            ("settings", SettingsPage(self.state, self.settings)),
        )
        self.page_indexes: dict[str, int] = {}
        for key, page in page_instances:
            self.page_indexes[key] = self.pages.addWidget(page)
        self.show_page("motor.id")

    def _add_nav_section(
        self,
        layout: QVBoxLayout,
        title: str,
        items: tuple[tuple[str, str], ...],
    ) -> None:
        heading = QLabel(title)
        heading.setObjectName("BrandCaption")
        heading.setStyleSheet("color:#7D8BA0; padding:0 8px 5px 8px;")
        layout.addWidget(heading)
        for key, label in items:
            button = SecondaryNavButton(label)
            button.setProperty("pageKey", key)
            button.clicked.connect(lambda _checked=False, selected=key: self.show_page(selected))
            self.nav_group.addButton(button)
            self.nav_buttons[key] = button
            layout.addWidget(button)

    def show_page(self, key: str) -> None:
        index = self.page_indexes.get(key)
        if index is None:
            return
        self.nav_buttons[key].setChecked(True)
        self.pages.setCurrentIndex(index)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.coordinator.shutdown()
        super().closeEvent(event)
