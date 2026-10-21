"""Bounded raster assets from untrusted Marker output; never dereference URLs."""

from __future__ import annotations

import base64
import binascii
import os
import stat
import struct
import warnings
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from framework.agent.artifacts.paths import (
    artifact_path_relative_to,
    validate_artifact_path_segment,
)
from framework.agent.artifacts.stores.errors import ArtifactStoreMetadataError
from framework.agent.artifacts.stores.fs_safety import (
    is_link_or_reparse_point,
    verified_atomic_create,
)
from framework.shared.windows_paths import resolve_storage_path, storage_io_path

MAX_MARKER_IMAGE_BYTES = 8 * 1024 * 1024
_MEDIA_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}
_FORMAT_SUFFIXES = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
_MAX_IMAGE_PIXELS = 16_000_000


def _raster_suffix(data: bytes) -> str | None:
    """Return an allowlisted raster suffix only for a structurally valid image."""
    if not data or len(data) > MAX_MARKER_IMAGE_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=list(_FORMAT_SUFFIXES)) as image:
                suffix = _FORMAT_SUFFIXES.get(image.format or "")
                width, height = image.size
                if (
                    suffix is None
                    or width <= 0
                    or height <= 0
                    or width * height > _MAX_IMAGE_PIXELS
                    or getattr(image, "n_frames", 1) != 1
                ):
                    return None
                if suffix == ".png" and not data.endswith(
                    b"\x00\x00\x00\x00IEND\xaeB`\x82"
                ):
                    return None
                if suffix == ".jpg" and not data.endswith(b"\xff\xd9"):
                    return None
                if suffix == ".webp" and (
                    len(data) < 12
                    or int.from_bytes(data[4:8], "little") + 8 != len(data)
                ):
                    return None
                image.verify()
            # verify() alone does not decode JPEG scan data or PNG pixels.
            with Image.open(BytesIO(data), formats=list(_FORMAT_SUFFIXES)) as image:
                image.load()
            return suffix
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
        struct.error,
    ):
        return None


def _configured_root(root: Path) -> Path:
    """Resolve only trusted ancestors, never the untrusted root itself."""
    root = Path(os.path.abspath(root))
    return resolve_storage_path(root.parent) / root.name


@contextmanager
def _windows_directory(path: Path) -> Iterator[None]:
    """Pin a directory against replacement and reparse edits; inspect its handle."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    class AttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("tag", wintypes.DWORD),
        ]

    query = kernel.GetFileInformationByHandleEx
    query.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    query.restype = wintypes.BOOL
    # READ_ATTRIBUTES, SHARE_READ (no write/delete sharing), OPEN_EXISTING,
    # BACKUP_SEMANTICS | OPEN_REPARSE_POINT. Inspect the node, not its target.
    handle = create(str(storage_io_path(path)), 0x80, 1, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = AttributeTagInfo()
        if not query(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not info.attributes & 0x10 or info.attributes & 0x400:
            raise ValueError("asset directory is not a regular directory")
        yield
    finally:
        close(handle)


@contextmanager
def _directory_tree(
    root: Path, parts: tuple[str, ...], *, create: bool
) -> Iterator[tuple[Path, int | None]]:
    """Keep every untrusted directory pinned throughout filesystem I/O."""
    with ExitStack() as stack:
        current = root
        descriptor = None
        pinned: list[tuple[Path, os.stat_result]] = []
        for index, part in enumerate((None, *parts)):
            if part is not None:
                validate_artifact_path_segment(part, field="marker directory")
                current = current / part
            if os.name == "nt":
                if create:
                    storage_io_path(current).mkdir(exist_ok=True)
                stack.enter_context(_windows_directory(current))
                info = storage_io_path(current).lstat()
                if is_link_or_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
                    raise ValueError("asset directory is not a regular directory")
            else:
                name = current if index == 0 else part
                if create:
                    try:
                        os.mkdir(name, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                opened = os.open(name, flags, dir_fd=descriptor)
                stack.callback(os.close, opened)
                descriptor = opened
                pinned.append((current, os.fstat(opened)))
        yield current, descriptor
        # dir_fd prevents redirected I/O; additionally reject paths whose public
        # names no longer identify the directories used during the operation.
        for path, expected in pinned:
            current_info = path.lstat()
            if is_link_or_reparse_point(current_info) or not os.path.samestat(
                expected, current_info
            ):
                raise ValueError("asset directory identity changed")


def _read_local_asset(source: Path, *, root: Path) -> bytes:
    relative = artifact_path_relative_to(source, root)
    if not relative.parts:
        raise ValueError("asset is not a file")
    with _directory_tree(root, relative.parts[:-1], create=False) as (_, parent):
        return _read_file(source, directory_descriptor=parent)


def _read_file(source: Path, *, directory_descriptor: int | None) -> bytes:
    name = storage_io_path(source) if directory_descriptor is None else source.name
    before = (
        storage_io_path(source).lstat()
        if directory_descriptor is None
        else os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    )
    if (
        is_link_or_reparse_point(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > MAX_MARKER_IMAGE_BYTES
    ):
        raise ValueError("asset is not a bounded regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
                raise ValueError("asset identity changed")
            data = handle.read(MAX_MARKER_IMAGE_BYTES + 1)
            after = os.fstat(handle.fileno())
        committed = (
            storage_io_path(source).lstat()
            if directory_descriptor is None
            else os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        )
        if (
            is_link_or_reparse_point(committed)
            or len(data) > MAX_MARKER_IMAGE_BYTES
            or len(data) != opened.st_size
            or not os.path.samestat(opened, committed)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
        ):
            raise ValueError("asset changed or exceeded byte budget")
        return data
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _create_local_asset(
    target: Path, data: bytes, *, root: Path, directory_descriptor: int | None
) -> bool:
    if directory_descriptor is None:
        # Windows directory handles prevent root/ancestor replacement while the
        # shared immutable publisher runs, including its internal resolve().
        return verified_atomic_create(target, data, root=root, identity="marker-image")
    temporary = ".marker-" + uuid4().hex + ".tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_descriptor,
    )
    owned = os.fstat(descriptor)
    try:
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor != -1:
                os.close(descriptor)
        staged = os.stat(temporary, dir_fd=directory_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(staged.st_mode) or not os.path.samestat(owned, staged):
            raise ValueError("asset staging identity changed")
        try:
            os.link(
                temporary,
                target.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        committed = os.stat(
            target.name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if not stat.S_ISREG(committed.st_mode) or not os.path.samestat(
            owned, committed
        ):
            raise ValueError("asset publication identity changed")
        os.fsync(directory_descriptor)
        return True
    finally:
        try:
            staged = os.stat(
                temporary, dir_fd=directory_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        else:
            if os.path.samestat(owned, staged):
                os.unlink(temporary, dir_fd=directory_descriptor)


def _publish_image(data: bytes, *, target_dir: Path, artifact_root: Path) -> str | None:
    suffix = _raster_suffix(data)
    if suffix is None:
        return None
    try:
        lexical_root = Path(os.path.abspath(artifact_root))
        relative = artifact_path_relative_to(
            Path(os.path.abspath(target_dir)), lexical_root
        )
        root = _configured_root(lexical_root)
        storage_io_path(root.parent).mkdir(parents=True, exist_ok=True)
        with _directory_tree(root, relative.parts, create=True) as (parent, descriptor):
            root_before = storage_io_path(root).lstat()
            target = parent / (sha256(data).hexdigest() + suffix)
            _create_local_asset(
                target, data, root=root, directory_descriptor=descriptor
            )
            if _read_file(target, directory_descriptor=descriptor) != data:
                return None
            root_after = storage_io_path(root).lstat()
            if is_link_or_reparse_point(root_after) or not os.path.samestat(
                root_before, root_after
            ):
                return None
            return str(target)
    except (OSError, ValueError, ArtifactStoreMetadataError):
        return None


def copy_marker_image(
    raw: str,
    *,
    output_root: Path,
    target_dir: Path,
    artifact_root: Path,
) -> str | None:
    """Copy only regular, bounded raster files beneath this run's output root."""
    if not raw or len(raw) > 4096 or "\x00" in raw:
        return None
    windows_path = PureWindowsPath(raw)
    if (
        raw.startswith(("\\\\", "//"))
        or (windows_path.drive and not windows_path.is_absolute())
        or (windows_path.root and not windows_path.drive and not raw.startswith("/"))
    ):
        return None
    try:
        # Validate before abspath can erase dot components. This also blocks ADS,
        # device names, trailing-dot aliases, control characters, and URI schemes.
        parts = windows_path.parts[1:] if windows_path.anchor else windows_path.parts
        for part in parts:
            validate_artifact_path_segment(part, field="marker source")
        source = Path(raw.replace("\\", "/"))
        lexical_root = Path(os.path.abspath(output_root))
        if not source.is_absolute():
            if windows_path.drive or windows_path.root:
                return None
            source = lexical_root / source
        relative = artifact_path_relative_to(source, lexical_root)
        root = _configured_root(lexical_root)
        data = _read_local_asset(root / relative, root=root)
        return _publish_image(data, target_dir=target_dir, artifact_root=artifact_root)
    except (OSError, ValueError, ArtifactStoreMetadataError):
        return None


def write_embedded_marker_image(
    raw: str,
    *,
    target_dir: Path,
    artifact_root: Path,
) -> str | None:
    """Decode only size-bounded PNG/JPEG/WebP payloads with matching MIME."""
    max_encoded = 4 * ((MAX_MARKER_IMAGE_BYTES + 2) // 3)
    if not raw or len(raw) > max_encoded + 64:
        return None
    payload = raw.strip()
    expected_suffix = None
    if payload.startswith("data:"):
        header, separator, payload = payload.partition(",")
        if not separator or not header.endswith(";base64"):
            return None
        expected_suffix = _MEDIA_SUFFIXES.get(header[5:-7].lower())
        if expected_suffix is None:
            return None
    if len(payload) > max_encoded:
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    suffix = _raster_suffix(data)
    if expected_suffix is not None and suffix != expected_suffix:
        return None
    return _publish_image(data, target_dir=target_dir, artifact_root=artifact_root)
