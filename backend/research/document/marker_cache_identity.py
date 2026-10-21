from __future__ import annotations

import json
import os
import re
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Callable


_MARKER_IMAGE_DIGEST_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*@sha256:[0-9a-f]{64}$"
)
_MARKER_WEIGHTS_MANIFEST_SCHEMA = "newsroom-marker-weights-manifest.v1"
_MARKER_WEIGHTS_BINDING_SCHEMA = "newsroom-marker-weights-binding.v1"
_MARKER_WEIGHTS_CONTAINER_CACHE_DIR = "/root/.cache"
_MARKER_WEIGHTS_MANIFEST_MAX_BYTES = 1024 * 1024
_MARKER_WEIGHTS_MANIFEST_MAX_FILES = 10_000
_MARKER_WEIGHTS_MAX_DIRECTORIES = 20_000
_MARKER_WEIGHTS_MAX_TOTAL_BYTES = 100 * 1024 * 1024 * 1024
_MARKER_WEIGHTS_PATH_MAX_LENGTH = 512
_MARKER_WEIGHTS_PATH_MAX_DEPTH = 32


def _declared_runner_identity(runner: Callable[..., Any]) -> dict[str, Any] | None:
    identity_method = type(runner).__dict__.get("cache_identity")
    if not callable(identity_method):
        return None
    try:
        identity = identity_method(runner)
        if (
            not isinstance(identity, dict)
            or not identity
            or not isinstance(identity.get("provider_identity"), str)
            or not identity["provider_identity"].strip()
            or identity["provider_identity"].strip().casefold() in {"unknown", "unspecified", "none"}
        ):
            return None
        # Snapshot the declaration and reject values that cannot participate in
        # the application's deterministic JSON fingerprint.
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
        snapshot = json.loads(encoded)
    except Exception:
        return None
    return snapshot if isinstance(snapshot, dict) and snapshot else None


def _immutable_marker_image() -> str | None:
    image = os.environ.get("NEWSROOM_MARKER_DOCKER_IMAGE", "").strip()
    if not _MARKER_IMAGE_DIGEST_RE.fullmatch(image):
        return None
    # Arbitrary docker flags can replace the entrypoint, mounts, or model
    # configuration and therefore invalidate an otherwise complete identity.
    if os.environ.get("NEWSROOM_MARKER_DOCKER_ARGS", "").strip():
        return None
    return image


def _verified_marker_weights_identity() -> tuple[dict[str, Any], str] | None:
    root_raw = os.environ.get("NEWSROOM_MARKER_CACHE_DIR", "").strip()
    manifest_raw = os.environ.get("NEWSROOM_MARKER_WEIGHTS_MANIFEST", "").strip()
    if not root_raw or not manifest_raw:
        return None
    root = Path(root_raw)
    manifest_path = Path(manifest_raw)
    if not root.is_absolute() or not manifest_path.is_absolute():
        return None
    if _path_has_link(root) or _path_has_link(manifest_path):
        return None
    try:
        root = root.resolve(strict=True)
        manifest_path = manifest_path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not root.is_dir() or not manifest_path.is_file():
        return None
    try:
        manifest_path.relative_to(root)
    except ValueError:
        pass
    else:
        # Keeping the manifest outside the mounted tree makes exact file-set
        # validation possible without a self-referential checksum.
        return None

    raw = _read_stable_bounded_file(
        manifest_path,
        max_bytes=_MARKER_WEIGHTS_MANIFEST_MAX_BYTES,
    )
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    entries = _validated_marker_manifest_entries(payload)
    if entries is None:
        return None
    declared_total_bytes = sum(entry["size"] for entry in entries.values())
    if declared_total_bytes > _MARKER_WEIGHTS_MAX_TOTAL_BYTES:
        return None

    actual_files = _marker_regular_files(root)
    if actual_files is None or set(actual_files) != set(entries):
        return None
    for relative_path, expected in entries.items():
        file_path = actual_files[relative_path]
        digest = _sha256_file(file_path, expected_size=expected["size"])
        if digest is None or digest != expected["sha256"]:
            return None

    canonical_entries = [
        {"path": path, "sha256": entries[path]["sha256"], "size": entries[path]["size"]}
        for path in sorted(entries)
    ]
    canonical = json.dumps(
        {"schema": _MARKER_WEIGHTS_MANIFEST_SCHEMA, "files": canonical_entries},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ({
        "schema": _MARKER_WEIGHTS_MANIFEST_SCHEMA,
        "verified_manifest_sha256": sha256(canonical).hexdigest(),
        "file_count": len(canonical_entries),
        "total_bytes": declared_total_bytes,
    }, str(root))


def _runner_binds_marker_weights(
    runner_identity: dict[str, Any],
    *,
    host_cache_dir: str,
    weights_identity: dict[str, Any],
) -> bool:
    return runner_identity.get("marker_weights_binding") == {
        "schema": _MARKER_WEIGHTS_BINDING_SCHEMA,
        "host_cache_dir": host_cache_dir,
        "container_cache_dir": _MARKER_WEIGHTS_CONTAINER_CACHE_DIR,
        "verified_manifest_sha256": weights_identity["verified_manifest_sha256"],
    }


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def _validated_marker_manifest_entries(
    payload: Any,
) -> dict[str, dict[str, Any]] | None:
    if not isinstance(payload, dict) or set(payload) != {"schema", "files"}:
        return None
    if payload["schema"] != _MARKER_WEIGHTS_MANIFEST_SCHEMA:
        return None
    files = payload["files"]
    if not isinstance(files, list) or not files or len(files) > _MARKER_WEIGHTS_MANIFEST_MAX_FILES:
        return None
    entries: dict[str, dict[str, Any]] = {}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            return None
        relative = entry["path"]
        digest = entry["sha256"]
        size = entry["size"]
        if (
            not isinstance(relative, str)
            or not relative
            or len(relative) > _MARKER_WEIGHTS_PATH_MAX_LENGTH
            or "\\" in relative
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            return None
        path = PurePosixPath(relative)
        if path.is_absolute() or len(path.parts) > _MARKER_WEIGHTS_PATH_MAX_DEPTH:
            return None
        if any(part in {"", ".", ".."} for part in path.parts) or relative in entries:
            return None
        entries[relative] = {"sha256": digest, "size": size}
    return entries


def _marker_regular_files(root: Path) -> dict[str, Path] | None:
    files: dict[str, Path] = {}
    try:
        visited_directories = 0
        for current_raw, directory_names, file_names in os.walk(root, followlinks=False):
            visited_directories += 1
            if visited_directories > _MARKER_WEIGHTS_MAX_DIRECTORIES:
                return None
            current = Path(current_raw)
            if len(current.relative_to(root).parts) > _MARKER_WEIGHTS_PATH_MAX_DEPTH:
                return None
            if len(directory_names) + len(file_names) > _MARKER_WEIGHTS_MANIFEST_MAX_FILES:
                return None
            for name in directory_names:
                directory = current / name
                if _is_link(directory):
                    return None
            for name in file_names:
                candidate = current / name
                if _is_link(candidate):
                    return None
                candidate_stat = candidate.stat()
                if not stat.S_ISREG(candidate_stat.st_mode):
                    return None
                relative = candidate.relative_to(root).as_posix()
                if len(relative) > _MARKER_WEIGHTS_PATH_MAX_LENGTH:
                    return None
                files[relative] = candidate
                if len(files) > _MARKER_WEIGHTS_MANIFEST_MAX_FILES:
                    return None
    except (OSError, RuntimeError, ValueError):
        return None
    return files


def _path_has_link(path: Path) -> bool:
    current = path
    while True:
        if _is_link(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _sha256_file(path: Path, *, expected_size: int) -> str | None:
    if expected_size < 0 or _path_has_link(path):
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(path, flags)
        before = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != expected_size
            or before.st_size > _MARKER_WEIGHTS_MAX_TOTAL_BYTES
        ):
            return None
        digest = sha256()
        total_read = 0
        while total_read <= expected_size:
            chunk = os.read(file_descriptor, min(1024 * 1024, expected_size + 1 - total_read))
            if not chunk:
                break
            total_read += len(chunk)
            if total_read > expected_size:
                return None
            digest.update(chunk)
        if total_read != expected_size:
            return None
        after = os.fstat(file_descriptor)
        if not _same_file_snapshot(before, after) or _path_has_link(path):
            return None
        if not _same_file_snapshot(after, path.stat()):
            return None
        return digest.hexdigest()
    except (OSError, OverflowError, ValueError):
        return None
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass


def _read_stable_bounded_file(
    path: Path,
    *,
    max_bytes: int,
) -> bytes | None:
    if max_bytes < 0 or _path_has_link(path):
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(path, flags)
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None
        raw = _read_bounded_fd(file_descriptor, max_bytes=max_bytes)
        if raw is None:
            return None
        after = os.fstat(file_descriptor)
        if not _same_file_snapshot(before, after) or _path_has_link(path):
            return None
        if not _same_file_snapshot(after, path.stat()):
            return None
        return raw
    except (OSError, OverflowError, ValueError):
        return None
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass


def _read_bounded_fd(file_descriptor: int, *, max_bytes: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        chunk = os.read(file_descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    return None if len(raw) > max_bytes else raw


def _same_file_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_size,
        first.st_mtime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_size,
        second.st_mtime_ns,
    )



def verified_marker_cache_identity(runner: Callable[..., Any]) -> dict[str, Any] | None:
    """Verify Marker image, local weights, and runner-declared mount binding."""
    runner_identity = _declared_runner_identity(runner)
    image = _immutable_marker_image()
    verified_weights = _verified_marker_weights_identity()
    if runner_identity is None or image is None or verified_weights is None:
        return None
    weights_identity, host_cache_dir = verified_weights
    if not _runner_binds_marker_weights(
        runner_identity,
        host_cache_dir=host_cache_dir,
        weights_identity=weights_identity,
    ):
        return None
    return {
        "type": "backend.research.document.marker_pdf_parser.MarkerPdfDocumentParser",
        "execution_adapter": runner_identity,
        "marker_image": image,
        "marker_weights": weights_identity,
    }
