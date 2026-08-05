from __future__ import annotations

import os

import pytest

from d7_factory_studio.core.models import CanMode
from d7_factory_studio.features.serial485.service import Serial485Config, Serial485Service
from d7_factory_studio.transports.zlg.driver import ZlgCanTransport


@pytest.mark.skipif(
    os.getenv("D7_RUN_LOCAL_HARDWARE_TESTS") != "1", reason="explicit hardware opt-in required"
)
def test_usbcanfd_200u_can_be_opened() -> None:
    transport = ZlgCanTransport()
    try:
        transport.open(int(os.getenv("D7_CAN_CHANNEL", "0")), CanMode.FD)
        assert transport.is_open
    finally:
        transport.close()


@pytest.mark.skipif(
    os.getenv("D7_RUN_LOCAL_HARDWARE_TESTS") != "1" or not os.getenv("D7_SERIAL_PORT"),
    reason="explicit serial hardware opt-in and D7_SERIAL_PORT are required",
)
def test_serial485_port_can_be_opened() -> None:
    service = Serial485Service(Serial485Config(os.environ["D7_SERIAL_PORT"]))
    try:
        service.open()
        assert service.is_open
    finally:
        service.close()
