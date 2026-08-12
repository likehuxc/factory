from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from d7_factory_studio.ui.main_window import MainWindow
from d7_factory_studio.ui.theme import apply_theme


def application_icon_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "app-icon.png"


def create_application(argv: list[str] | None = None) -> QApplication:
    QCoreApplication.setOrganizationName("Pudu Robotics")
    QCoreApplication.setApplicationName("D7 Factory Studio")
    QCoreApplication.setApplicationVersion("0.1.0")
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        existing.setStyle("Fusion")
        existing.setWindowIcon(QIcon(str(application_icon_path())))
        apply_theme(existing)
        return existing
    app = QApplication(argv if argv is not None else sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(str(application_icon_path())))
    apply_theme(app)
    return app


def main() -> int:
    app = create_application()
    window = MainWindow()
    window.show()
    return app.exec()
