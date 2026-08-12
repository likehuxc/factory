from __future__ import annotations

import base64
from pathlib import Path

import keyring
from PySide6.QtCore import QSettings, QStandardPaths

SERVICE_NAME = "D7 Factory Studio"


def ssh_fingerprint_key(host: str) -> str:
    """Return a QSettings-safe key for one SSH host's pinned fingerprint."""
    normalized = host.strip().lower()
    encoded = base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")
    return f"ssh/fingerprints/{encoded}"


class SettingsStore:
    def __init__(self) -> None:
        self._settings = QSettings("Pudu Robotics", "D7 Factory Studio")

    def value(self, key: str, default: object = None) -> object:
        return self._settings.value(key, default)

    def set_value(self, key: str, value: object) -> None:
        self._settings.setValue(key, value)
        self._settings.sync()

    def bool_value(self, key: str, default: bool = False) -> bool:
        value = self.value(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def ssh_password(self, host: str, username: str) -> str | None:
        try:
            return keyring.get_password(SERVICE_NAME, f"{username}@{host}")
        except keyring.errors.KeyringError:
            return None

    def set_ssh_password(self, host: str, username: str, password: str) -> None:
        keyring.set_password(SERVICE_NAME, f"{username}@{host}", password)

    @property
    def report_directory(self) -> Path:
        return Path(str(self.value("paths/reports", Path.cwd() / "reports")))

    @property
    def app_config_directory(self) -> Path:
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
        return Path(location)

    @property
    def known_hosts_path(self) -> Path:
        return self.app_config_directory / "known_hosts"
