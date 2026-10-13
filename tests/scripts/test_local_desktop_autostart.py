from __future__ import annotations

from pathlib import Path

import pytest

from framework.shared.local_desktop import resolve_local_desktop_config
from scripts.local_desktop.autostart import AutostartManager
from scripts.local_desktop.common import DesktopOperationError


def test_autostart_enable_refuses_foreign_existing_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper = tmp_path / "scripts" / "local-desktop.ps1"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("# entry", encoding="utf-8")
    manager = AutostartManager(resolve_local_desktop_config(project_root=tmp_path))
    monkeypatch.setattr("scripts.local_desktop.autostart.os.name", "nt")
    monkeypatch.setattr(manager, "_read", lambda: 'powershell.exe -File "C:\\foreign.ps1"')

    with pytest.raises(DesktopOperationError) as raised:
        manager.enable()

    assert raised.value.reason_code == "desktop_autostart_not_owned"


def test_autostart_command_runs_owned_wrapper_hidden(tmp_path: Path) -> None:
    manager = AutostartManager(resolve_local_desktop_config(project_root=tmp_path))

    assert "-WindowStyle Hidden" in manager.command
    assert manager.command.endswith('local-desktop.ps1" start')
