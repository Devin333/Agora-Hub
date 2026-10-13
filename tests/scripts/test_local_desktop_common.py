from __future__ import annotations

import os
from pathlib import Path

import pytest

import scripts.local_desktop.common as common
from scripts.local_desktop.common import DesktopOperationError


def test_atomic_write_json_closes_mkstemp_descriptor_and_preserves_acl_failure(tmp_path: Path, monkeypatch) -> None:
    opened: list[int] = []
    real_mkstemp = common.tempfile.mkstemp
    real_unlink = Path.unlink

    def tracked_mkstemp(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, **kwargs)
        opened.append(descriptor)
        return descriptor, name

    monkeypatch.setattr(common.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(
        common,
        "protect_private_path",
        lambda _path: (_ for _ in ()).throw(
            DesktopOperationError("desktop_acl_protection_failed", "acl failed")
        ),
    )
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda _self, *args, **kwargs: (_ for _ in ()).throw(
            PermissionError("simulated cleanup failure")
        ),
    )

    with pytest.raises(DesktopOperationError) as raised:
        common.atomic_write_json(tmp_path / "state.json", {"ok": True})

    assert raised.value.reason_code == "desktop_acl_protection_failed"
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    monkeypatch.setattr(Path, "unlink", real_unlink)
    for candidate in tmp_path.iterdir():
        candidate.unlink()
