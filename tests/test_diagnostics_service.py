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
from d7_factory_studio.features.diagnostics.broadcast import LIVE_RESPONSE_PREFIX
from d7_factory_studio.features.diagnostics.models import DiagnosticProfile
from d7_factory_studio.features.diagnostics.service import (
    DiagnosticOptions,
    DiagnosticService,
    diagnostic_raw_artifacts,
)


class FakeSession(RemoteSession):
    def __init__(
        self,
        *,
        missing_tools: bool = False,
        fail_configuration: bool = False,
        empty_stage: bool = False,
    ) -> None:
        self.requests: list[RemoteCommandRequest] = []
        self.missing_tools = missing_tools
        self.fail_configuration = fail_configuration
        self.empty_stage = empty_stage

    def connect(self) -> None: ...
    def close(self) -> None: ...

    def execute(self, request, token, on_output=None):
        self.requests.append(request)
        returncode = 0
        if request.evidence_label == "socketcan-probe":
            output = (
                "1: lo: <LOOPBACK>\n"
                "2: can4: <NOARP>\n    link/can promiscuity 0\n"
                "3: can99: <NOARP>\n    link/can promiscuity 0\n"
            )
        elif request.evidence_label == "required-tools" and self.missing_tools:
            returncode = 127
            output = ""
        elif request.evidence_label.endswith("-configure") and self.fail_configuration:
            returncode = 1
            output = ""
        elif request.evidence_label in {
            "can4-custom",
            "can4-fixed_64b_1ms",
            "can4-fixed_64b_0gap",
            "can4-random_length_fd_brs",
        } and not self.empty_stage:
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
        if on_output is not None and request.stream_output and output:
            on_output(output, False)
        return RemoteCommandResult(returncode, output, "", 0.01)

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
    assert result["execution"] == {
        "status": "complete",
        "planned_stage_count": 1,
        "executed_stage_count": 1,
    }
    assert result["stages"][0]["interface"] == "can4"


def test_diagnostic_reports_stage_progress_and_real_verdict() -> None:
    progress: list[tuple[int, str]] = []
    service = DiagnosticService(load_builtin_evt("EVT2"), FakeSession(empty_stage=True))
    result = service.run(
        ("can4",),
        DiagnosticOptions(
            DiagnosticProfile.CUSTOM,
            fixed_can_id=0x6A5,
            duration_s=1,
            configure_interface=False,
        ),
        CancellationToken(),
        on_progress=lambda value, message: progress.append((value, message)),
    )
    assert result["evaluation"]["verdict"] == "FAIL"
    assert any("未收到完整阶段证据" in item for item in result["evaluation"]["findings"])
    assert progress[0] == (1, "正在检查 Orin 诊断环境")
    assert any("custom 开始" in message for _, message in progress)
    assert progress[-1] == (100, "链路测试完成：FAIL")


def test_missing_tools_and_configuration_failure_never_run_traffic() -> None:
    missing_session = FakeSession(missing_tools=True)
    missing = DiagnosticService(load_builtin_evt("EVT2"), missing_session).run(
        ("can4",),
        DiagnosticOptions(DiagnosticProfile.CUSTOM, 0x6A5, duration_s=1),
        CancellationToken(),
    )
    assert missing["evaluation"]["verdict"] == "FAIL"
    assert missing["execution"] == {
        "status": "not_started",
        "planned_stage_count": 1,
        "executed_stage_count": 0,
    }
    assert not any(request.evidence_label.endswith("custom") for request in missing_session.requests)

    configuration_session = FakeSession(fail_configuration=True)
    configured = DiagnosticService(load_builtin_evt("EVT2"), configuration_session).run(
        ("can4",),
        DiagnosticOptions(DiagnosticProfile.CUSTOM, 0x6A5, duration_s=1),
        CancellationToken(),
    )
    assert configured["evaluation"]["verdict"] == "FAIL"
    assert not any(request.evidence_label.endswith("custom") for request in configuration_session.requests)


def test_sudo_password_is_stdin_only_and_raw_evidence_can_be_bundled() -> None:
    session = FakeSession()
    result = DiagnosticService(load_builtin_evt("EVT2"), session).run(
        ("can4",),
        DiagnosticOptions(DiagnosticProfile.CUSTOM, 0x6A5, duration_s=1),
        CancellationToken(),
        sudo_password="secret",
    )
    privileged = [request for request in session.requests if request.argv[:2] == ("sudo", "-S")]
    assert privileged and all(request.stdin_secret == "secret" for request in privileged)
    assert "secret" not in str(result)
    artifacts = diagnostic_raw_artifacts(result)
    assert any(path.endswith("candump.log") for path in artifacts)
    assert any(path.startswith("commands/") for path in artifacts)


def test_dmesg_clear_requires_explicit_high_risk_confirmation() -> None:
    service = DiagnosticService(load_builtin_evt("EVT2"), FakeSession())
    with pytest.raises(PermissionError, match="高风险"):
        service.run(
            ("can4",),
            DiagnosticOptions(DiagnosticProfile.CUSTOM, 0x6A5, clear_dmesg=True),
            CancellationToken(),
        )


def test_quick_chain_clears_dmesg_then_runs_setup_in_home_before_each_stage() -> None:
    session = FakeSession()
    output: list[str] = []
    result = DiagnosticService(load_builtin_evt("EVT2"), session).run(
        ("can4",),
        DiagnosticOptions(
            DiagnosticProfile.QUICK,
            0x6A5,
            setup_can_script="~/setup_can.sh",
            setup_each_stage=True,
            clear_dmesg=True,
            high_risk_dmesg_clear_confirmed=True,
        ),
        CancellationToken(),
        on_output=lambda message, _is_error: output.append(message),
        sudo_password="secret",
    )

    labels = [request.evidence_label for request in session.requests]
    assert labels[0] == "HIGH-RISK-clear-dmesg"
    assert labels.count("setup-can") == 3
    stage_labels = [
        "can4-fixed_64b_1ms",
        "can4-fixed_64b_0gap",
        "can4-random_length_fd_brs",
    ]
    for stage_label in stage_labels:
        stage_index = labels.index(stage_label)
        assert labels[stage_index - 5 : stage_index] == [
            "setup-can",
            "can4-down",
            "can4-configure",
            "can4-up",
            "can4-verify",
        ]
    setup = next(request for request in session.requests if request.evidence_label == "setup-can")
    assert setup.argv == (
        "sh",
        "-lc",
        'cd -- "$HOME/$1" && sudo -S -p "" -- "./$2"',
        "d7-setup",
        "",
        "setup_can.sh",
    )
    assert result["evaluation"]["verdict"] == "PASS"
    assert result["stages"][0]["candump"] == "normal frame"
    assert any("采集 1 帧" in message for message in output)
    assert not any("normal frame" in message for message in output)


def test_node_parameter_and_broadcast_services_execute_plans() -> None:
    session = FakeSession()
    service = DiagnosticService(load_builtin_evt("EVT2"), session)
    nodes = service.run_node_parameters(
        "/opt/actuator_sdk/tool", CancellationToken(), logic_ids=(23,), write_then_read=False
    )
    assert nodes["verdict"] == "PASS"
    assert nodes["nodes"][0]["bus"] == "can2"
    assert all(" -w " not in " ".join(request.argv) for request in session.requests)
    assert any("test -x /opt/actuator_sdk/tool" in request.argv[-1] for request in session.requests)
    broadcast = service.run_broadcast("can2", 1, CancellationToken())
    assert set(broadcast["responses"]) == {23}
    assert broadcast["send_error_count"] == 0


def test_motor_broadcast_setup_and_live_counts_are_separate_from_frame_log() -> None:
    session = FakeSession()
    service = DiagnosticService(load_builtin_evt("EVT2"), session)
    service.prepare_motor_can(CancellationToken(), sudo_password="secret")
    setup = session.requests[-1]
    assert setup.evidence_label == "setup-can"
    assert "setup_can.sh" in setup.argv[-1]
    assert setup.stdin_secret == "secret"

    events: list[str] = []

    def execute(request, token, on_output=None):  # type: ignore[no-untyped-def]
        del token
        session.requests.append(request)
        output = "(1.0) can2 251##1AA\n(1.1) can2 251##1BB\n__SEND_ERROR_COUNT__:0\n"
        if on_output is not None:
            on_output(output[:24], False)
            on_output(output[24:], False)
        return RemoteCommandResult(0, output, "", 1.0)

    session.execute = execute  # type: ignore[method-assign]
    result = service.run_broadcast(
        "can2",
        1,
        CancellationToken(),
        on_output=lambda line, _error: events.append(line),
    )
    assert result["responses"][23]["count"] == 2
    assert result["response_frame_count"] == 2
    assert events[:2] == [
        f"{LIVE_RESPONSE_PREFIX}:can2:23:1:251",
        f"{LIVE_RESPONSE_PREFIX}:can2:23:2:251",
    ]
    assert not any("251##1" in event for event in events)


def test_motor_broadcast_send_error_fails_instead_of_reporting_zero_responses() -> None:
    session = FakeSession()

    def execute(request, token, on_output=None):  # type: ignore[no-untyped-def]
        del request, token, on_output
        return RemoteCommandResult(
            0,
            "__SEND_ERROR_COUNT__:3\n__SEND_ERROR__:write: No buffer space available\n",
            "",
            1.0,
        )

    session.execute = execute  # type: ignore[method-assign]
    service = DiagnosticService(load_builtin_evt("EVT2"), session)
    with pytest.raises(RuntimeError, match="广播发送失败 3 次"):
        service.run_broadcast("can2", 1, CancellationToken())


def test_node_parameter_write_runs_write_before_read() -> None:
    session = FakeSession()
    service = DiagnosticService(load_builtin_evt("EVT2"), session)

    result = service.run_node_parameters(
        "/opt/actuator_sdk/jihua_calib_param_factory",
        CancellationToken(),
        logic_ids=(7,),
        write_then_read=True,
    )

    node_requests = [request for request in session.requests if request.evidence_label.startswith("node-")]
    assert [request.evidence_label for request in node_requests] == ["node-write-7", "node-read-7"]
    assert " -w 7" in node_requests[0].argv[-1]
    assert " -r 7" in node_requests[1].argv[-1]
    assert result["nodes"][0]["operations"].keys() == {"write", "read"}
