from __future__ import annotations

import json

import pytest

from d7_factory_studio.core.models import CanFrame, CanMode
from d7_factory_studio.features.motor.orin_service import OrinMotorService
from d7_factory_studio.transports.orin_agent import (
    AgentCanTransport,
    AgentProtocolError,
    JsonlDecoder,
)


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def request(self, operation, *, target=None, args=None, timeout_s=15.0, request_id=None):
        self.calls.append((operation, target, args))
        if operation == "zero.prepare":
            return {"token": "zero:r1:1"}
        return {}


class FakeEventAgent(FakeAgent):
    is_running = True

    def __init__(self) -> None:
        super().__init__()
        self.listeners = []

    def add_event_listener(self, listener):
        self.listeners.append(listener)

    def remove_event_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)


def test_jsonl_decoder_accepts_split_messages() -> None:
    decoder = JsonlDecoder()
    assert decoder.feed(b'{"v":1,"type":"sta') == []
    messages = decoder.feed(b'te","seq":1}\n{"v":1,"type":"result","id":"r1","ok":true}\n')
    assert [message["type"] for message in messages] == ["state", "result"]


def test_jsonl_decoder_rejects_bad_version_and_oversize() -> None:
    decoder = JsonlDecoder(max_line_bytes=20)
    with pytest.raises(AgentProtocolError, match="大小限制"):
        decoder.feed(b"x" * 21)
    with pytest.raises(AgentProtocolError, match="v=1"):
        JsonlDecoder().feed(json.dumps({"v": 2}).encode() + b"\n")


def test_motor_service_enforces_factory_velocity_limit() -> None:
    client = FakeAgent()
    service = OrinMotorService(client)
    service.set_velocity({"group": "LEFT_ARM_HAND"}, 0.5, 1000)
    assert client.calls[-1] == (
        "motor.set_velocity",
        {"groups": ["LEFT_ARM_HAND"]},
        {"rad_s": 0.5, "accel_time_ms": 1000},
    )
    with pytest.raises(ValueError, match="±0.5"):
        service.set_velocity({"motor": 1}, 0.5001)
    with pytest.raises(ValueError, match="1..65535"):
        service.set_velocity({"motor": 1}, 0.1, 0)


def test_zero_calibration_requires_prepare_token() -> None:
    client = FakeAgent()
    service = OrinMotorService(client)
    target = {"motor": 1}
    with pytest.raises(RuntimeError, match="准备令牌"):
        service.zero_commit(target)
    assert service.zero_prepare(target) == "zero:r1:1"
    service.zero_commit(target)
    assert client.calls[-1] == ("zero.commit", None, {"token": "zero:r1:1"})


def test_zero_calibration_accepts_multiple_fixed_groups_as_motor_ids() -> None:
    client = FakeAgent()
    service = OrinMotorService(client)
    target = {"motors": [1, 2, 7, 8]}
    assert service.zero_prepare(target) == "zero:r1:1"
    assert client.calls[-1][1] == {"motors": [1, 2, 7, 8]}
    service.zero_commit(target)
    assert client.calls[-1] == ("zero.commit", None, {"token": "zero:r1:1"})


def test_control_authority_supports_fixed_group() -> None:
    client = FakeAgent()
    service = OrinMotorService(client)
    service.set_control_authority({"group": "LEFT_ARM_HAND"}, True)
    assert client.calls[-1] == (
        "motor.set_control_authority",
        {"groups": ["LEFT_ARM_HAND"]},
        {"owned": True},
    )


def test_emergency_stop_orders_zero_before_disable() -> None:
    client = FakeAgent()
    service = OrinMotorService(client)
    service.emergency_stop({"motors": [1, 2]})
    assert [call[0] for call in client.calls] == ["motor.set_velocity", "motor.disable"]
    assert client.calls[0][2]["rad_s"] == 0.0


def test_agent_can_transport_filters_bus_and_round_trips_frame() -> None:
    client = FakeEventAgent()
    transport = AgentCanTransport(client, "can5")
    transport.open(0, CanMode.CLASSIC)
    assert client.calls[-1][0] == "can.subscribe"
    client.listeners[0]({"v": 1, "type": "can.frame", "bus": "can2", "id": 1, "is_fd": False, "data": [1]})
    client.listeners[0](
        {"v": 1, "type": "can.frame", "bus": "can5", "id": 0x18, "is_fd": False, "data": [1, 2]}
    )
    assert transport.receive(0) == [
        CanFrame(0x18, b"\x01\x02", is_fd=False, bitrate_switch=False, timestamp=0)
    ]
    transport.send(CanFrame(0x18, b"\x03", is_fd=False))
    assert client.calls[-1] == (
        "can.send",
        None,
        {"unsafe": True, "bus": "can5", "id": 0x18, "is_fd": False, "data": [3]},
    )
    transport.close()
    assert client.listeners == []
