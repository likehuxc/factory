from __future__ import annotations

import json
import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from d7_factory_studio.app import create_application
from d7_factory_studio.core.models import LinkState
from d7_factory_studio.ui.controls import D7DoubleSpinBox, D7SpinBox, D7TableWidget
from d7_factory_studio.ui.main_window import MainWindow
from d7_factory_studio.ui.pages.firmware import FirmwareTargetPanel
from d7_factory_studio.ui.pages.motor import (
    MotorIdPage,
    Serial485ControlPage,
    serial_servo_state,
)


class MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def value(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set_value(self, key: str, value: object) -> None:
        self.values[key] = value


def test_production_navigation_starts_at_motor_id() -> None:
    app = create_application([])
    assert not app.windowIcon().isNull()
    window = MainWindow()
    assert "home" not in window.page_indexes
    assert "motor.nodes" not in window.page_indexes
    assert window.nav_buttons["motor.id"].isChecked()
    assert window.pages.currentIndex() == window.page_indexes["motor.id"]
    window.nav_buttons["motor.can"].click()
    assert window.pages.currentIndex() == window.page_indexes["motor.can"]
    window.close()
    app.processEvents()


def test_diagnostic_interfaces_use_multi_select_choice_buttons() -> None:
    app = create_application([])
    window = MainWindow()
    page = window.pages.widget(window.page_indexes["diagnostics.link"])
    buttons = [
        page.interface_checks.itemAt(index).widget()
        for index in range(page.interface_checks.count())
        if isinstance(page.interface_checks.itemAt(index).widget(), QPushButton)
    ]
    assert len(buttons) == len(window.state.evt.interfaces)
    assert all(button.isCheckable() and button.isChecked() for button in buttons)
    assert {str(button.property("interface")) for button in buttons} == {
        "can0",
        "can1",
        "can2",
        "can4",
        "can5",
    }
    assert buttons[-1].text() == "MCU CAN5"
    assert "Classic CAN" in buttons[-1].toolTip()

    first_interface = str(buttons[0].property("interface"))
    buttons[0].click()
    assert first_interface not in page._selected_interfaces()
    window.close()
    app.processEvents()


def test_top_rail_is_global_and_node_test_selects_its_interface() -> None:
    app = create_application([])
    window = MainWindow()
    assert not hasattr(window.status_rail, "interface_combo")
    assert not hasattr(window.status_rail, "mode")
    assert not hasattr(window.status_rail, "connect_button")
    page = window.pages.widget(window.page_indexes["diagnostics.nodes"])
    page.node_interface.setCurrentIndex(1)
    interface = str(page.node_interface.currentData())
    assert page.node_table.rowCount() == len(window.state.evt.nodes_for_bus(interface))
    captured: list[tuple[str, object]] = []
    window.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    page._run_broadcast()
    assert captured[-1] == (
        "diagnostics.broadcast",
        {"interface": interface, "duration_s": 10},
    )

    tables = window.findChildren(D7TableWidget)
    assert len(tables) >= 6
    assert all(table.verticalHeader().defaultSectionSize() == 38 for table in tables)
    machine = window.pages.widget(window.page_indexes["machine"])
    assert machine.can_interface.count() == len(window.state.evt.interfaces)
    window.close()
    app.processEvents()


def test_evt_switch_rebuilds_motor_catalog() -> None:
    app = create_application([])
    window = MainWindow()
    window.state.set_evt("EVT1")
    motor_id = window.pages.widget(window.page_indexes["motor.id"])
    assert sum(table.rowCount() for table in motor_id._tables) == 30
    assert any(
        table.item(row, 6).text() == "CAN7"
        for table in motor_id._tables
        for row in range(table.rowCount())
    )
    first_table = motor_id._tables[0]
    assert first_table.columnCount() == 8
    assert first_table.horizontalHeaderItem(3).text() == "名称"
    assert first_table.item(0, 3).text() == "14mini N2"
    window.close()
    app.processEvents()


def test_evt_switch_rebuilds_link_test_with_evt1_mcu_can3() -> None:
    app = create_application([])
    window = MainWindow()
    window.state.set_evt("EVT1")
    page = window.pages.widget(window.page_indexes["diagnostics.link"])
    interfaces = {
        str(page.interface_checks.itemAt(index).widget().property("interface"))
        for index in range(page.interface_checks.count())
        if isinstance(page.interface_checks.itemAt(index).widget(), QPushButton)
    }

    assert interfaces == {"can0", "can1", "can3", "can5", "can7"}
    window.close()
    app.processEvents()


def test_motor_id_write_status_is_visible_and_persists() -> None:
    app = create_application([])
    settings = MemorySettings()
    window = MainWindow()
    page = MotorIdPage(window.state, settings)  # type: ignore[arg-type]
    page._on_task_event("serial485.open", "succeeded", {})
    page._pending_logic_id = 1
    page._pending_target_id = 0x11

    window.state.notify_task("serial485.write_identity", "succeeded", {})

    assert page._status_by_logic_id[1] == "已写入"
    assert "ID 已写入 1 / 30" in page.summary.text()
    assert page.tabs.tabText(0).endswith("1/6")
    page.close()

    restored = MotorIdPage(window.state, settings)  # type: ignore[arg-type]
    assert restored._tables[0].item(0, 0).text() == "● 已写入"
    restored.close()
    window.close()
    app.processEvents()


def test_motor_id_refresh_preserves_active_group_row_and_scroll() -> None:
    app = create_application([])
    window = MainWindow()
    page = MotorIdPage(window.state, MemorySettings())  # type: ignore[arg-type]
    page.resize(1000, 600)
    page.show()
    app.processEvents()
    page.tabs.setCurrentIndex(1)
    table = page._tables[1]
    table.setCurrentCell(5, 0)
    table.selectRow(5)
    table.verticalScrollBar().setValue(table.verticalScrollBar().maximum())
    selected_logic_id = int(table.item(5, 0).data(Qt.ItemDataRole.UserRole))
    assert table.verticalScrollBar().value() == table.verticalScrollBar().maximum()

    page._rebuild_tables()
    app.processEvents()

    rebuilt = page._tables[1]
    assert page.tabs.currentIndex() == 1
    assert int(rebuilt.item(rebuilt.currentRow(), 0).data(Qt.ItemDataRole.UserRole)) == selected_logic_id
    assert rebuilt.verticalScrollBar().value() == rebuilt.verticalScrollBar().maximum()
    page.close()
    window.close()
    app.processEvents()


def test_motor_id_connection_displays_reported_comm_id_in_decimal_and_hex() -> None:
    app = create_application([])
    window = MainWindow()
    page = MotorIdPage(window.state, MemorySettings())  # type: ignore[arg-type]
    page._on_task_event("serial485.open", "succeeded", {})

    window.state.notify_task(
        "serial485.connect",
        "succeeded",
        {
            "comm_id": 0x21,
            "station": 0x01,
            "parameters": [{"address": 0x200129, "value": 33}],
        },
    )

    assert "连接地址：0x01（1）" in page.connection_status.text()
    assert "通信 ID：0x21（33）" in page.connection_status.text()
    page.close()
    window.close()
    app.processEvents()


def test_motor_id_serial_toggle_restores_first_launch_state() -> None:
    app = create_application([])
    settings = MemorySettings()
    window = MainWindow()
    page = MotorIdPage(window.state, settings)  # type: ignore[arg-type]
    page._serial_open = True
    page.current_id.setValue(0x21)
    page.connection_status.setText("已连接 0x21")
    page.connection_status.setToolTip("旧连接")
    page.scan_result.setText("检测到 0x21")
    page._pending_logic_id = 1
    page._pending_target_id = 0x11
    page._status_by_logic_id[1] = "已写入"
    page._persist_statuses()
    page._rebuild_tables()
    page.tabs.setCurrentIndex(1)
    page._tables[1].selectRow(2)

    page._on_task_event("serial485.close", "started", {})

    assert not page._serial_open
    assert page.current_id.value() == 1
    assert page.connection_status.text() == "未连接"
    assert page.connection_status.toolTip() == ""
    assert page.scan_result.text() == "尚未检测"
    assert page._pending_logic_id is None
    assert page._pending_target_id is None
    assert page._status_by_logic_id == {1: "已写入"}
    assert json.loads(str(settings.value(page._status_key()))) == {"1": "已写入"}
    assert page.summary.text() == "ID 已写入 1 / 30 台 · 待写入 29 台"
    assert page.tabs.currentIndex() == 0

    # Results already queued by a cancelled worker must not dirty the reset page.
    page._on_task_event(
        "serial485.connect",
        "succeeded",
        {"comm_id": 0x21, "station": 0x01},
    )
    page._on_task_event("serial485.scan", "succeeded", object())
    assert page.current_id.value() == 1
    assert page.connection_status.text() == "未连接"
    assert page.scan_result.text() == "尚未检测"

    page._on_task_event("serial485.open", "succeeded", {})
    assert page._serial_open
    assert page.current_id.value() == 1
    assert page.connection_status.text() == "未连接"
    assert page.scan_result.text() == "尚未检测"
    assert page._status_by_logic_id == {1: "已写入"}

    page.close()
    window.close()
    app.processEvents()


def test_485_control_connect_reads_enable_and_authority_without_writing() -> None:
    app = create_application([])
    window = MainWindow()
    page = Serial485ControlPage(window.state, MemorySettings())  # type: ignore[arg-type]
    assert page.velocity.value() == 0.5
    captured: list[tuple[str, object]] = []
    window.state.action_requested.disconnect()
    window.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    page._on_task_event("serial485.open", "succeeded", {})
    comm_id = int(page.comm_id.currentData())

    window.state.notify_task(
        "serial485.connect",
        "succeeded",
        {"comm_id": 1, "parameters": [{"address": 0x200201, "value": 0}]},
    )
    assert page.connection_status.text() == "连接：未连接"

    page.connect_motor.click()

    assert captured == [("serial485.connect", {"station_id": comm_id})]
    window.state.notify_task(
        "serial485.connect",
        "succeeded",
        {
            "comm_id": comm_id,
            "parameters": [
                {"address": 0x604100, "value": 0x0037},
                {"address": 0x200201, "value": 0},
            ],
        },
    )
    assert page.connection_status.text() == f"连接：已连接 0x{comm_id:02X}"
    assert page.enable_status.text() == "使能状态：使能（0x0037）"
    assert page.authority_status.text() == "控制权：485（0）"
    assert page._authority_comm_id == comm_id

    page.connect_motor.click()
    window.state.notify_task(
        "serial485.connect",
        "succeeded",
        {
            "comm_id": comm_id,
            "parameters": [
                {"address": 0x604100, "value": 0x0033},
                {"address": 0x200201, "value": 1},
            ],
        },
    )
    assert page.enable_status.text() == "使能状态：禁能（0x0033）"
    assert page.authority_status.text() == "控制权：CAN（1）"
    assert page._authority_comm_id is None
    page.close()
    window.close()
    app.processEvents()


def test_485_control_actions_are_rendered_below_the_connection_row() -> None:
    app = create_application([])
    window = MainWindow()
    page = Serial485ControlPage(window.state, MemorySettings())  # type: ignore[arg-type]
    page.resize(1100, 700)
    page.show()
    app.processEvents()

    connection_y = page.connect_motor.mapTo(page, QPoint()).y()
    control_buttons = [
        button
        for button in page.findChildren(QPushButton)
        if button.text() in {"释放控制权", "获取控制权", "使能", "释放抱闸", "停止"}
    ]
    assert len(control_buttons) == 5
    assert all(button.mapTo(page, QPoint()).y() > connection_y for button in control_buttons)

    page.close()
    window.close()
    app.processEvents()


def test_jihua_servo_statusword_mapping() -> None:
    assert serial_servo_state(0x0037) == "使能"
    assert serial_servo_state(0x0033) == "禁能"
    assert serial_servo_state(0x0018) == "故障"
    assert serial_servo_state(0x0001) == "未定义"


def test_485_control_automatically_reads_back_after_control_change(monkeypatch) -> None:
    app = create_application([])
    window = MainWindow()
    page = Serial485ControlPage(window.state, MemorySettings())  # type: ignore[arg-type]
    captured: list[tuple[str, object]] = []
    timers: list[tuple[int, object]] = []
    window.state.action_requested.disconnect()
    window.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    page._on_task_event("serial485.open", "succeeded", {})

    monkeypatch.setattr(
        "d7_factory_studio.ui.pages.motor.QTimer.singleShot",
        lambda delay_ms, callback: timers.append((delay_ms, callback)),
    )
    comm_id = int(page.comm_id.currentData())
    page._request_common("take_control")
    window.state.notify_task(
        "serial485.take_control",
        "succeeded",
        {"operation": "take_control", "comm_id": comm_id},
    )

    assert page.connection_status.text() == "连接：等待电机重启并自动读取"
    assert timers[-1][0] == 2200
    timers[-1][1]()
    assert captured[-1] == ("serial485.connect", {"station_id": comm_id})
    page.close()
    window.close()
    app.processEvents()


def test_485_control_blocks_repeated_enable_until_verified() -> None:
    app = create_application([])
    window = MainWindow()
    page = Serial485ControlPage(window.state, MemorySettings())  # type: ignore[arg-type]
    captured: list[tuple[str, object]] = []
    window.state.action_requested.disconnect()
    window.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    page._on_task_event("serial485.open", "succeeded", {})
    comm_id = int(page.comm_id.currentData())
    page._authority_comm_id = comm_id

    page.enable_button.click()
    page._on_task_event("serial485.enable", "started", {})
    page.enable_button.click()

    assert captured == [("serial485.enable", {"station_id": comm_id})]
    assert not page.enable_button.isEnabled()
    assert page.enable_button.text() == "使能中…"
    assert page.stop_button.isEnabled()

    page._on_task_event(
        "serial485.enable",
        "succeeded",
        {"operation": "enable", "comm_id": comm_id, "statusword": 0x1237},
    )
    assert page.enable_button.isEnabled()
    assert page.enable_status.text() == "使能状态：使能（0x1237）"
    assert page.enable_button.text() == "使能"
    page.close()
    window.close()
    app.processEvents()


def test_double_spinbox_first_click_allows_keyboard_replacement() -> None:
    app = create_application([])
    spin = D7DoubleSpinBox()
    spin.setRange(-0.5, 0.5)
    spin.setDecimals(3)
    spin.setSuffix(" rad/s")
    spin.resize(500, 44)
    spin.show()
    app.processEvents()

    QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=spin.lineEdit().rect().center())
    app.processEvents()
    QTest.keyClicks(spin, "-0.25")
    QTest.keyClick(spin, Qt.Key.Key_Return)

    assert spin.value() == -0.25
    spin.close()


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


def test_battery_upgrade_defaults_to_pace_and_preserves_d7_iap_fields() -> None:
    app = create_application([])
    window = MainWindow()
    firmware_page = window.pages.widget(window.page_indexes["firmware"])
    panels = firmware_page.findChildren(FirmwareTargetPanel)
    battery = next(panel for panel in panels if panel.target == "battery")
    assert battery.protocol.currentData() == "pace_bin"
    assert not battery.battery_address.isHidden()
    assert battery.battery_address.text() == "0"
    assert battery.target_id.isHidden()
    assert battery.iap_id.isHidden()
    assert battery.quick_widget.isHidden()

    battery.protocol.setCurrentIndex(1)
    assert battery.protocol.currentData() == "d7_iap"
    assert battery.battery_address.isHidden()
    assert not battery.target_id.isHidden()
    assert not battery.quick_widget.isHidden()
    assert battery.target_id.text() == "0x42"
    assert battery.iap_id.text() == "0x7FF"
    window.close()
    app.processEvents()


def test_firmware_role_and_version_queries_are_exposed_and_update_values() -> None:
    app = create_application([])
    window = MainWindow()
    firmware_page = window.pages.widget(window.page_indexes["firmware"])
    panels = firmware_page.findChildren(FirmwareTargetPanel)
    pmu = next(panel for panel in panels if panel.target == "pmu")
    captured: list[tuple[str, object]] = []
    window.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    window.state.set_link_state(LinkState.CONNECTED)

    assert pmu.query_role_button.isEnabled()
    assert pmu.query_version_button.isEnabled()
    pmu.query_role_button.click()
    assert captured[-1] == (
        "firmware.query_role",
        {"target": "pmu", "target_id": 0x18, "iap_id": 0x7FF},
    )
    window.state.notify_task("firmware.query_role.pmu", "succeeded", {"role": "BOOT"})
    assert pmu.role_value.text() == "BOOT"

    pmu.query_version_button.click()
    assert captured[-1] == (
        "firmware.query_version",
        {"target": "pmu", "target_id": 0x18, "iap_id": 0x7FF},
    )
    window.state.notify_task(
        "firmware.query_version.pmu", "succeeded", {"version": "1.12.3"}
    )
    assert pmu.version_value.text() == "1.12.3"
    window.close()
    app.processEvents()


def test_battery_iap_preview_prints_frames_without_requesting_hardware(
    tmp_path: Path,
) -> None:
    app = create_application([])
    window = MainWindow()
    firmware_page = window.pages.widget(window.page_indexes["firmware"])
    panels = firmware_page.findChildren(FirmwareTargetPanel)
    battery = next(panel for panel in panels if panel.target == "battery")
    source = tmp_path / "battery.bin"
    source.write_bytes(struct.pack("<II", 0x20001000, 0x000202C9) + bytes(64))
    captured: list[tuple[str, object]] = []
    window.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))

    battery.protocol.setCurrentIndex(1)
    battery.file_path.setText(str(source))
    battery.preview.click()

    assert captured == []
    assert "以下报文仅打印，不会发送到 CAN 总线" in battery.console.toPlainText()
    assert "模拟TX[01]" in battery.console.toPlainText()
    assert "SP 0x20001000" in battery.firmware_details.text()
    assert "ResetVector 0x000202C9" in battery.firmware_details.text()
    window.close()
    app.processEvents()
