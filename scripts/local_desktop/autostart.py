from __future__ import annotations

import os
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from framework.shared.local_desktop import LocalDesktopConfig
from scripts.local_desktop.common import DesktopOperationError, OperationResult

_VALUE_NAME_PREFIX = "AgoraHubLocalDesktop-"


@dataclass
class AutostartManager:
    config: LocalDesktopConfig

    @property
    def value_name(self) -> str:
        suffix = sha256(self.config.instance_key.encode("utf-8")).hexdigest()[:12]
        return f"{_VALUE_NAME_PREFIX}{suffix}"

    @property
    def command(self) -> str:
        wrapper = self.config.project_root / "scripts" / "local-desktop.ps1"
        return f'powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{wrapper}" start'

    def status(self) -> OperationResult:
        value = self._read()
        enabled = value == self.command
        reason = "desktop_autostart_enabled" if enabled else "desktop_autostart_disabled"
        return OperationResult(True, "autostart", "ENABLED" if enabled else "DISABLED", reason, "current-user logon startup status", {"enabled": enabled})

    def enable(self) -> OperationResult:
        if os.name != "nt":
            raise DesktopOperationError("desktop_autostart_windows_only", "logon startup is supported only on Windows")
        wrapper = self.config.project_root / "scripts" / "local-desktop.ps1"
        if not wrapper.is_file():
            raise DesktopOperationError("desktop_entry_missing", "local desktop PowerShell entrypoint is missing")
        current = self._read()
        if current is not None and current != self.command:
            raise DesktopOperationError("desktop_autostart_not_owned", "refusing to replace an autostart value that is not owned by this instance")
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, self.command)
        return OperationResult(True, "autostart", "ENABLED", "desktop_autostart_enabled", "current-user logon startup enabled", {"enabled": True})

    def disable(self) -> OperationResult:
        if os.name != "nt":
            raise DesktopOperationError("desktop_autostart_windows_only", "logon startup is supported only on Windows")
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            try:
                current, _ = winreg.QueryValueEx(key, self.value_name)
            except FileNotFoundError:
                current = None
            if current is not None and current != self.command:
                raise DesktopOperationError("desktop_autostart_not_owned", "refusing to remove an autostart value that is not owned by this instance")
            if current is not None:
                winreg.DeleteValue(key, self.value_name)
        return OperationResult(True, "autostart", "DISABLED", "desktop_autostart_disabled", "current-user logon startup disabled", {"enabled": False})

    def _read(self) -> str | None:
        if os.name != "nt":
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                value, _ = winreg.QueryValueEx(key, self.value_name)
                return str(value)
        except FileNotFoundError:
            return None


__all__ = ["AutostartManager"]
