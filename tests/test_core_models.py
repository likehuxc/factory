from __future__ import annotations

import pytest

from d7_factory_studio.core.models import CanFrame


def test_classic_frame_forces_brs_off() -> None:
    frame = CanFrame(0x18, b"\x01", is_fd=False, bitrate_switch=True)
    assert frame.bitrate_switch is False


def test_standard_id_and_length_validation() -> None:
    with pytest.raises(ValueError):
        CanFrame(0x800)
    with pytest.raises(ValueError):
        CanFrame(0x12, bytes(9), is_fd=False)

