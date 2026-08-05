from __future__ import annotations

import json

import pytest

from d7_factory_studio.features.motor.orin_service import OrinMotorService
from d7_factory_studio.transports.orin_agent import AgentProtocolError, JsonlDecoder


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def request(self, operation, *, target=None, args=None, timeout_s=15.0, request_id=None):
        self.calls.append((operation, target, args))
        if operation == "zero.prepare":
            return {"token": "zero:r1:1"}
        return {}


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


def test_emergency_stop_orders_zero_before_disable() -> None:
    client = FakeAgent()
    service = OrinMotorService(client)
    service.emergency_stop({"motors": [1, 2]})
    assert [call[0] for call in client.calls] == ["motor.set_velocity", "motor.disable"]
    assert client.calls[0][2]["rad_s"] == 0.0
