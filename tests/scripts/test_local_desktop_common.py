from __future__ import annotations

import os
import subprocess
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



def _create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"Windows junction creation unavailable: {result.stderr or result.stdout}")


def _remove_junction(link: Path) -> None:
    if os.path.lexists(link):
        os.rmdir(link)


def test_reject_reparse_path_checks_lexical_containment_before_resolution(tmp_path: Path) -> None:
    root = tmp_path / "owned"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    with pytest.raises(DesktopOperationError) as raised:
        common.reject_reparse_path(outside / "value.json", root=root)

    assert raised.value.reason_code == "desktop_path_escape"


def test_reject_reparse_path_rejects_lexical_parent_traversal(tmp_path: Path) -> None:
    root = tmp_path / "owned"
    root.mkdir()

    with pytest.raises(DesktopOperationError) as raised:
        common.reject_reparse_path(root / "nested" / ".." / ".." / "outside.json", root=root)

    assert raised.value.reason_code == "desktop_path_escape"


def test_reject_reparse_path_uses_one_lstat_per_lexical_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "owned"
    root.mkdir()
    candidate = root / "missing" / "value.json"
    real_lstat = Path.lstat
    inspected: list[str] = []

    def tracked_lstat(path: Path):
        inspected.append(os.path.normcase(os.fspath(path)))
        return real_lstat(path)

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("redundant path query was called")

    monkeypatch.setattr(Path, "lstat", tracked_lstat)
    monkeypatch.setattr(Path, "resolve", unexpected_call)
    monkeypatch.setattr(common.os.path, "lexists", unexpected_call)

    common.reject_reparse_path(candidate, root=root)

    assert len(inspected) == len(set(inspected))
    assert any(path.endswith(os.path.normcase("missing\\value.json")) for path in inspected)
    assert any(path.endswith(os.path.normcase("owned")) for path in inspected)


def test_reject_reparse_path_fails_closed_on_inspection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "owned"
    root.mkdir()

    def deny_lstat(_path: Path):
        raise PermissionError("inspection denied")

    monkeypatch.setattr(Path, "lstat", deny_lstat)

    with pytest.raises(DesktopOperationError) as raised:
        common.reject_reparse_path(root / "value.json", root=root)

    assert raised.value.reason_code == "desktop_path_inspection_failed"
    assert isinstance(raised.value.__cause__, PermissionError)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows extended-length paths")
def test_reject_reparse_path_accepts_safe_long_path_without_exposing_extended_prefix(tmp_path: Path) -> None:
    root = tmp_path / "owned"
    nested = root.joinpath(*(f"segment-{index:02d}-abcdefghij" for index in range(12)))
    common.windows_extended_path(nested).mkdir(parents=True)
    candidate = nested / "value.json"
    with open(common.windows_extended_path(candidate), "wb") as handle:
        handle.write(b"{}")

    assert len(os.fspath(candidate.absolute())) > 260
    assert not os.fspath(candidate).startswith("\\\\?\\")
    common.reject_reparse_path(candidate, root=root)


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_reject_reparse_path_rejects_in_root_junction_and_child(tmp_path: Path) -> None:
    root = tmp_path / "owned"
    target = root / "target"
    target.mkdir(parents=True)
    (target / "probe.txt").write_text("unchanged", encoding="utf-8")
    junction = root / "alias"
    _create_junction(junction, target)
    try:
        for candidate in (junction, junction / "probe.txt"):
            with pytest.raises(DesktopOperationError) as raised:
                common.reject_reparse_path(candidate, root=root)
            assert raised.value.reason_code == "desktop_reparse_point_rejected"
        assert (target / "probe.txt").read_text(encoding="utf-8") == "unchanged"
    finally:
        _remove_junction(junction)


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_reject_reparse_path_rejects_junction_root_and_existing_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "target"
    (target / "child").mkdir(parents=True)
    root_junction = tmp_path / "root-junction"
    _create_junction(root_junction, target)
    try:
        for candidate, owned_root in (
            (root_junction, root_junction),
            (root_junction / "child" / "value.json", root_junction / "child"),
        ):
            with pytest.raises(DesktopOperationError) as raised:
                common.reject_reparse_path(candidate, root=owned_root)
            assert raised.value.reason_code == "desktop_reparse_point_rejected"
        assert not (target / "child" / "value.json").exists()
    finally:
        _remove_junction(root_junction)
