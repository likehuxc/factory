from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from d7_factory_studio.app import create_application
from d7_factory_studio.ui.controls import D7SpinBox
from d7_factory_studio.ui.main_window import MainWindow


def test_primary_and_motor_navigation() -> None:
    app = create_application([])
    window = MainWindow()
    assert window.primary_buttons["home"].isChecked()
    assert not window.secondary.isVisible()
    window.select_primary("motor")
    assert window.primary_buttons["motor"].isChecked()
    assert window.secondary_buttons["motor.nodes"].isChecked()
    assert window.pages.currentIndex() == window.page_indexes["motor.nodes"]
    window.secondary_buttons["motor.can"].click()
    assert window.pages.currentIndex() == window.page_indexes["motor.can"]
    window.close()
    app.processEvents()


def test_evt_switch_rebuilds_motor_catalog() -> None:
    app = create_application([])
    window = MainWindow()
    window.state.set_evt("EVT1")
    overview = window.pages.widget(window.page_indexes["motor.nodes"])
    assert overview.table.rowCount() == 30
    assert overview.table.item(0, 3).text() == "CAN7"
    window.close()
    app.processEvents()


def test_spinbox_wheel_requires_an_explicit_click() -> None:
    app = create_application([])
    spin = D7SpinBox()
    spin.setRange(0, 10)
    spin.setValue(5)
    spin.resize(180, 40)
    spin.show()
    app.processEvents()

    def send_wheel() -> None:
        local = QPointF(spin.rect().center())
        event = QWheelEvent(
            local,
            spin.mapToGlobal(spin.rect().center()).toPointF(),
            QPoint(),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(spin, event)

    spin.setFocus()
    app.processEvents()
    send_wheel()
    assert spin.value() == 5

    QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=spin.rect().center())
    send_wheel()
    assert spin.value() == 6

    spin.clearFocus()
    send_wheel()
    assert spin.value() == 6
    spin.close()
