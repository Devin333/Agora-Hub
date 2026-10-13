from __future__ import annotations

from pathlib import Path

import pytest

from framework.shared.local_desktop import resolve_local_desktop_config
from scripts.local_desktop.common import DesktopOperationError
from scripts.local_desktop.desktop_entry import DesktopEntryManager, ShortcutRecord


class FakeShortcutAdapter:
    def __init__(self, desktop: Path) -> None:
        self.desktop = desktop
        self.records: dict[Path, ShortcutRecord] = {}

    def desktop_path(self) -> Path:
        return self.desktop

    def read(self, path: Path) -> ShortcutRecord | None:
        return self.records.get(path)

    def create(self, path: Path, record: ShortcutRecord) -> None:
        self.records[path] = record

    def remove(self, path: Path) -> None:
        self.records.pop(path, None)


def _manager(tmp_path: Path) -> tuple[DesktopEntryManager, FakeShortcutAdapter]:
    wrapper = tmp_path / "scripts" / "local-desktop.ps1"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("# entry", encoding="utf-8")
    adapter = FakeShortcutAdapter(tmp_path / "Desktop")
    adapter.desktop.mkdir()
    manager = DesktopEntryManager(resolve_local_desktop_config(project_root=tmp_path), adapter=adapter)
    return manager, adapter


def test_desktop_entry_installs_owned_hidden_start_open_shortcut(tmp_path: Path) -> None:
    manager, adapter = _manager(tmp_path)

    result = manager.install()
    record = adapter.records[manager.shortcut_path]

    assert result.status == "INSTALLED"
    assert manager.name.startswith("Agora Hub Research (")
    assert "-WindowStyle Hidden" in record.arguments
    assert record.arguments.endswith("start --open")
    assert record.working_directory == tmp_path.resolve()


def test_desktop_entry_refuses_to_replace_or_remove_foreign_shortcut(tmp_path: Path) -> None:
    manager, adapter = _manager(tmp_path)
    adapter.records[manager.shortcut_path] = ShortcutRecord(Path("C:/foreign.exe"), "--foreign", tmp_path)

    with pytest.raises(DesktopOperationError) as install_error:
        manager.install()
    with pytest.raises(DesktopOperationError) as remove_error:
        manager.remove()

    assert install_error.value.reason_code == "desktop_entry_not_owned"
    assert remove_error.value.reason_code == "desktop_entry_not_owned"
    assert manager.shortcut_path in adapter.records


def test_desktop_entry_remove_is_idempotent_for_owned_instance(tmp_path: Path) -> None:
    manager, adapter = _manager(tmp_path)
    manager.install()

    first = manager.remove()
    second = manager.remove()

    assert first.status == "NOT_INSTALLED"
    assert second.status == "NOT_INSTALLED"
    assert manager.shortcut_path not in adapter.records
