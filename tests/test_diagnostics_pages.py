from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import ConnectionMode, LinkState
from d7_factory_studio.ui.pages.diagnostics import (
    DiagnosticsPage,
    LinkTestPage,
    NodeTestPage,
)
from d7_factory_studio.ui.widgets import FunctionConnectionBar


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_link_page_includes_evt_mcu_channel_and_compatible_payload() -> None:
    app = _app()
    state = ApplicationState()
    page = LinkTestPage(state)
    buttons = [
        page.interface_checks.itemAt(index).widget()
        for index in range(page.interface_checks.count())
        if page.interface_checks.itemAt(index).widget() is not None
    ]

    assert [button.property("interface") for button in buttons] == [
        "can0",
        "can1",
        "can2",
        "can4",
        "can5",
    ]
    assert all(button.isChecked() for button in buttons)
    assert [button.text() for button in buttons] == [
        "左臂 CAN0",
        "右臂 CAN1",
        "底盘 CAN2",
        "躯干 & 头部 CAN4",
        "MCU CAN5",
    ]
    assert page.findChildren(QCheckBox) == [page.loop_test]
    assert not page.findChildren(FunctionConnectionBar)

    captured: list[tuple[str, object]] = []
    state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    state.set_connection_mode(ConnectionMode.ORIN_REMOTE)
    state.set_link_state(LinkState.CONNECTED)
    page.loop_test.setChecked(False)
    page._start_stress()

    action, payload = captured[-1]
    assert action == "diagnostics.stress_start"
    assert payload == {
        "interfaces": ["can0", "can1", "can2", "can4", "can5"],
        "profile": "quick",
        "duration_s": 60,
        "gap_ms": 1,
        "capture_dmesg": True,
        "capture_candump": True,
        "clear_dmesg": True,
        "setup_can_script": "~/setup_can.sh",
        "setup_each_stage": True,
        "loop": False,
    }

    state.notify_task(
        "diagnostics.stress_start",
        "succeeded",
        {
            "result": {"evaluation": {"verdict": "PASS"}},
            "bundle": {"report_html": "C:/reports/link.html"},
        },
    )
    assert page.result_value.text() == "通过"
    assert page.report_path.text() == "C:/reports/link.html"
    assert page.diag_progress.value() == 100

    state.notify_task(
        "diagnostics.stress_start",
        "succeeded",
        {"result": {"evaluation": {"verdict": "FAIL"}}, "bundle": {}},
    )
    assert page.result_value.text() == "失败"
    page.close()
    app.processEvents()


def test_link_page_marks_prerequisite_abort_as_not_executed(tmp_path: Path) -> None:
    app = _app()
    state = ApplicationState()
    page = LinkTestPage(state)
    report = tmp_path / "report.html"
    report.write_text("<html></html>", encoding="utf-8")

    state.notify_task(
        "diagnostics.stress_start",
        "succeeded",
        {
            "result": {
                "evaluation": {
                    "verdict": "FAIL",
                    "findings": ["required-tools 检查失败"],
                },
                "execution": {
                    "status": "not_started",
                    "planned_stage_count": 15,
                    "executed_stage_count": 0,
                },
            },
            "bundle": {"report_html": str(report)},
        },
    )

    assert page.result_value.text() == "未执行（前置检查失败）"
    assert page.diag_progress.value() == 0
    assert page.open_report.isEnabled()
    assert "未发送链路测试流量" in page.diag_console.toPlainText()
    assert "required-tools 检查失败" in page.diag_console.toPlainText()
    page.close()
    app.processEvents()


def test_node_page_shares_channel_across_broadcast_and_parameter_actions() -> None:
    app = _app()
    state = ApplicationState()
    page = NodeTestPage(state)
    captured: list[tuple[str, object]] = []
    state.action_requested.connect(lambda action, payload: captured.append((action, payload)))

    assert [page.node_interface.itemData(index) for index in range(page.node_interface.count())] == [
        "can0",
        "can1",
        "can2",
        "can4",
    ]
    assert page.broadcast_duration.value() == 10
    interface = str(page.node_interface.currentData())
    nodes = state.evt.nodes_for_bus(interface)
    assert page.node_table.rowCount() == len(nodes)
    assert page.node_table.columnCount() == 8
    assert page.node_table.horizontalHeaderItem(1).text() == "节点"
    assert page.node_table.horizontalHeaderItem(2).text() == "配置名"
    assert page.node_table.horizontalHeaderItem(6).text() == "参数状态"
    assert page.node_table.horizontalHeaderItem(7).text() == "广播应答"
    assert page.read_button.parentWidget().layout().indexOf(
        page.read_button
    ) < page.write_button.parentWidget().layout().indexOf(page.write_button)
    assert not any(
        button.text() == "开始参数检查" for button in page.findChildren(QPushButton)
    )

    selected = page.node_table.cellWidget(0, 0)
    assert isinstance(selected, QCheckBox)
    selected.setChecked(True)
    page.read_button.click()
    assert captured[-1] == (
        "diagnostics.node_param_read",
        {"interface": interface, "logic_ids": [nodes[0].logic_id]},
    )

    page._run_broadcast()
    assert captured[-1] == (
        "diagnostics.broadcast",
        {"interface": interface, "duration_s": 10},
    )

    state.notify_task(
        "diagnostics.node_param_read",
        "succeeded",
        {"nodes": [{"logic_id": nodes[0].logic_id, "verdict": "PASS"}]},
    )
    state.notify_task(
        "diagnostics.broadcast",
        "succeeded",
        {"interface": interface, "responses": {nodes[0].logic_id: {"count": 1}}},
    )
    assert page.node_table.item(0, page.PARAMETER_COLUMN).text() == "PASS"
    assert page.node_table.item(0, page.BROADCAST_COLUMN).text() == "已应答 1 帧"
    assert page.node_table.item(1, page.BROADCAST_COLUMN).text() == "无应答"

    page._broadcast_live_counts.clear()
    page._broadcast_running_interface = interface
    first_node = nodes[0]
    response_id = (state.evt.broadcast.response_base_id + first_node.dev_id) & 0x7FF
    page._on_activity("12:00:00", "诊断", f"info|{interface} {response_id:03X}##100")
    page._on_activity("12:00:01", "诊断", f"info|{interface} {response_id:03X}##100")
    assert page.node_table.item(0, page.BROADCAST_COLUMN).text() == "已应答 2 帧"
    assert page.frame_count.text() == "2"
    assert page.layout.indexOf(page.workspace) < page.layout.indexOf(page.log_card)
    assert page.action_panel.maximumWidth() == 350
    assert page.node_table.minimumWidth() == 620
    page.close()
    app.processEvents()


def test_node_page_uses_evt_business_channel_names() -> None:
    app = _app()
    state = ApplicationState()
    state.set_evt("EVT1")
    page = NodeTestPage(state)

    assert [button.text() for button in page._channel_buttons] == [
        "右臂 CAN0 (8)",
        "左臂 CAN1 (8)",
        "底盘 CAN5 (8)",
        "躯干 & 头部 CAN7 (6)",
    ]
    assert page.node_table.item(0, 5).text() == "右臂 CAN0"
    assert "CAN FD" not in page.node_interface.itemText(0)
    page.close()
    app.processEvents()


def test_compatibility_shell_contains_two_standalone_pages() -> None:
    app = _app()
    page = DiagnosticsPage(ApplicationState())

    assert page.tabs.count() == 2
    assert isinstance(page.tabs.widget(0), LinkTestPage)
    assert isinstance(page.tabs.widget(1), NodeTestPage)
    assert page.tabs.tabText(0) == "链路测试"
    assert page.tabs.tabText(1) == "电机节点测试"
    assert not page.findChildren(FunctionConnectionBar)
    page.close()
    app.processEvents()
