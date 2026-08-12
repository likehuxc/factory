from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import ConnectionMode, LinkState
from d7_factory_studio.ui.pages.motor import CanControlPage


class MemorySettings:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = values or {}

    def value(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _target_index(page: CanControlPage, target: tuple[str, object]) -> int:
    return next(
        index
        for index in range(page.target.count())
        if page.target.itemData(index) == target
    )


def _connected_can_box_page() -> tuple[ApplicationState, CanControlPage]:
    _app()
    state = ApplicationState()
    page = CanControlPage(state, MemorySettings())  # type: ignore[arg-type]
    page.source.setCurrentIndex(page.source.findData(ConnectionMode.PC_DIRECT.value))
    state.set_connection_mode(ConnectionMode.PC_DIRECT)
    state.set_link_state(LinkState.CONNECTED)
    page._authority_target = page.target.currentData()
    page._refresh_source()
    return state, page


def test_can_box_blocks_unverified_enable_to_protect_ethercat() -> None:
    state, page = _connected_can_box_page()
    captured: list[tuple[str, object]] = []
    state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    enable = page._operation_buttons["enable"]

    enable.click()

    assert captured == []
    assert not enable.isEnabled()
    assert "EtherCAT" in enable.toolTip()
    assert page.stop_button.isEnabled()
    page.close()


def test_can_box_disables_unverified_motion_controls_to_protect_ethercat() -> None:
    _state, page = _connected_can_box_page()

    for operation in (
        "take_control",
        "release_control",
        "clear_errors",
        "enable",
        "set_velocity",
        "move_absolute",
        "move_relative",
    ):
        assert not page._operation_buttons[operation].isEnabled()
        assert page._operation_buttons[operation].toolTip()
    assert page._operation_buttons["disable"].isEnabled()
    assert page.stop_button.isEnabled()
    page.close()


def test_can_box_blocks_motion_but_keeps_emergency_stop_available(monkeypatch) -> None:
    state, page = _connected_can_box_page()
    captured: list[str] = []
    state.action_requested.connect(lambda action, _payload: captured.append(action))
    monkeypatch.setattr(
        "d7_factory_studio.ui.pages.motor.QMessageBox.warning",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    page.velocity_button.click()

    assert captured == []
    assert not page.modes.isEnabled()
    assert page.stop_button.isEnabled()

    page.stop_button.click()
    assert captured == ["motor.emergency_stop"]
    assert not page.stop_button.isEnabled()
    assert page.control_status.text() == "正在安全停止…"

    state.notify_task("motor.emergency_stop", "started", {})
    state.notify_task("motor.emergency_stop", "succeeded", {"motors": [1]})
    assert page.control_status.text() == "已安全停止"

    assert page.control_status.text() == "已安全停止"
    assert not page.modes.isEnabled()
    page.close()


def test_can_box_connection_failure_can_be_retried() -> None:
    _app()
    state = ApplicationState()
    page = CanControlPage(state, MemorySettings())  # type: ignore[arg-type]
    page.source.setCurrentIndex(page.source.findData(ConnectionMode.PC_DIRECT.value))
    captured: list[str] = []
    state.action_requested.connect(lambda action, _payload: captured.append(action))

    page.connect_can.click()
    page.connect_can.click()
    assert captured == ["connection.connect"]
    assert not page.connect_can.isEnabled()

    state.set_link_state(LinkState.FAULT, "adapter unavailable")
    state.notify_task(
        "connection.connect",
        "failed",
        {"error": "adapter unavailable"},
    )
    assert page.connect_can.isEnabled()
    assert "连接失败" in page.control_status.text()

    page.connect_can.click()
    assert captured == ["connection.connect", "connection.connect"]
    page.close()


def test_can_box_connect_uses_selected_physical_channel() -> None:
    _app()
    state = ApplicationState()
    page = CanControlPage(state, MemorySettings({"zlg/channel": 1}))  # type: ignore[arg-type]
    page.source.setCurrentIndex(page.source.findData(ConnectionMode.PC_DIRECT.value))
    captured: list[tuple[str, object]] = []
    state.action_requested.connect(lambda action, payload: captured.append((action, payload)))

    assert page.can_channel.currentData() == 1
    assert page.connect_can.text() == "打开 CH1"
    page.connect_can.click()

    action, payload = captured[-1]
    assert action == "connection.connect"
    assert payload["channel"] == 1
    assert page.source_status.text() == "CAN 盒 CH1 连接中…"
    assert not page.can_channel.isEnabled()
    page.close()


def test_can_box_background_heartbeat_fault_is_visible() -> None:
    state, page = _connected_can_box_page()

    state.notify_task(
        "motor.local_can_safety",
        "failed",
        {"error": "位置心跳发送失败，已双发失能"},
    )

    assert page.control_status.text() == "安全保护：位置心跳发送失败，已双发失能"
    assert "#C43F45" in page.control_status.styleSheet()
    assert "[失败] local_can_safety" in page.console.toPlainText()
    page.close()


def test_orin_controls_are_not_locked_by_can_box_state() -> None:
    _app()
    state = ApplicationState()
    page = CanControlPage(state, MemorySettings())  # type: ignore[arg-type]
    state.set_connection_mode(ConnectionMode.ORIN_REMOTE)
    state.set_link_state(LinkState.CONNECTED)
    page._can_box_pending_actions.add("motor.enable")
    page._refresh_source()

    assert page._selected_mode() is ConnectionMode.ORIN_REMOTE
    assert page._operation_buttons["enable"].isEnabled()
    assert page._operation_buttons["release_control"].isEnabled()
    assert page._operation_buttons["release_control"].toolTip() == ""
    assert page.control_status.isHidden()
    page.close()


def test_orin_single_target_probe_displays_real_online_status() -> None:
    _app()
    state = ApplicationState()
    page = CanControlPage(state, MemorySettings())  # type: ignore[arg-type]
    state.set_connection_mode(ConnectionMode.ORIN_REMOTE)
    state.set_link_state(LinkState.CONNECTED)
    page.target.setCurrentIndex(_target_index(page, ("motor", 13)))
    captured: list[tuple[str, object]] = []
    state.action_requested.connect(lambda action, payload: captured.append((action, payload)))

    page._probe_target()

    assert captured[-1] == (
        "motor.probe",
        {"source": "orin_remote", "target": {"motor": 13}},
    )
    state.notify_task(
        "motor.probe",
        "succeeded",
        {
            "source": "orin_remote",
            "target": {"motor": 13},
            "total": 1,
            "online": [13],
            "missing": [],
        },
    )

    assert page.source_status.text() == "Orin 已连接"
    assert page.target_status.text() == "节点状态：已连接 · 使能：未知 · 控制权：未获取"
    assert "#16845B" in page.target_status.styleSheet()
    page.close()


def test_can_box_group_probe_only_lists_broadcast_response_ids() -> None:
    state, page = _connected_can_box_page()
    group_target = page.target.currentData()
    assert group_target[0] == "group"

    page._probe_target()
    state.notify_task(
        "motor.probe",
        "succeeded",
        {
            "source": "pc_direct",
            "target": {group_target[0]: group_target[1]},
            "total": 8,
            "online": [7, 8, 9],
            "missing": [10, 11, 12, 13, 14],
            "response_ids": [0x23, 0x21, 0x22, 0x01],
        },
    )

    assert page.target_status.text() == "广播应答：0x01、0x21、0x22、0x23"
    assert page.authority_status.isHidden()
    assert page.control_status.isHidden()
    assert "缺失" not in page.target_status.text()
    page.close()


def test_can_box_single_probe_shows_connection_enable_and_authority() -> None:
    state, page = _connected_can_box_page()
    page.target.setCurrentIndex(_target_index(page, ("motor", 11)))
    page._authority_target = page.target.currentData()
    page._probe_target()
    state.notify_task(
        "motor.probe",
        "succeeded",
        {
            "source": "pc_direct",
            "target": {"motor": 11},
            "total": 1,
            "online": [11],
            "missing": [],
            "response_ids": [0x25],
            "statuses": {"11": 0x42},
        },
    )

    assert page.target_status.text() == "节点状态：已连接 · 使能：使能（0x42） · 控制权：CAN（1）"
    assert page.authority_status.isHidden()
    assert not page.control_status.isHidden()
    page.close()


def test_can_control_prepare_card_does_not_force_horizontal_overflow() -> None:
    _app()
    page = CanControlPage(ApplicationState(), MemorySettings())  # type: ignore[arg-type]
    page.resize(700, 720)
    page.show()
    QApplication.processEvents()

    assert page.control_card.minimumSizeHint().width() <= page.viewport().width()
    page.close()


def test_group_probe_lists_missing_logic_ids_instead_of_marking_group_connected() -> None:
    _app()
    state = ApplicationState()
    page = CanControlPage(state, MemorySettings())  # type: ignore[arg-type]
    state.set_connection_mode(ConnectionMode.ORIN_REMOTE)
    state.set_link_state(LinkState.CONNECTED)
    group_target = page.target.currentData()
    assert group_target[0] == "group"

    page._probe_target()
    state.notify_task(
        "motor.probe",
        "succeeded",
        {
            "source": "orin_remote",
            "target": {group_target[0]: group_target[1]},
            "total": 8,
            "online": [7, 8, 9, 10, 11, 12, 14],
            "missing": [13],
        },
    )

    assert page.target_status.text() == "目标状态：在线 7/8 · 缺失逻辑 ID 13"
    assert "#C43F45" in page.target_status.styleSheet()
    page.close()


def test_stale_probe_result_does_not_mark_new_target_online() -> None:
    _app()
    state = ApplicationState()
    page = CanControlPage(state, MemorySettings())  # type: ignore[arg-type]
    state.set_connection_mode(ConnectionMode.ORIN_REMOTE)
    state.set_link_state(LinkState.CONNECTED)
    page.target.setCurrentIndex(_target_index(page, ("motor", 13)))
    page._probe_target()
    page.target.setCurrentIndex(_target_index(page, ("motor", 14)))

    state.notify_task(
        "motor.probe",
        "succeeded",
        {
            "source": "orin_remote",
            "target": {"motor": 13},
            "total": 1,
            "online": [13],
            "missing": [],
        },
    )

    assert page.target_status.text() == "节点状态：未检测"
    assert "已忽略上一目标" in page.console.toPlainText()
    page.close()
