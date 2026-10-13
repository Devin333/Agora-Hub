from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from framework.shared.local_desktop import LocalDesktopConfig
from scripts.local_desktop.common import DesktopOperationError, OperationResult

_SHORTCUT_NAME_PREFIX = "Agora Hub Research"


@dataclass(frozen=True)
class ShortcutRecord:
    target: Path
    arguments: str
    working_directory: Path


class ShortcutAdapter(Protocol):
    def desktop_path(self) -> Path: ...
    def read(self, path: Path) -> ShortcutRecord | None: ...
    def create(self, path: Path, record: ShortcutRecord) -> None: ...
    def remove(self, path: Path) -> None: ...


@dataclass
class DesktopEntryManager:
    config: LocalDesktopConfig
    adapter: ShortcutAdapter | None = None

    def __post_init__(self) -> None:
        if self.adapter is None:
            self.adapter = WindowsShellShortcutAdapter()

    @property
    def name(self) -> str:
        instance = sha256(self.config.instance_key.encode("utf-8")).hexdigest()[:10]
        return f"{_SHORTCUT_NAME_PREFIX} ({instance}).lnk"

    @property
    def shortcut_path(self) -> Path:
        assert self.adapter is not None
        desktop = self.adapter.desktop_path().expanduser().resolve(strict=False)
        if desktop == Path(desktop.anchor):
            raise DesktopOperationError("desktop_entry_path_invalid", "current-user Desktop path is invalid")
        return desktop / self.name

    @property
    def expected(self) -> ShortcutRecord:
        system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
        target = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        wrapper = self.config.project_root / "scripts" / "local-desktop.ps1"
        arguments = f'-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{wrapper}" start --open'
        return ShortcutRecord(target.resolve(strict=False), arguments, self.config.project_root.resolve(strict=False))

    def status(self) -> OperationResult:
        assert self.adapter is not None
        path = self.shortcut_path
        current = self.adapter.read(path)
        installed = current == self.expected
        return OperationResult(True, "desktop-entry", "INSTALLED" if installed else "NOT_INSTALLED", "desktop_entry_installed" if installed else "desktop_entry_not_installed", "current-user Desktop entry status", {"installed": installed, "name": self.name})

    def install(self) -> OperationResult:
        if os.name != "nt" and isinstance(self.adapter, WindowsShellShortcutAdapter):
            raise DesktopOperationError("desktop_entry_windows_only", "Desktop entry installation is supported only on Windows")
        wrapper = self.config.project_root / "scripts" / "local-desktop.ps1"
        if not wrapper.is_file():
            raise DesktopOperationError("desktop_entry_missing", "local desktop PowerShell entrypoint is missing")
        assert self.adapter is not None
        path = self.shortcut_path
        current = self.adapter.read(path)
        if current is not None and current != self.expected:
            raise DesktopOperationError("desktop_entry_not_owned", "refusing to replace a Desktop shortcut that is not owned by this instance")
        self.adapter.create(path, self.expected)
        return OperationResult(True, "desktop-entry", "INSTALLED", "desktop_entry_installed", "current-user Desktop entry installed", {"installed": True, "name": self.name})

    def remove(self) -> OperationResult:
        if os.name != "nt" and isinstance(self.adapter, WindowsShellShortcutAdapter):
            raise DesktopOperationError("desktop_entry_windows_only", "Desktop entry removal is supported only on Windows")
        assert self.adapter is not None
        path = self.shortcut_path
        current = self.adapter.read(path)
        if current is not None and current != self.expected:
            raise DesktopOperationError("desktop_entry_not_owned", "refusing to remove a Desktop shortcut that is not owned by this instance")
        if current is not None:
            self.adapter.remove(path)
        return OperationResult(True, "desktop-entry", "NOT_INSTALLED", "desktop_entry_removed", "current-user Desktop entry removed", {"installed": False, "name": self.name})


class WindowsShellShortcutAdapter:
    def desktop_path(self) -> Path:
        payload = self._invoke("[Environment]::GetFolderPath('Desktop')")
        path = Path(str(payload).strip())
        if not str(path):
            raise DesktopOperationError("desktop_entry_path_invalid", "Windows did not return a current-user Desktop path")
        return path

    def read(self, path: Path) -> ShortcutRecord | None:
        script = """
$shortcutPath = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:NEWSROOM_SHORTCUT_PATH_B64))
if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) { return }
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcutPath)
@{ target = $link.TargetPath; arguments = $link.Arguments; working_directory = $link.WorkingDirectory } | ConvertTo-Json -Compress
"""
        output = self._invoke(script, shortcut_path=path)
        if not str(output).strip():
            return None
        try:
            value = json.loads(output)
            return ShortcutRecord(Path(value["target"]).resolve(strict=False), str(value["arguments"]), Path(value["working_directory"]).resolve(strict=False))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DesktopOperationError("desktop_entry_invalid", "Desktop shortcut metadata is invalid") from exc

    def create(self, path: Path, record: ShortcutRecord) -> None:
        script = """
$shortcutPath = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:NEWSROOM_SHORTCUT_PATH_B64))
$target = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:NEWSROOM_SHORTCUT_TARGET_B64))
$arguments = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:NEWSROOM_SHORTCUT_ARGUMENTS_B64))
$workingDirectory = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:NEWSROOM_SHORTCUT_WORKDIR_B64))
$parent = Split-Path -Parent $shortcutPath
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Desktop directory is unavailable' }
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcutPath)
$link.TargetPath = $target
$link.Arguments = $arguments
$link.WorkingDirectory = $workingDirectory
$link.WindowStyle = 7
$link.Save()
"""
        self._invoke(script, shortcut_path=path, target=record.target, arguments=record.arguments, working_directory=record.working_directory)

    def remove(self, path: Path) -> None:
        script = """
$shortcutPath = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:NEWSROOM_SHORTCUT_PATH_B64))
if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) { Remove-Item -LiteralPath $shortcutPath -Force }
"""
        self._invoke(script, shortcut_path=path)

    @staticmethod
    def _invoke(script: str, *, shortcut_path: Path | None = None, target: Path | None = None, arguments: str | None = None, working_directory: Path | None = None) -> str:
        if os.name != "nt":
            raise DesktopOperationError("desktop_entry_windows_only", "Desktop shortcuts are supported only on Windows")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        environment = dict(os.environ)
        values = {
            "NEWSROOM_SHORTCUT_PATH_B64": str(shortcut_path) if shortcut_path is not None else None,
            "NEWSROOM_SHORTCUT_TARGET_B64": str(target) if target is not None else None,
            "NEWSROOM_SHORTCUT_ARGUMENTS_B64": arguments,
            "NEWSROOM_SHORTCUT_WORKDIR_B64": str(working_directory) if working_directory is not None else None,
        }
        for name, value in values.items():
            if value is not None:
                environment[name] = base64.b64encode(value.encode("utf-8")).decode("ascii")
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
            startupinfo=startupinfo,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if completed.returncode != 0:
            raise DesktopOperationError("desktop_entry_operation_failed", "Windows Desktop shortcut operation failed")
        return completed.stdout.strip()


__all__ = ["DesktopEntryManager", "ShortcutAdapter", "ShortcutRecord", "WindowsShellShortcutAdapter"]
