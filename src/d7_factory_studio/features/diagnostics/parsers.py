from __future__ import annotations

import re

from .models import DiagnosticEvaluation, DiagnosticVerdict, LinkSnapshot, StageEvidence

ERROR_KEYWORDS = (
    "ack error",
    "crc error",
    "can error frame",
    "bus error",
    "stuff error",
    "nominal-stuff",
    "form error",
    "bit error",
    "bit0 error",
    "bit1 error",
    "bus-off",
    "bus off",
    "error-passive",
    "error passive",
    "error-warning",
    "error warning",
    "errorframe",
)


def parse_can_interfaces(text: str) -> tuple[str, ...]:
    found: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        header = re.match(r"^\d+:\s+([^:@]+)", line)
        if header:
            current = header.group(1)
        elif current and " link/can " in line:
            found.append(current)
            current = None
    return tuple(
        sorted(
            set(found),
            key=lambda item: (not item[3:].isdigit(), int(item[3:]) if item[3:].isdigit() else item),
        )
    )


def parse_link_snapshot(text: str) -> LinkSnapshot:
    state_match = re.search(r"\bcan\s+<[^>]*>\s+state\s+([A-Z-]+)\b", text)
    if not state_match:
        state_match = re.search(r"\bstate\s+([A-Z-]+)\b", text)
    counters: dict[str, int] = {}
    berr = re.search(r"berr-counter\s+tx\s+(\d+)\s+rx\s+(\d+)", text)
    if berr:
        counters.update(berr_counter_tx=int(berr.group(1)), berr_counter_rx=int(berr.group(2)))
    can_stats = re.search(
        r"re-started\s+bus-errors\s+arbit-lost\s+error-warn\s+error-pass\s+bus-off\s*\n"
        r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
        text,
    )
    if can_stats:
        counters.update(
            zip(
                ("re_started", "bus_errors", "arbit_lost", "error_warn", "error_pass", "bus_off"),
                (int(value) for value in can_stats.groups()),
                strict=True,
            )
        )
    for direction, names in (
        ("RX", ("rx_bytes", "rx_packets", "rx_errors", "rx_dropped", "rx_missed", "rx_mcast")),
        ("TX", ("tx_bytes", "tx_packets", "tx_errors", "tx_dropped", "tx_carrier", "tx_collsns")),
    ):
        match = re.search(
            rf"{direction}:\s+bytes\s+packets\s+errors\s+dropped\s+\w+\s+\w+\s*\n"
            r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
            text,
        )
        if match:
            counters.update(zip(names, (int(value) for value in match.groups()), strict=True))
    return LinkSnapshot(state=state_match.group(1) if state_match else "UNKNOWN", counters=counters, raw=text)


def dmesg_suffix(before: str, after: str) -> str:
    if after.startswith(before):
        return after[len(before) :]
    before_lines, after_lines = before.splitlines(), after.splitlines()
    for size in range(min(len(before_lines), len(after_lines)), 0, -1):
        if before_lines[-size:] == after_lines[:size]:
            return "\n".join(after_lines[size:])
    return after


def extract_error_lines(text: str, interface: str | None = None) -> tuple[str, ...]:
    result: list[str] = []
    for line in text.splitlines():
        lowered = line.lower()
        if not any(keyword in lowered for keyword in ERROR_KEYWORDS):
            continue
        mentioned = set(re.findall(r"\b(?:v?can|slcan)\d+\b", lowered))
        if interface and mentioned and interface.lower() not in mentioned:
            continue
        result.append(line)
    return tuple(result)


def counter_delta(before: LinkSnapshot, after: LinkSnapshot, key: str) -> int:
    return max(0, after.counters.get(key, 0) - before.counters.get(key, 0))


def evaluate(
    evidence: tuple[StageEvidence, ...], command_failures: tuple[str, ...] = ()
) -> DiagnosticEvaluation:
    findings = list(command_failures)
    hints: list[str] = []
    failed = bool(command_failures)
    warned = False
    for item in evidence:
        name = f"{item.interface}/{item.stage.name}"
        if item.generator_returncode not in {0, 124} or item.cancelled:
            failed = True
            findings.append(f"{name}: 流量生成器未正常完成 (RC={item.generator_returncode})")
        if item.error_lines:
            failed = True
            findings.append(f"{name}: dmesg 或 candump 发现 CAN 错误")
            lowered = "\n".join(item.error_lines).lower()
            if "ack" in lowered:
                hints.append("ACK error：检查对端在线、ACK、CAN FD/BRS 与波特率配置")
            if any(word in lowered for word in ("crc", "stuff", "form")):
                hints.append("CRC/stuff/form error：检查数据段时序、采样点、TDC 与物理层裕量")
            if any(word in lowered for word in ("bit error", "bit0", "bit1")):
                hints.append("Bit error：检查终端电阻、线束、收发器与采样点")
        for key in (
            "berr_counter_tx",
            "berr_counter_rx",
            "bus_errors",
            "error_warn",
            "error_pass",
            "bus_off",
            "rx_errors",
            "tx_errors",
        ):
            delta = counter_delta(item.before, item.after, key)
            if delta:
                warned = True
                findings.append(f"{name}: {key} +{delta}")
        if item.after.state.upper() in {"BUS-OFF", "ERROR-PASSIVE"}:
            failed = True
            findings.append(f"{name}: 接口进入 {item.after.state.upper()}")
            hints.append("bus-off/error-passive：优先检查最早出现的驱动错误")
    verdict = (
        DiagnosticVerdict.FAIL if failed else DiagnosticVerdict.WARN if warned else DiagnosticVerdict.PASS
    )
    return DiagnosticEvaluation(verdict, tuple(dict.fromkeys(findings)), tuple(dict.fromkeys(hints)))
