from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from d7_factory_studio.app import create_application
from d7_factory_studio.ui.controls import D7SpinBox, D7TableWidget
from d7_factory_studio.ui.main_window import MainWindow


def test_primary_and_motor_navigation() -> None:
    app = create_application([])
    assert not app.windowIcon().isNull()
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


def test_diagnostic_interfaces_use_multi_select_choice_buttons() -> None:
    app = create_application([])
    window = MainWindow()
    page = window.pages.widget(window.page_indexes["diagnostics"])
    buttons = [
        page.interface_checks.itemAt(index).widget()
        for index in range(page.interface_checks.count())
        if isinstance(page.interface_checks.itemAt(index).widget(), QPushButton)
    ]
    assert len(buttons) == len(window.state.evt.interfaces)
    assert all(button.isCheckable() and button.isChecked() for button in buttons)

    first_interface = str(buttons[0].property("interface"))
    buttons[0].click()
    assert first_interface not in page._selected_interfaces()
    window.close()
    app.processEvents()


def test_top_rail_hides_global_can_and_nodes_select_their_own_interface() -> None:
    app = create_application([])
    window = MainWindow()
    assert not hasattr(window.status_rail, "interface_combo")

    page = window.pages.widget(window.page_indexes["diagnostics"])
    page.node_interface.setCurrentIndex(1)
    interface = str(page.node_interface.currentData())
    assert page.node_table.rowCount() == len(window.state.evt.nodes_for_bus(interface))
    captured: list[tuple[str, object]] = []
    window.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    page._run_broadcast()
    assert captured[-1] == ("diagnostics.broadcast", {"interface": interface})

    tables = window.findChildren(D7TableWidget)
    assert len(tables) >= 9
    assert all(table.verticalHeader().defaultSectionSize() == 38 for table in tables)
    machine = window.pages.widget(window.page_indexes["machine"])
    assert machine.can_interface.count() == len(window.state.evt.interfaces)
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
