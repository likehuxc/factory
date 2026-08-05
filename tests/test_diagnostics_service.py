from __future__ import annotations

from pathlib import Path

import pytest

from d7_factory_studio.core.evt import load_builtin_evt
from d7_factory_studio.core.ports import (
    CancellationToken,
    RemoteCommandRequest,
    RemoteCommandResult,
    RemoteFileInfo,
    RemoteSession,
)
from d7_factory_studio.features.diagnostics.models import DiagnosticProfile
from d7_factory_studio.features.diagnostics.service import DiagnosticOptions, DiagnosticService


class FakeSession(RemoteSession):
    def __init__(self) -> None:
        self.requests: list[RemoteCommandRequest] = []

    def connect(self) -> None: ...
    def close(self) -> None: ...

    def execute(self, request, token, on_output=None):
        self.requests.append(request)
        if request.evidence_label == "socketcan-probe":
            output = (
                "1: lo: <LOOPBACK>\n"
                "2: can4: <NOARP>\n    link/can promiscuity 0\n"
                "3: can99: <NOARP>\n    link/can promiscuity 0\n"
            )
        elif request.evidence_label.endswith("custom"):
            output = """__D7_SECTION_BEGIN__:ip_before
can <FD> state ERROR-ACTIVE
berr-counter tx 0 rx 0
__D7_SECTION_END__:ip_before
__D7_SECTION_BEGIN__:ip_after
can <FD> state ERROR-ACTIVE
berr-counter tx 0 rx 0
__D7_SECTION_END__:ip_after
__D7_SECTION_BEGIN__:dmesg_before
old
__D7_SECTION_END__:dmesg_before
__D7_SECTION_BEGIN__:dmesg_after
old
__D7_SECTION_END__:dmesg_after
__D7_SECTION_BEGIN__:candump
normal frame
__D7_SECTION_END__:candump
__D7_SECTION_BEGIN__:generator_out
sent
__D7_SECTION_END__:generator_out
__D7_SECTION_BEGIN__:generator_err

__D7_SECTION_END__:generator_err
__D7_GENERATOR_RC__:0
"""
        elif request.evidence_label == "broadcast-can2":
            output = "(1.0) can2 151##1AABB\n__SEND_ERROR_COUNT__:0\n"
        elif request.evidence_label.startswith("node-"):
            output = "Succeeded: 1\nFailed: 0\n"
        else:
            output = ""
        return RemoteCommandResult(0, output, "", 0.01)

    def list_files(self, remote_root: str) -> list[RemoteFileInfo]:
        return []

    def download_files(self, entries, destination: Path, token):
        return []


def test_probe_filters_interfaces_not_in_evt() -> None:
    session = FakeSession()
    service = DiagnosticService(load_builtin_evt("EVT2"), session)
    assert service.probe_interfaces(CancellationToken()) == ("can4",)


def test_custom_diagnostic_produces_structured_result() -> None:
    session = FakeSession()
    service = DiagnosticService(load_builtin_evt("EVT2"), session)
    result = service.run(
        ("can4",),
        DiagnosticOptions(
            DiagnosticProfile.CUSTOM, fixed_can_id=0x6A5, duration_s=1, configure_interface=False
        ),
        CancellationToken(),
    )
    assert result["evt"]["variant"] == "EVT2"
    assert result["evaluation"]["verdict"] == "PASS"
    assert result["stages"][0]["interface"] == "can4"


def test_dmesg_clear_requires_explicit_high_risk_confirmation() -> None:
    service = DiagnosticService(load_builtin_evt("EVT2"), FakeSession())
    with pytest.raises(PermissionError, match="高风险"):
        service.run(
            ("can4",),
            DiagnosticOptions(DiagnosticProfile.CUSTOM, 0x6A5, clear_dmesg=True),
            CancellationToken(),
        )


def test_node_parameter_and_broadcast_services_execute_plans() -> None:
    session = FakeSession()
    service = DiagnosticService(load_builtin_evt("EVT2"), session)
    nodes = service.run_node_parameters(
        "/opt/actuator_sdk/tool", CancellationToken(), logic_ids=(23,), write_then_read=False
    )
    assert nodes["verdict"] == "PASS"
    assert nodes["nodes"][0]["bus"] == "can2"
    assert all(" -w " not in " ".join(request.argv) for request in session.requests)
    broadcast = service.run_broadcast("can2", 1, CancellationToken())
    assert set(broadcast["responses"]) == {23}
    assert broadcast["send_error_count"] == 0
