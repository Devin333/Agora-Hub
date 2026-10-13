from __future__ import annotations

import json
import os
import stat
import sys
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from framework.shared.redaction import redact_sensitive_values


class DesktopOperationError(RuntimeError):
    def __init__(self, reason_code: str, message: str, *, exit_code: int = 2, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.exit_code = exit_code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "reason_code": self.reason_code,
            "message": str(self),
            "details": redact_sensitive_values(self.details),
        }


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    action: str
    status: str
    reason_code: str
    message: str
    details: dict[str, Any]
    exit_code: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "status": self.status,
            "reason_code": self.reason_code,
            "message": self.message,
            "details": redact_sensitive_values(self.details),
        }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    handle = None
    try:
        protect_private_path(temp)
        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = None
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temp, path)
        protect_private_path(path)
    finally:
        preserving_exception = sys.exc_info()[0] is not None
        cleanup_error: OSError | None = None
        try:
            if handle is not None:
                handle.close()
            elif descriptor is not None:
                os.close(descriptor)
        except OSError as exc:
            cleanup_error = exc
        try:
            temp.unlink(missing_ok=True)
        except OSError as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is not None and not preserving_exception:
            raise cleanup_error



def read_json_object(path: Path, *, missing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(missing or {})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopOperationError("desktop_metadata_invalid", f"invalid desktop metadata: {path.name}") from exc
    if not isinstance(payload, dict):
        raise DesktopOperationError("desktop_metadata_invalid", f"desktop metadata must be an object: {path.name}")
    return payload


def protect_private_path(path: Path) -> None:
    """Protect exactly one owned path; directories secure subsequently created children."""
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
        return
    try:
        import ntsecuritycon
        import win32security
    except ImportError as exc:
        raise DesktopOperationError("desktop_acl_protection_failed", "Windows ACL support is unavailable") from exc
    current_sid = win32security.ConvertStringSidToSid(_windows_current_user_sid())
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise DesktopOperationError("desktop_acl_protection_failed", "private Windows path is a reparse point")
        existing = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION,
        )
        owner_sid = existing.GetSecurityDescriptorOwner()
        if owner_sid is None or owner_sid != current_sid:
            raise DesktopOperationError("desktop_acl_path_not_owned", "refusing to change a Windows ACL not owned by the current user")
        acl = win32security.ACL()
        inheritance = (
            win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
            if path.is_dir()
            else 0
        )
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            inheritance,
            ntsecuritycon.FILE_ALL_ACCESS,
            current_sid,
        )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )
    except DesktopOperationError:
        raise
    except (OSError, RuntimeError) as exc:
        raise DesktopOperationError("desktop_acl_protection_failed", "could not apply private Windows ACL") from exc


def _windows_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user = 1
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    process = kernel32.GetCurrentProcess()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process, token_query, ctypes.byref(token)):
        raise DesktopOperationError("desktop_acl_identity_failed", "could not resolve the current Windows identity")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(required))
        if not required.value:
            raise DesktopOperationError("desktop_acl_identity_failed", "could not resolve the current Windows identity")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(token, token_user, buffer, required, ctypes.byref(required)):
            raise DesktopOperationError("desktop_acl_identity_failed", "could not resolve the current Windows identity")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise DesktopOperationError("desktop_acl_identity_failed", "could not resolve the current Windows identity")
        try:
            return str(sid_text.value)
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


@contextmanager
def exclusive_lock(path: Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    handle = path.open("a+b")
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise DesktopOperationError("desktop_operation_busy", "another local desktop operation owns the instance lock", exit_code=4) from exc
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def ensure_contained(root: Path, candidate: Path, *, field: str) -> Path:
    resolved_root = root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate == resolved_root or resolved_candidate.is_relative_to(resolved_root):
        return resolved_candidate
    raise DesktopOperationError("desktop_path_escape", f"{field} escapes its owned root")


def reject_reparse_path(path: Path, *, root: Path) -> None:
    current = path.resolve(strict=False)
    owned_root = root.resolve(strict=False)
    if not (current == owned_root or current.is_relative_to(owned_root)):
        raise DesktopOperationError("desktop_path_escape", "path escapes its owned root")
    probe = path
    while probe != probe.parent and (probe == root or probe.is_relative_to(root)):
        if probe.exists() and probe.is_symlink():
            raise DesktopOperationError("desktop_reparse_point_rejected", f"symbolic link is not allowed: {probe.name}")
        if os.name == "nt" and probe.exists():
            attributes = getattr(probe.stat(), "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if attributes & reparse:
                raise DesktopOperationError("desktop_reparse_point_rejected", f"reparse point is not allowed: {probe.name}")
        if probe == root:
            break
        probe = probe.parent
