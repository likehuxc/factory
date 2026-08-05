from __future__ import annotations

from pathlib import Path

import keyring
from PySide6.QtCore import QSettings

SERVICE_NAME = "D7 Factory Studio"


class SettingsStore:
    def __init__(self) -> None:
        self._settings = QSettings("Pudu Robotics", "D7 Factory Studio")

    def value(self, key: str, default: object = None) -> object:
        return self._settings.value(key, default)

    def set_value(self, key: str, value: object) -> None:
        self._settings.setValue(key, value)
        self._settings.sync()

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
