from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from d7_factory_studio.ui.main_window import MainWindow
from d7_factory_studio.ui.theme import apply_theme


def create_application(argv: list[str] | None = None) -> QApplication:
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    QCoreApplication.setOrganizationName("Pudu Robotics")
    QCoreApplication.setApplicationName("D7 Factory Studio")
    QCoreApplication.setApplicationVersion("0.1.0")
    app = QApplication(argv if argv is not None else sys.argv)
    app.setStyle("Fusion")
    apply_theme(app)
    return app


def main() -> int:
    app = create_application()
    window = MainWindow()
    window.show()
    return app.exec()
