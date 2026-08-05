from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from d7_factory_studio.app import create_application
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
