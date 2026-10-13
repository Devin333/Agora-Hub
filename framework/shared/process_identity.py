from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_id: str

    def to_dict(self) -> dict[str, object]:
        return {"pid": self.pid, "creationId": self.creation_id}


def current_process_identity() -> ProcessIdentity:
    identity = process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("current process identity is unavailable")
    return identity


def process_identity(pid: int) -> ProcessIdentity | None:
    actual = _valid_pid(pid)
    if actual is None:
        return None
    if sys.platform == "win32":
        return _windows_process_identity(actual)
    creation_id = _procfs_creation_id(actual)
    if creation_id is None or not _posix_process_exists(actual):
        return None
    return ProcessIdentity(pid=actual, creation_id=creation_id)


def process_matches(pid: object, creation_id: object) -> bool:
    actual = _valid_pid(pid)
    expected = str(creation_id or "").strip()
    if actual is None or not expected:
        return False
    identity = process_identity(actual)
    return identity is not None and identity.creation_id == expected


def _windows_process_identity(pid: int) -> ProcessIdentity | None:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize,
        False,
        pid,
    )
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != still_active:
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return ProcessIdentity(pid=pid, creation_id=f"windows-filetime:{ticks}")
    finally:
        kernel32.CloseHandle(handle)


def _procfs_creation_id(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            value = handle.read()
    except (OSError, UnicodeError):
        return None
    closing = value.rfind(")")
    if closing < 0:
        return None
    fields = value[closing + 2 :].split()
    if len(fields) <= 19 or not fields[19].isdigit():
        return None
    return f"procfs-starttime:{fields[19]}"


def _posix_process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False
    return True


def _valid_pid(value: object) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


__all__ = ["ProcessIdentity", "current_process_identity", "process_identity", "process_matches"]
