from __future__ import annotations

import base64
import shlex
import textwrap

from d7_factory_studio.core.evt import CanInterfaceConfig, EvtConfig
from d7_factory_studio.core.models import CanMode
from d7_factory_studio.core.ports import RemoteCommandRequest

from .models import DiagnosticStage
from .profiles import safe_random_ids


def probe_request() -> RemoteCommandRequest:
    return RemoteCommandRequest(
        ("ip", "-details", "-statistics", "link", "show"), timeout_s=20, evidence_label="socketcan-probe"
    )


def environment_requests() -> tuple[RemoteCommandRequest, ...]:
    return (
        RemoteCommandRequest(("uname", "-a"), evidence_label="uname"),
        RemoteCommandRequest(("cat", "/etc/os-release"), evidence_label="os-release"),
        RemoteCommandRequest(("cat", "/proc/device-tree/model"), evidence_label="device-model"),
    )


def configure_requests(config: CanInterfaceConfig) -> tuple[RemoteCommandRequest, ...]:
    type_args = ["sudo", "ip", "link", "set", config.name, "type", "can", "bitrate", str(config.bitrate)]
    if config.sample_point is not None:
        type_args.extend(("sample-point", str(config.sample_point)))
    if config.mode is CanMode.FD:
        type_args.extend(("dbitrate", str(config.dbitrate), "fd", "on"))
        if config.data_sample_point is not None:
            type_args.extend(("dsample-point", str(config.data_sample_point)))
    else:
        type_args.extend(("fd", "off"))
    type_args.extend(("berr-reporting", "on", "restart-ms", str(config.restart_ms)))
    return (
        RemoteCommandRequest(
            ("sudo", "ip", "link", "set", config.name, "down"), evidence_label=f"{config.name}-down"
        ),
        RemoteCommandRequest(tuple(type_args), evidence_label=f"{config.name}-configure"),
        RemoteCommandRequest(
            ("sudo", "ip", "link", "set", config.name, "up"), evidence_label=f"{config.name}-up"
        ),
        RemoteCommandRequest(
            ("ip", "-details", "-statistics", "link", "show", config.name),
            evidence_label=f"{config.name}-verify",
        ),
    )


def clear_dmesg_request(*, high_risk_confirmed: bool = False) -> RemoteCommandRequest:
    if not high_risk_confirmed:
        raise PermissionError("dmesg -C 默认禁用；必须显式确认 high_risk_confirmed")
    return RemoteCommandRequest(("sudo", "dmesg", "-C"), timeout_s=20, evidence_label="HIGH-RISK-clear-dmesg")


def _safe_random_command(evt: EvtConfig, config: CanInterfaceConfig, stage: DiagnosticStage) -> str:
    source = textwrap.dedent(
        """
        import json, random, select, socket, struct, sys, time
        iface, duration, gap_ms, is_fd, ids_csv = sys.argv[1:]
        ids = tuple(int(value) for value in ids_csv.split(','))
        fd = is_fd == '1'; lengths = tuple(range(9)) + ((12,16,20,24,32,48,64) if fd else ())
        sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        if fd: sock.setsockopt(socket.SOL_CAN_RAW, 5, 1)
        sock.setblocking(False); sock.bind((iface,)); sent = blocked = 0; start = time.monotonic()
        try:
            while time.monotonic() - start < int(duration):
                can_id = random.choice(ids); length = random.choice(lengths)
                payload = bytes(random.getrandbits(8) for _ in range(length))
                layout = '=IBBBB64s' if fd else '=IBBBB8s'
                frame = struct.pack(layout, can_id, length, 1 if fd else 0, 0, 0,
                                    payload.ljust(64 if fd else 8, b'\\0'))
                try: sock.send(frame); sent += 1
                except BlockingIOError: blocked += 1; select.select([], [sock], [], .01)
                if int(gap_ms): time.sleep(int(gap_ms) / 1000)
        finally: sock.close()
        print(json.dumps({'sent': sent, 'blocked': blocked, 'duration_s': time.monotonic()-start}))
        """
    ).strip()
    encoded = base64.b64encode(source.encode()).decode()
    bootstrap = f"import base64;exec(base64.b64decode('{encoded}'))"
    ids = ",".join(str(item) for item in safe_random_ids(evt))
    return shlex.join(
        (
            "python3",
            "-c",
            bootstrap,
            config.name,
            str(stage.duration_s),
            str(stage.gap_ms),
            "1" if config.mode is CanMode.FD else "0",
            ids,
        )
    )


def _fixed_generator(config: CanInterfaceConfig, stage: DiagnosticStage) -> str:
    argv = ["timeout", f"{stage.duration_s}s", "cangen", config.name, "-g", str(stage.gap_ms), "-p", "10"]
    if config.mode is CanMode.FD:
        argv.extend(("-f", "-b"))
    argv.extend(("-I", f"{stage.can_id:X}", "-L", str(stage.payload_length), "-D", "i"))
    return shlex.join(argv)


def stage_request(evt: EvtConfig, config: CanInterfaceConfig, stage: DiagnosticStage) -> RemoteCommandRequest:
    generator = (
        _safe_random_command(evt, config, stage) if stage.random_frames else _fixed_generator(config, stage)
    )
    script = textwrap.dedent(
        f"""
        set +e
        work="$(mktemp -d)"; dump_pid=""
        cleanup() {{
          trap - EXIT INT TERM
          if [ -n "$dump_pid" ]; then kill -TERM "$dump_pid" 2>/dev/null; wait "$dump_pid" 2>/dev/null; fi
          rm -rf -- "$work"
        }}
        trap cleanup EXIT INT TERM
        dmesg --time-format iso >"$work/dmesg_before" 2>&1 || dmesg >"$work/dmesg_before" 2>&1
        ip -details -statistics link show {shlex.quote(config.name)} >"$work/ip_before" 2>&1
        timeout {stage.duration_s + 3}s candump -e -x -t A {shlex.quote(config.name)} \
          >"$work/candump" 2>&1 & dump_pid=$!
        sleep 0.3
        {generator} >"$work/generator_out" 2>"$work/generator_err"; generator_rc=$?
        sleep 0.3; kill -TERM "$dump_pid" 2>/dev/null; wait "$dump_pid" 2>/dev/null; dump_pid=""
        ip -details -statistics link show {shlex.quote(config.name)} >"$work/ip_after" 2>&1
        dmesg --time-format iso >"$work/dmesg_after" 2>&1 || dmesg >"$work/dmesg_after" 2>&1
        for section in ip_before ip_after dmesg_before dmesg_after candump generator_out generator_err; do
          echo "__D7_SECTION_BEGIN__:$section"; cat "$work/$section"; echo "__D7_SECTION_END__:$section"
        done
        echo "__D7_GENERATOR_RC__:$generator_rc"
        exit 0
        """
    ).strip()
    return RemoteCommandRequest(
        ("sh", "-lc", script),
        timeout_s=stage.duration_s + 20,
        stream_output=True,
        evidence_label=f"{config.name}-{stage.name}",
    )


def parse_stage_sections(stdout: str) -> tuple[dict[str, str], int]:
    sections: dict[str, str] = {}
    pattern = re_compile_sections()
    for match in pattern.finditer(stdout):
        sections[match.group("name")] = match.group("body").rstrip("\r\n")
    rc_match = __import__("re").search(
        r"^__D7_GENERATOR_RC__:(-?\d+)\s*$", stdout, __import__("re").MULTILINE
    )
    return sections, int(rc_match.group(1)) if rc_match else -1


def re_compile_sections():
    import re

    return re.compile(
        r"^__D7_SECTION_BEGIN__:(?P<name>[a-z_]+)\r?\n(?P<body>.*?)^__D7_SECTION_END__:\1\s*$",
        re.MULTILINE | re.DOTALL,
    )
