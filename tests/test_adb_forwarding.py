from __future__ import annotations

from pathlib import Path

import pytest

from d7_factory_studio.core.ports import RemoteSession
from d7_factory_studio.features.diagnostics.forwarding import (
    AdbNetworkForwarder,
    ForwardConfig,
    NetworkInfo,
    parse_gateway,
    parse_ipv4,
)


class UnusedSession(RemoteSession):
    def connect(self): ...
    def close(self): ...
    def execute(self, request, token, on_output=None):
        raise AssertionError("not used")

    def list_files(self, remote_root):
        return []

    def download_files(self, entries, destination, token):
        return []


def config() -> ForwardConfig:
    return ForwardConfig(
        adb_path=Path("adb.exe"),
        wlan_device="wlan0",
        rk_ethernet_device="eth0",
        orin_ethernet_device="eth0",
        orin_ip="10.254.254.1",
    )


def test_forward_network_parsers() -> None:
    assert parse_ipv4("inet 192.168.8.20/24 brd 192.168.8.255") == ("192.168.8.20", 24)
    assert parse_ipv4("inet addr:10.0.0.2 Mask:255.255.255.0") == ("10.0.0.2", 24)
    assert parse_gateway("default via 192.168.8.1 dev wlan0", "192.168.8.20", 24) == "192.168.8.1"
    assert parse_gateway("", "192.168.8.20", 24) == "192.168.8.1"


def test_forwarding_rules_are_idempotent_and_do_not_flush_firewall() -> None:
    forwarder = AdbNetworkForwarder(config(), UnusedSession())
    commands = forwarder.build_rk_commands(
        NetworkInfo("192.168.8.20", 24, "192.168.8.1", "10.254.254.10", 24)
    )
    text = "\n".join(commands)
    assert len(commands) == 13
    assert "--to-destination 10.254.254.1:22" in text
    assert "iptables -C" in text
    assert "iptables -F" not in text


def test_forward_config_rejects_shell_metacharacters() -> None:
    with pytest.raises(ValueError, match="接口名无效"):
        ForwardConfig(Path("adb.exe"), "wlan0; reboot", "eth0", "eth0", "10.0.0.1")
