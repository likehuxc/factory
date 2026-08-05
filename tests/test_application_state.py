from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from d7_factory_studio.application import ApplicationState
from d7_factory_studio.core.models import ConnectionMode, LinkState


def test_unlock_requires_connected_session() -> None:
    state = ApplicationState()
    with pytest.raises(RuntimeError, match="未连接"):
        state.unlock()


def test_mode_evt_and_interface_changes_relock() -> None:
    state = ApplicationState()
    state.set_link_state(LinkState.CONNECTED)
    state.unlock()
    assert state.safety_locked is False

    state.set_connection_mode(ConnectionMode.ORIN_REMOTE)
    assert state.safety_locked is True
    assert state.link_state is LinkState.DISCONNECTED

    state.set_link_state(LinkState.CONNECTED)
    state.unlock()
    state.set_evt("EVT1")
    assert state.safety_locked is True
    assert state.link_state is LinkState.DISCONNECTED

    state.set_link_state(LinkState.CONNECTED)
    state.unlock()
    state.set_interface("can1")
    assert state.safety_locked is True


def test_fault_always_relocks() -> None:
    state = ApplicationState()
    state.set_link_state(LinkState.CONNECTED)
    state.unlock()
    state.report_fault("bus-off")
    assert state.safety_locked is True
    assert state.link_state is LinkState.FAULT
    assert state.fault_message == "bus-off"
