from __future__ import annotations

import ipaddress
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from d7_factory_studio.core.ports import CancellationToken, RemoteCommandRequest, RemoteSession

INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class ForwardConfig:
    adb_path: Path
    wlan_device: str
    rk_ethernet_device: str
    orin_ethernet_device: str
    orin_ip: str
    forward_port: int = 22
    adb_serial: str = ""
    wait_seconds: int = 120

    def __post_init__(self) -> None:
        for name, value in (
            ("RK Wi-Fi", self.wlan_device),
            ("RK Ethernet", self.rk_ethernet_device),
            ("Orin Ethernet", self.orin_ethernet_device),
        ):
            if not INTERFACE_RE.fullmatch(value):
                raise ValueError(f"{name} 接口名无效: {value}")
        if self.adb_serial and not SERIAL_RE.fullmatch(self.adb_serial):
            raise ValueError("ADB serial 包含不支持的字符")
        ipaddress.IPv4Address(self.orin_ip)
        if not 1 <= self.forward_port <= 65535:
            raise ValueError("转发端口必须在 1..65535")


@dataclass(frozen=True, slots=True)
class NetworkInfo:
    wlan_ip: str
    wlan_prefix: int
    wlan_gateway: str
    ethernet_ip: str
    ethernet_prefix: int


Runner = Callable[[Sequence[str], int], ProcessResult]


def run_process(argv: Sequence[str], timeout_s: int) -> ProcessResult:
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def parse_ipv4(text: str) -> tuple[str, int] | None:
    match = re.search(r"\binet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+)(?:/(\d+))?", text)
    if not match:
        return None
    address = str(ipaddress.IPv4Address(match.group(1)))
    if match.group(2):
        return address, int(match.group(2))
    mask = re.search(r"(?:netmask\s+|Mask:)(\d+\.\d+\.\d+\.\d+)", text)
    return address, ipaddress.IPv4Network(f"0.0.0.0/{mask.group(1)}").prefixlen if mask else 24


def parse_gateway(text: str, address: str, prefix: int) -> str:
    match = re.search(r"\bdefault\s+via\s+(\d+\.\d+\.\d+\.\d+)", text)
    if match:
        return str(ipaddress.IPv4Address(match.group(1)))
    network = ipaddress.IPv4Interface(f"{address}/{prefix}").network
    if network.num_addresses < 2:
        raise ValueError("无法推断默认网关")
    return str(network.network_address + 1)


class AdbNetworkForwarder:
    """Configure the established RK3588-to-Orin forwarding path."""

    def __init__(
        self, config: ForwardConfig, orin_session: RemoteSession, runner: Runner = run_process
    ) -> None:
        self.config = config
        self.orin_session = orin_session
        self.runner = runner
        self.serial = config.adb_serial
        self.root_method = ""

    def _adb_argv(self, *args: str) -> list[str]:
        result = [str(self.config.adb_path)]
        if self.serial:
            result.extend(("-s", self.serial))
        return [*result, *args]

    def _run(self, argv: Sequence[str], timeout_s: int = 30) -> ProcessResult:
        result = self.runner(argv, timeout_s)
        if result.returncode:
            raise RuntimeError(
                result.stderr.strip() or result.stdout.strip() or f"命令失败: {result.returncode}"
            )
        return result

    def wait_for_device(self) -> str:
        if not self.config.adb_path.is_file():
            raise FileNotFoundError(f"找不到 ADB: {self.config.adb_path}")
        deadline = time.monotonic() + self.config.wait_seconds
        while time.monotonic() < deadline:
            devices = [
                line.split("\t", 1)[0]
                for line in self._run(self._adb_argv("devices"), 15).stdout.splitlines()
                if "\tdevice" in line
            ]
            if self.serial and self.serial in devices:
                return self.serial
            if not self.serial and len(devices) == 1:
                self.serial = devices[0]
                return self.serial
            if not self.serial and len(devices) > 1:
                raise RuntimeError("发现多个 ADB 设备，请指定 serial")
            time.sleep(1)
        raise TimeoutError("等待 ADB 设备超时")

    def _raw_shell(self, command: str, timeout_s: int = 30) -> ProcessResult:
        return self.runner(self._adb_argv("shell", command), timeout_s)

    def resolve_root(self) -> str:
        direct = self._raw_shell("id -u", 10)
        if direct.returncode == 0 and direct.stdout.strip() == "0":
            self.root_method = "direct"
            return self.root_method
        via_su = self._raw_shell("su -c id -u", 10)
        if via_su.returncode == 0 and via_su.stdout.strip() == "0":
            self.root_method = "su"
            return self.root_method
        root = self.runner(self._adb_argv("root"), 20)
        if root.returncode == 0:
            self.runner(self._adb_argv("wait-for-device"), 20)
            if self._raw_shell("id -u", 10).stdout.strip() == "0":
                self.root_method = "direct"
                return self.root_method
        raise PermissionError("无法获得 RK3588 root 权限")

    def adb_shell(self, command: str, *, root: bool = False, timeout_s: int = 30) -> ProcessResult:
        if root and (self.root_method or self.resolve_root()) == "su":
            import shlex

            command = "su -c " + shlex.quote(command)
        result = self._raw_shell(command, timeout_s)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "RK3588 命令失败")
        return result

    def read_network(self) -> NetworkInfo:
        wlan = parse_ipv4(self.adb_shell(f"ip -4 addr show dev {self.config.wlan_device}").stdout)
        ethernet = parse_ipv4(self.adb_shell(f"ip -4 addr show dev {self.config.rk_ethernet_device}").stdout)
        if wlan is None or ethernet is None:
            raise RuntimeError("无法读取 RK3588 网络地址")
        route = self.adb_shell(f"ip route show default dev {self.config.wlan_device}").stdout
        return NetworkInfo(wlan[0], wlan[1], parse_gateway(route, *wlan), ethernet[0], ethernet[1])

    def build_rk_commands(self, network: NetworkInfo) -> tuple[str, ...]:
        wlan, ethernet, orin, port = (
            self.config.wlan_device,
            self.config.rk_ethernet_device,
            self.config.orin_ip,
            self.config.forward_port,
        )

        def ensure(table: str, rule: str) -> str:
            option = f" -t {table}" if table else ""
            return f"iptables{option} -C {rule} 2>/dev/null || iptables{option} -A {rule}"

        return (
            f"ip route replace default via {network.wlan_gateway} dev {wlan}",
            "echo 1 > /proc/sys/net/ipv4/ip_forward",
            "echo 0 > /proc/sys/net/ipv4/conf/all/rp_filter",
            f"echo 0 > /proc/sys/net/ipv4/conf/{ethernet}/rp_filter",
            f"echo 0 > /proc/sys/net/ipv4/conf/{wlan}/rp_filter",
            ensure("nat", f"POSTROUTING -o {wlan} -j MASQUERADE"),
            ensure("", f"FORWARD -i {ethernet} -o {wlan} -j ACCEPT"),
            ensure("", f"FORWARD -i {wlan} -o {ethernet} -m state --state RELATED,ESTABLISHED -j ACCEPT"),
            (
                f"while iptables -t nat -D PREROUTING -p tcp --dport {port} "
                f"-j DNAT --to-destination {orin}:22 2>/dev/null; do :; done"
            ),
            f"iptables -t nat -A PREROUTING -p tcp --dport {port} -j DNAT --to-destination {orin}:22",
            ensure("nat", f"POSTROUTING -p tcp -d {orin} --dport 22 -j MASQUERADE"),
            ensure("", f"FORWARD -p tcp -d {orin} --dport 22 -m state --state NEW,ESTABLISHED -j ACCEPT"),
            ensure("", f"FORWARD -p tcp -s {orin} --sport 22 -m state --state ESTABLISHED -j ACCEPT"),
        )

    def execute(self, token: CancellationToken) -> dict[str, object]:
        token.raise_if_cancelled()
        self.wait_for_device()
        network = self.read_network()
        self.resolve_root()
        for command in self.build_rk_commands(network):
            token.raise_if_cancelled()
            self.adb_shell(command, root=True, timeout_s=40)
        route = self.orin_session.execute(
            RemoteCommandRequest(
                (
                    "sudo",
                    "ip",
                    "route",
                    "replace",
                    "default",
                    "via",
                    network.ethernet_ip,
                    "dev",
                    self.config.orin_ethernet_device,
                ),
                evidence_label="orin-default-route",
            ),
            token,
        )
        if route.returncode:
            raise RuntimeError(route.stderr.strip() or "配置 Orin 默认路由失败")
        return {"adb_serial": self.serial, "network": network, "forward_port": self.config.forward_port}
