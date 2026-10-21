from __future__ import annotations

import base64
import os
import stat
import subprocess
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from PIL import Image

from backend.research.document import marker_assets
from backend.research.document.marker_assets import (
    copy_marker_image,
    write_embedded_marker_image,
)


@pytest.fixture
def asset_paths(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    root = tmp_path / "papers"
    return output, root / "paper" / "figures", root


@pytest.fixture
def png_bytes():
    document = pymupdf.open()
    document.new_page(width=10, height=10)
    try:
        return document[0].get_pixmap().tobytes("png")
    finally:
        document.close()


def _copy(raw, paths):
    output, target, root = paths
    return copy_marker_image(
        raw,
        output_root=output,
        target_dir=target,
        artifact_root=root,
    )


def _embed(raw, paths):
    _, target, root = paths
    return write_embedded_marker_image(raw, target_dir=target, artifact_root=root)


def test_valid_local_and_embedded_raster_share_immutable_content_address(
    asset_paths, png_bytes
):
    output, _, _ = asset_paths
    (output / "figure.png").write_bytes(png_bytes)
    relative = _copy("figure.png", asset_paths)
    absolute = _copy(str(output / "figure.png"), asset_paths)
    embedded = _embed(
        "data:image/png;base64," + base64.b64encode(png_bytes).decode(),
        asset_paths,
    )
    assert relative == absolute == embedded
    assert Path(relative).read_bytes() == png_bytes
    assert Path(relative).stem == marker_assets.sha256(png_bytes).hexdigest()


@pytest.mark.parametrize(
    "raw",
    [
        "../secret.png",
        "missing.png",
        "https://example.com/a.png",
        "file:///etc/passwd",
        "\\\\server\\share\\file.png",
        "\\\\?\\C:\\secret.png",
        r"\\.\C:\secret.png",
        "C:secret.png",
        ":secret.png",
        "x:secret.png",
        "figure.png:payload",
        "NUL.png",
        "CON",
        "figures/COM1.png",
        "figure.png.",
        "figure.png ",
        "figure\n.png",
        "images/../figure.png",
        "a.png:secret",
        "data:image/png;base64,aGVsbG8=",
        "x\x00.png",
    ],
)
def test_untrusted_paths_are_not_returned_as_image_refs(raw, asset_paths, png_bytes):
    output, _, root = asset_paths
    (output.parent / "secret.png").write_bytes(png_bytes)
    assert _copy(raw, asset_paths) is None
    assert not root.exists()


def test_external_absolute_file_rejected(asset_paths, png_bytes):
    output, _, _ = asset_paths
    external = output.parent / "secret.png"
    external.write_bytes(png_bytes)
    assert _copy(str(external), asset_paths) is None


def test_non_raster_and_missing_assets_cannot_be_copied(asset_paths):
    output, _, _ = asset_paths
    (output / "secret.png").write_bytes(b"<script>not an image</script>")
    assert _copy("secret.png", asset_paths) is None
    assert _copy("missing.png", asset_paths) is None


def test_file_and_encoded_payloads_have_byte_limits(
    asset_paths, png_bytes, monkeypatch
):
    output, _, _ = asset_paths
    (output / "figure.png").write_bytes(png_bytes)
    monkeypatch.setattr(marker_assets, "MAX_MARKER_IMAGE_BYTES", len(png_bytes) - 1)
    assert _copy("figure.png", asset_paths) is None
    assert _embed(base64.b64encode(png_bytes).decode(), asset_paths) is None

    # Length rejection happens before any base64 decoder is invoked.
    def decode_forbidden(*args, **kwargs):
        raise AssertionError("oversized content must not be decoded")

    monkeypatch.setattr(marker_assets.base64, "b64decode", decode_forbidden)
    assert _embed("A" * (len(png_bytes) * 2 + 100), asset_paths) is None


@pytest.mark.parametrize(
    "raw",
    [
        "data:image/svg+xml;base64,PHN2Zy8+",
        "data:text/html;base64,PGgxLz4=",
        "data:image/png,notbase64",
        "data:image/png;base64,%%%",
        "YQ==",
        base64.b64encode(b"<svg>fake content</svg>").decode(),
        base64.b64encode(b"\x89PNG\r\n\x1a\nnot-a-png").decode(),
        base64.b64encode(b"\xff\xd8\xffnot-a-jpeg").decode(),
        base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPnot-webp").decode(),
    ],
)
def test_invalid_or_active_embedded_content_is_rejected(raw, asset_paths):
    assert _embed(raw, asset_paths) is None


def test_mime_must_match_raster_signature(asset_paths, png_bytes):
    assert (
        _embed(
            "data:image/jpeg;base64," + base64.b64encode(png_bytes).decode(),
            asset_paths,
        )
        is None
    )


def test_link_and_reparse_detection_covers_windows_junction_flags():
    assert marker_assets.is_link_or_reparse_point(
        SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x0400)
    )
    assert marker_assets.is_link_or_reparse_point(
        SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
    )


def _mark_reparse(monkeypatch, path: Path):
    # Patch both OS no-follow entry points: Path.lstat uses os.stat in Python
    # 3.12 and os.lstat in 3.14. Leave normal following stat calls untouched.
    original_lstat = os.lstat
    original_stat = os.stat

    def path_key(candidate):
        return os.path.normcase(os.path.abspath(candidate)).removeprefix("\\\\?\\")

    key = path_key(path)

    def mark(candidate, result):
        if not isinstance(candidate, int) and path_key(candidate) == key:
            attributes = {
                name: getattr(result, name)
                for name in dir(result)
                if name.startswith("st_")
            }
            attributes["st_file_attributes"] = (
                getattr(result, "st_file_attributes", 0) | 0x0400
            )
            return SimpleNamespace(**attributes)
        return result

    def lstat(candidate, *args, **kwargs):
        return mark(candidate, original_lstat(candidate, *args, **kwargs))

    def stat_no_follow(candidate, *args, **kwargs):
        result = original_stat(candidate, *args, **kwargs)
        if kwargs.get("follow_symlinks", True) is False:
            return mark(candidate, result)
        return result

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "stat", stat_no_follow)


@pytest.mark.parametrize(
    "accessor", ["os_lstat", "os_stat_nofollow", "path_lstat", "path_stat_nofollow"]
)
def test_reparse_fixture_covers_os_no_follow_boundaries(
    tmp_path, monkeypatch, accessor
):
    target = tmp_path / "target.png"
    target.write_bytes(b"fixture payload")
    unrelated = tmp_path / "unrelated.png"
    unrelated.write_bytes(b"unrelated payload")
    original = target.stat()
    _mark_reparse(monkeypatch, target)
    probes = {
        "os_lstat": lambda: os.lstat(target),
        "os_stat_nofollow": lambda: os.stat(target, follow_symlinks=False),
        "path_lstat": target.lstat,
        "path_stat_nofollow": lambda: target.stat(follow_symlinks=False),
    }
    marked = probes[accessor]()
    assert marker_assets.is_link_or_reparse_point(marked)
    assert marked.st_size == original.st_size
    assert os.path.samestat(marked, original)
    assert not marker_assets.is_link_or_reparse_point(target.stat())
    assert not marker_assets.is_link_or_reparse_point(unrelated.lstat())


def test_source_reparse_file_is_rejected_before_read(
    asset_paths, png_bytes, monkeypatch
):
    output, _, _ = asset_paths
    source = output / "link.png"
    source.write_bytes(png_bytes)
    _mark_reparse(monkeypatch, source)
    assert _copy("link.png", asset_paths) is None


def test_output_root_reparse_point_is_rejected(asset_paths, png_bytes, monkeypatch):
    output, _, _ = asset_paths
    (output / "figure.png").write_bytes(png_bytes)
    _mark_reparse(monkeypatch, output)
    assert _copy("figure.png", asset_paths) is None


@pytest.mark.parametrize("linked_component", ["root", "paper", "figures", "file"])
def test_target_reparse_points_are_never_written(
    asset_paths, png_bytes, linked_component, monkeypatch
):
    _, target, root = asset_paths
    targets = {
        "root": root,
        "paper": root / "paper",
        "figures": target,
        "file": target / (marker_assets.sha256(png_bytes).hexdigest() + ".png"),
    }
    link = targets[linked_component]
    if linked_component == "file":
        link.parent.mkdir(parents=True)
        link.write_bytes(b"unchanged")
    else:
        link.mkdir(parents=True)
    _mark_reparse(monkeypatch, link)
    assert _embed(base64.b64encode(png_bytes).decode(), asset_paths) is None
    if linked_component == "file":
        assert link.read_bytes() == b"unchanged"
    else:
        assert list(link.iterdir()) == []


def _directory_link(link: Path, destination: Path):
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "New-Item -ItemType Junction -Path $env:MARKER_TEST_LINK "
                "-Target $env:MARKER_TEST_DEST -ErrorAction Stop | Out-Null",
            ],
            env={
                **os.environ,
                "MARKER_TEST_LINK": str(link),
                "MARKER_TEST_DEST": str(destination),
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(destination, target_is_directory=True)


@pytest.mark.parametrize("boundary", ["source", "destination"])
def test_real_directory_link_does_not_leak_assets(asset_paths, png_bytes, boundary):
    output, _, root = asset_paths
    external = output.parent / "external"
    external.mkdir()
    secret = external / "secret.png"
    secret.write_bytes(png_bytes)
    link = output / "images" if boundary == "source" else root
    _directory_link(link, external)
    try:
        if boundary == "source":
            assert _copy("images/secret.png", asset_paths) is None
        else:
            assert _embed(base64.b64encode(png_bytes).decode(), asset_paths) is None
        assert secret.read_bytes() == png_bytes
        assert sorted(item.name for item in external.iterdir()) == ["secret.png"]
    finally:
        # Remove only the link itself, never recursively traverse its target.
        link.parent.resolve().relative_to(output.parent.resolve())
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_target_must_stay_under_artifact_root(asset_paths, png_bytes):
    output, _, root = asset_paths
    outside = output.parent / "outside"
    assert (
        write_embedded_marker_image(
            base64.b64encode(png_bytes).decode(),
            target_dir=outside,
            artifact_root=root,
        )
        is None
    )
    assert not outside.exists()


def test_artifact_root_replacement_during_publish_is_rejected(
    asset_paths, png_bytes, monkeypatch
):
    output, _, root = asset_paths
    root.mkdir()
    external = output.parent / "external-root"
    external.mkdir()
    secret = external / "keep.png"
    secret.write_bytes(png_bytes)
    displaced = output.parent / "displaced-root"
    original_create = marker_assets._create_local_asset
    attempted = False

    def replace_root_then_create(target, content, *, root, directory_descriptor):
        nonlocal attempted
        attempted = True
        root.rename(displaced)
        _directory_link(root, external)
        return original_create(
            target, content, root=root, directory_descriptor=directory_descriptor
        )

    monkeypatch.setattr(
        marker_assets,
        "_create_local_asset",
        replace_root_then_create,
    )
    try:
        assert _embed(base64.b64encode(png_bytes).decode(), asset_paths) is None
        assert attempted
        assert secret.read_bytes() == png_bytes
        assert sorted(path.name for path in external.iterdir()) == ["keep.png"]
    finally:
        # Windows refuses the rename while a no-delete-share handle pins it.
        if displaced.exists():
            if os.name == "nt":
                root.rmdir()
            else:
                root.unlink()
            displaced.rename(root)


@pytest.mark.parametrize(
    "image_format, suffix", [("PNG", ".png"), ("JPEG", ".jpg"), ("WEBP", ".webp")]
)
def test_real_raster_formats_are_identified_by_content(
    asset_paths, image_format, suffix
):
    output, _, _ = asset_paths
    stream = BytesIO()
    with Image.new("RGB", (8, 8), color="red") as image:
        image.save(stream, format=image_format)
    data = stream.getvalue()
    (output / "figure.unknown").write_bytes(data)
    local = _copy("figure.unknown", asset_paths)
    embedded = _embed(base64.b64encode(data).decode(), asset_paths)
    assert local == embedded
    assert local is not None
    assert Path(local).suffix == suffix
    assert Path(local).read_bytes() == data


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_truncated_rasters_are_rejected_locally_and_embedded(asset_paths, image_format):
    output, _, root = asset_paths
    stream = BytesIO()
    with Image.new("RGB", (8, 8), color="red") as image:
        image.save(stream, format=image_format)
    truncated = stream.getvalue()[:-2]
    (output / "figure.png").write_bytes(truncated)
    assert _copy("figure.png", asset_paths) is None
    assert _embed(base64.b64encode(truncated).decode(), asset_paths) is None
    assert not root.exists()


def test_pixel_budget_is_enforced_before_decode(asset_paths, png_bytes, monkeypatch):
    monkeypatch.setattr(marker_assets, "_MAX_IMAGE_PIXELS", 99)

    def forbidden_load(*args, **kwargs):
        raise AssertionError("oversized pixel dimensions must not be decoded")

    monkeypatch.setattr(Image.Image, "load", forbidden_load)
    assert _embed(base64.b64encode(png_bytes).decode(), asset_paths) is None


def test_existing_content_address_is_not_overwritten(asset_paths, png_bytes):
    _, target, _ = asset_paths
    target.mkdir(parents=True)
    existing = target / (marker_assets.sha256(png_bytes).hexdigest() + ".png")
    existing.write_bytes(b"corrupt existing artifact")
    assert _embed(base64.b64encode(png_bytes).decode(), asset_paths) is None
    assert existing.read_bytes() == b"corrupt existing artifact"
    assert list(target.iterdir()) == [existing]


def test_missing_output_root_is_rejected(asset_paths):
    output, _, root = asset_paths
    output.rmdir()
    assert _copy("figure.png", asset_paths) is None
    assert not root.exists()


def test_source_root_replacement_during_open_is_rejected(
    asset_paths, png_bytes, monkeypatch
):
    output, _, root = asset_paths
    (output / "figure.png").write_bytes(png_bytes)
    external = output.parent / "external-source"
    external.mkdir()
    (external / "figure.png").write_bytes(b"external secret")
    displaced = output.parent / "displaced-output"
    original_open = os.open
    attempted = False

    def replace_root_then_open(path, flags, *args, **kwargs):
        nonlocal attempted
        if Path(path).name == "figure.png":
            attempted = True
            output.rename(displaced)
            _directory_link(output, external)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_root_then_open)
    try:
        assert _copy("figure.png", asset_paths) is None
        assert attempted
        assert not root.exists()
        assert (external / "figure.png").read_bytes() == b"external secret"
    finally:
        if displaced.exists():
            if os.name == "nt":
                output.rmdir()
            else:
                output.unlink()
            displaced.rename(output)


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path namespace")
def test_windows_long_paths_and_equivalent_namespaces(tmp_path, png_bytes):
    base = tmp_path.joinpath(*("long-marker-path-" + str(i) for i in range(10)))
    output = base / "output"
    root = base / "papers"
    target = root / "paper" / "figures"
    marker_assets.storage_io_path(output).mkdir(parents=True)
    marker_assets.storage_io_path(output / "figure.png").write_bytes(png_bytes)
    local = copy_marker_image(
        str(output / "figure.png"),
        output_root=marker_assets.storage_io_path(output),
        target_dir=marker_assets.storage_io_path(target),
        artifact_root=root,
    )
    embedded = write_embedded_marker_image(
        base64.b64encode(png_bytes).decode(),
        target_dir=target,
        artifact_root=marker_assets.storage_io_path(root),
    )
    assert local is not None
    assert local == embedded
    assert not local.startswith("\\\\?\\")
    assert marker_assets.storage_io_path(local).read_bytes() == png_bytes


def test_parent_creation_failure_is_rejected(asset_paths, png_bytes, monkeypatch):
    def denied_mkdir(*args, **kwargs):
        raise PermissionError("artifact parent is not writable")

    monkeypatch.setattr(Path, "mkdir", denied_mkdir)
    assert _embed(base64.b64encode(png_bytes).decode(), asset_paths) is None


@pytest.mark.parametrize("pixel_limit", [10, 75])
def test_pillow_decompression_bombs_are_rejected(
    asset_paths, png_bytes, monkeypatch, pixel_limit
):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", pixel_limit)
    assert _embed(base64.b64encode(png_bytes).decode(), asset_paths) is None


@pytest.mark.parametrize("boundary", ["source", "destination"])
def test_real_nested_directory_links_are_rejected(asset_paths, png_bytes, boundary):
    output, target, root = asset_paths
    external = output.parent / "nested-external"
    external.mkdir()
    (external / "figure.png").write_bytes(png_bytes)
    root.mkdir()
    link = output / "images" if boundary == "source" else root / "paper"
    _directory_link(link, external)
    try:
        if boundary == "source":
            assert _copy("images/figure.png", asset_paths) is None
        else:
            assert _embed(base64.b64encode(png_bytes).decode(), asset_paths) is None
        assert [path.name for path in external.iterdir()] == ["figure.png"]
        assert (external / "figure.png").read_bytes() == png_bytes
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()
