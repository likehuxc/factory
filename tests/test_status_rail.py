from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from d7_factory_studio.app import create_application
from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import ConnectionMode, LinkState
from d7_factory_studio.settings_store import ssh_fingerprint_key
from d7_factory_studio.ui.theme import stylesheet
from d7_factory_studio.ui.widgets import StatusLight, StatusRail


class MemorySettings:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = dict(values or {})

    def value(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set_value(self, key: str, value: object) -> None:
        self.values[key] = value


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = create_application([])
    instance.setStyleSheet(stylesheet())
    return instance


@pytest.fixture
def rail(app: QApplication, monkeypatch: pytest.MonkeyPatch) -> StatusRail:
    monkeypatch.setattr(
        "d7_factory_studio.ui.widgets.list_ports.comports",
        lambda: [SimpleNamespace(device="COM7"), SimpleNamespace(device="COM12")],
    )
    widget = StatusRail(
        ApplicationState(),
        MemorySettings({"ssh/host": "192.168.1.100", "serial485/baud": "57600"}),
    )
    yield widget
    widget.close()
    app.processEvents()


def test_evt_selector_keeps_both_variants_and_updates_state(rail: StatusRail) -> None:
    assert [rail.evt_combo.itemText(index) for index in range(rail.evt_combo.count())] == [
        "EVT2",
        "EVT1",
    ]

    rail.evt_combo.setCurrentText("EVT1")

    assert rail.state.evt.variant == "EVT1"


def test_orin_connect_saves_host_and_emits_complete_payload(rail: StatusRail) -> None:
    captured: list[tuple[str, object]] = []
    rail.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    rail.state.set_evt("EVT1")
    rail.orin_host.setEditText("10.10.8.21 ")

    rail.orin_button.click()

    assert rail.settings.value("ssh/host") == "10.10.8.21"
    assert rail.settings.value("ssh/host_history") == ["10.10.8.21", "192.168.1.100"]
    assert rail.orin_host.isEditable()
    assert rail.orin_host.itemText(0) == "10.10.8.21"
    assert rail.state.connection_mode is ConnectionMode.ORIN_REMOTE
    assert rail.state.link_state is LinkState.CONNECTING
    assert captured[-1] == (
        "connection.connect",
        {"mode": "orin_remote", "host": "10.10.8.21", "evt": "EVT1"},
    )


def test_connected_orin_button_only_requests_disconnect(rail: StatusRail) -> None:
    captured: list[tuple[str, object]] = []
    rail.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    rail.state.set_connection_mode(ConnectionMode.ORIN_REMOTE)
    rail.state.set_link_state(LinkState.CONNECTED)

    rail.orin_button.click()

    assert captured == [("connection.disconnect", {})]
    assert rail.orin_status.property("statusTone") == "warning"
    assert rail.orin_status.text_label.text() == "Orin 断开中"


@pytest.mark.parametrize(
    ("link_state", "text", "tone"),
    [
        (LinkState.DISCONNECTED, "Orin 未连接", "neutral"),
        (LinkState.CONNECTING, "Orin 连接中", "warning"),
        (LinkState.CONNECTED, "Orin 已连接", "success"),
        (LinkState.FAULT, "Orin 失败", "danger"),
    ],
)
def test_orin_light_maps_application_link_state(
    rail: StatusRail, link_state: LinkState, text: str, tone: str
) -> None:
    rail.state.set_connection_mode(ConnectionMode.ORIN_REMOTE)
    rail.state.set_link_state(link_state)

    assert rail.orin_status.text_label.text() == text
    assert rail.orin_status.property("statusTone") == tone


def test_only_successful_connect_and_open_show_completion_dialogs(
    rail: StatusRail, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, message: messages.append((title, message)),
    )

    rail.state.set_connection_mode(ConnectionMode.ORIN_REMOTE)
    rail.state.notify_task("connection.connect", "succeeded", {})
    rail.state.notify_task("connection.disconnect", "succeeded", {})
    rail.state.notify_task("serial485.open", "succeeded", {})
    rail.state.notify_task("serial485.close", "succeeded", {})

    assert messages == [
        ("Orin 已连接", "Orin 连接成功。"),
        ("485 已打开", "485 通信已打开。"),
    ]


def test_unknown_orin_fingerprint_can_be_confirmed_and_retried(
    rail: StatusRail, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, object]] = []
    rail.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    rail.orin_host.setEditText("192.168.140.87")
    rail.state.set_connection_mode(ConnectionMode.ORIN_REMOTE)

    rail.state.notify_task(
        "connection.connect",
        "failed",
        {"error": "192.168.140.87 的 SSH 主机指纹尚未确认: SHA256:test-key"},
    )

    assert rail.settings.value(ssh_fingerprint_key("192.168.140.87")) == "SHA256:test-key"
    assert rail.state.link_state is LinkState.CONNECTING
    assert captured[-1] == (
        "connection.connect",
        {"mode": "orin_remote", "host": "192.168.140.87", "evt": "EVT2"},
    )


def test_serial_button_uses_saved_baud_and_tracks_task_state(
    rail: StatusRail, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    captured: list[tuple[str, object]] = []
    rail.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    rail.serial_port.setCurrentText("COM12")

    rail.serial_button.click()
    assert captured[-1] == ("serial485.open", {"port": "COM12", "baud": 57600})
    assert rail.serial_status.property("statusTone") == "warning"
    assert not rail.serial_button.isEnabled()

    rail.state.notify_task("serial485.open", "succeeded", {})
    assert rail.serial_status.property("statusTone") == "success"
    assert rail.serial_status.text_label.text() == "485 已打开"
    assert rail.serial_button.text() == "关闭"
    # Refresh stays available while connected so unplugging the adapter can be
    # detected immediately instead of waiting for the periodic presence check.
    assert rail.serial_scan_button.isEnabled()

    rail.serial_button.click()
    assert captured[-1] == ("serial485.close", {})
    rail.state.notify_task("serial485.close", "failed", {"error": "端口忙"})
    assert rail.serial_status.property("statusTone") == "danger"
    assert rail.serial_status.toolTip() == "端口忙"
    assert rail.serial_button.text() == "关闭"

    rail.state.notify_task("serial485.close", "succeeded", {})
    assert rail.serial_status.property("statusTone") == "neutral"
    assert rail.serial_status.text_label.text() == "485 未打开"
    assert rail.serial_button.text() == "打开"
    assert rail.serial_scan_button.isEnabled()

    rail.settings.values.pop("serial485/baud")
    rail.serial_button.click()
    assert captured[-1] == ("serial485.open", {"port": "COM12", "baud": 115200})


def test_serial_open_failure_can_retry_same_port_immediately(rail: StatusRail) -> None:
    captured: list[tuple[str, object]] = []
    rail.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    rail.serial_port.setCurrentText("COM12")

    rail.serial_button.click()
    rail.state.notify_task("serial485.open", "failed", {"error": "端口暂时不可用"})

    assert rail.serial_button.isEnabled()
    assert rail.serial_port.isEnabled()
    assert rail.serial_button.text() == "打开"
    assert rail.serial_status.text_label.text() == "485 故障"

    rail.serial_button.click()

    assert captured == [
        ("serial485.open", {"port": "COM12", "baud": 57600}),
        ("serial485.open", {"port": "COM12", "baud": 57600}),
    ]
    assert rail.serial_status.text_label.text() == "485 打开中"
    assert rail.serial_status.toolTip() == ""


def test_serial_scan_refreshes_and_naturally_sorts_ports(
    rail: StatusRail, monkeypatch: pytest.MonkeyPatch
) -> None:
    popup_calls: list[bool] = []
    monkeypatch.setattr(
        "d7_factory_studio.ui.widgets.list_ports.comports",
        lambda: [SimpleNamespace(device="COM20"), SimpleNamespace(device="COM13")],
    )
    monkeypatch.setattr(rail.serial_port, "showPopup", lambda: popup_calls.append(True))
    rail.serial_port.setCurrentText("COM12")

    rail.serial_scan_button.click()

    assert [rail.serial_port.itemText(index) for index in range(rail.serial_port.count())] == [
        "COM13",
        "COM20",
    ]
    assert rail.serial_port.currentText() == "COM13"
    assert rail.serial_port.toolTip() == "检测到 2 个 485 串口"
    assert popup_calls == [True]


@pytest.mark.parametrize(
    ("answer", "expected_actions"),
    [
        (QMessageBox.StandardButton.No, []),
        (
            QMessageBox.StandardButton.Yes,
            [("diagnostics.network_forward_configure", {})],
        ),
    ],
)
def test_network_forwarding_requires_danger_confirmation(
    rail: StatusRail,
    monkeypatch: pytest.MonkeyPatch,
    answer: QMessageBox.StandardButton,
    expected_actions: list[tuple[str, object]],
) -> None:
    captured: list[tuple[str, object]] = []
    rail.state.action_requested.connect(lambda action, payload: captured.append((action, payload)))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: answer)

    rail.network_forward_button.click()

    assert captured == expected_actions


def test_status_light_supports_all_workstation_tones(app: QApplication) -> None:
    light = StatusLight()

    for tone in ("neutral", "warning", "success", "danger"):
        light.set_status(tone, tone)
        assert light.property("statusTone") == tone
        assert light.text_label.text() == tone

    light.close()
    app.processEvents()


def test_top_rail_fits_1180_window_content_width(rail: StatusRail, app: QApplication) -> None:
    available_width = 1180 - 228
    rail.resize(available_width, rail.height())
    rail.show()
    app.processEvents()

    assert rail.minimumSizeHint().width() <= available_width
    assert rail.network_forward_button.geometry().left() > rail.evt_combo.geometry().right()
    assert rail.orin_button.geometry().right() < rail.serial_port.geometry().left()
    assert rail.serial_button.geometry().right() < rail.width()
