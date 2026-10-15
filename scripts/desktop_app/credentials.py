from __future__ import annotations

import argparse
import ctypes
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable

from scripts.local_desktop.common import DesktopOperationError, reject_reparse_path

APPLICATION_ID = "AgoraHub.NewsRoom.PaperAgent"
FIXED_CREDENTIAL_REF = "paper-agent/research/openai-compatible"
FIXED_CREDENTIAL_ENV = "NEWSROOM_DESKTOP_RESEARCH_API_KEY"
_NAMESPACE_VERSION = "v1"
_CREDENTIAL_USERNAME = "agora-paper-agent-research"
_MAX_SECRET_BYTES = 2048
_ERROR_NOT_FOUND = 1168
_ERROR_CANCELLED = 1223
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_CREDUI_MAX_USERNAME_LENGTH = 513
_CREDUI_MAX_PASSWORD_LENGTH = 256
_CREDUI_FLAGS_DO_NOT_PERSIST = 0x00002
_CREDUI_FLAGS_ALWAYS_SHOW_UI = 0x00080
_CREDUI_FLAGS_PASSWORD_ONLY_OK = 0x00200
_CREDUI_FLAGS_GENERIC_CREDENTIALS = 0x40000
_CREDUI_FLAGS_KEEP_USERNAME = 0x100000


class CredentialError(RuntimeError):
    """A bounded credential operation failure that never includes secret material."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CredentialSlot(StrEnum):
    """The single Research-compatible credential slot owned by the desktop App."""

    RESEARCH_OPENAI_COMPATIBLE = FIXED_CREDENTIAL_REF

    @property
    def environment_name(self) -> str:
        return FIXED_CREDENTIAL_ENV


RESEARCH_CREDENTIAL_SLOT = CredentialSlot.RESEARCH_OPENAI_COMPATIBLE


@dataclass(frozen=True)
class CredentialDescriptor:
    slot: CredentialSlot
    target_name: str
    credential_ref: str
    environment_name: str
    data_root: Path


@dataclass(frozen=True)
class CredentialPresence:
    present: bool


@dataclass(frozen=True)
class CredentialPromptResult:
    changed: bool
    present: bool
    cancelled: bool


@runtime_checkable
class CredentialVault(Protocol):
    def inspect(self, descriptor: CredentialDescriptor) -> CredentialPresence: ...

    def read(self, descriptor: CredentialDescriptor) -> str | None: ...

    def write(self, descriptor: CredentialDescriptor, secret: str) -> None: ...

    def delete(self, descriptor: CredentialDescriptor) -> bool: ...

    def prompt_and_replace(self, descriptor: CredentialDescriptor, *, parent_window: int = 0) -> CredentialPromptResult: ...


def descriptor_for(
    data_root: str | os.PathLike[str],
    slot: CredentialSlot = RESEARCH_CREDENTIAL_SLOT,
) -> CredentialDescriptor:
    if slot is not RESEARCH_CREDENTIAL_SLOT:
        raise CredentialError("desktop_credential_slot_invalid", "credential slot is not allowed")
    lexical = _lexical_data_root(data_root)
    _reject_reparse_data_root(lexical)
    namespace = sha256(os.path.normcase(os.fspath(lexical)).encode("utf-8", errors="strict")).hexdigest()
    target_name = f"{APPLICATION_ID}/{_NAMESPACE_VERSION}/{namespace}/{FIXED_CREDENTIAL_REF}"
    return CredentialDescriptor(
        slot=slot,
        target_name=target_name,
        credential_ref=FIXED_CREDENTIAL_REF,
        environment_name=FIXED_CREDENTIAL_ENV,
        data_root=lexical,
    )


def inspect_slot(
    data_root: str | os.PathLike[str],
    *,
    vault: CredentialVault | None = None,
) -> CredentialPresence:
    return (vault or WindowsCredentialVault()).inspect(descriptor_for(data_root))


def read_slot_for_owner(
    data_root: str | os.PathLike[str],
    *,
    vault: CredentialVault | None = None,
) -> str | None:
    """Read only for the configuration/launch owner; never project this value."""

    return (vault or WindowsCredentialVault()).read(descriptor_for(data_root))


def replace_slot_with_native_prompt(
    data_root: str | os.PathLike[str],
    *,
    parent_window: int = 0,
    vault: CredentialVault | None = None,
) -> CredentialPromptResult:
    if isinstance(parent_window, bool) or not isinstance(parent_window, int) or parent_window < 0 or parent_window > (2**64 - 1):
        raise CredentialError("desktop_credential_parent_invalid", "native parent window is invalid")
    return (vault or WindowsCredentialVault()).prompt_and_replace(
        descriptor_for(data_root),
        parent_window=parent_window,
    )


def delete_slot(
    data_root: str | os.PathLike[str],
    *,
    vault: CredentialVault | None = None,
) -> CredentialPresence:
    selected = vault or WindowsCredentialVault()
    descriptor = descriptor_for(data_root)
    selected.delete(descriptor)
    presence = selected.inspect(descriptor)
    if presence.present:
        raise CredentialError(
            "desktop_credential_delete_unconfirmed",
            "credential vault still reports the fixed Research slot after deletion",
        )
    return presence


class AppCredentialProvider:
    """Adapter for RuntimeConfigManager's fixed configured/project_environment protocol."""

    def __init__(self, data_root: str | os.PathLike[str], *, vault: CredentialVault | None = None) -> None:
        self._descriptor = descriptor_for(data_root)
        self._vault = vault or WindowsCredentialVault()

    def configured(self, credential_ref: str) -> bool:
        self._validate_binding(credential_ref, FIXED_CREDENTIAL_ENV)
        return self._vault.inspect(self._descriptor).present

    def project_environment(self, credential_ref: str, *, environment_name: str) -> Mapping[str, str]:
        self._validate_binding(credential_ref, environment_name)
        secret = self._vault.read(self._descriptor)
        return {} if secret is None else {FIXED_CREDENTIAL_ENV: secret}

    @staticmethod
    def _validate_binding(credential_ref: str, environment_name: str) -> None:
        if credential_ref != FIXED_CREDENTIAL_REF or environment_name != FIXED_CREDENTIAL_ENV:
            raise CredentialError("desktop_credential_binding_invalid", "credential binding is not allowed")


def bind_launch_environment(
    base: Mapping[str, str],
    *,
    data_root: str | os.PathLike[str],
    vault: CredentialVault | None = None,
) -> dict[str, str]:
    """Clear inherited managed material and inject only the App-owned Research key."""

    environment = {str(key): str(value) for key, value in base.items() if str(key).upper() != FIXED_CREDENTIAL_ENV}
    provider = AppCredentialProvider(data_root, vault=vault)
    environment.update(provider.project_environment(FIXED_CREDENTIAL_REF, environment_name=FIXED_CREDENTIAL_ENV))
    return environment


def _lexical_data_root(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise CredentialError("desktop_credential_data_root_invalid", "data root must be a non-empty absolute path")
    path = Path(raw)
    if not path.is_absolute():
        raise CredentialError("desktop_credential_data_root_invalid", "data root must be an absolute path")
    if ".." in path.parts:
        raise CredentialError("desktop_credential_data_root_invalid", "data root must not contain parent traversal")
    normalized = Path(os.path.normpath(raw))
    if normalized == Path(normalized.anchor):
        raise CredentialError("desktop_credential_data_root_invalid", "data root cannot be a filesystem root")
    return normalized


def _reject_reparse_data_root(data_root: Path) -> None:
    try:
        reject_reparse_path(data_root, root=data_root)
    except DesktopOperationError as exc:
        raise CredentialError("desktop_credential_data_root_untrusted", "data root must not traverse a symbolic link or junction") from exc


def _validate_descriptor(descriptor: CredentialDescriptor) -> None:
    if not isinstance(descriptor, CredentialDescriptor):
        raise CredentialError("desktop_credential_descriptor_invalid", "credential descriptor was not issued by the App")
    expected = descriptor_for(descriptor.data_root)
    if descriptor != expected:
        raise CredentialError("desktop_credential_descriptor_invalid", "credential descriptor was not issued by the App")


def _validate_secret(secret: str) -> bytearray:
    if not isinstance(secret, str) or not secret or secret != secret.strip() or any(character in secret for character in ("\x00", "\r", "\n")):
        raise CredentialError("desktop_credential_value_invalid", "credential value is empty or malformed")
    encoded = bytearray(secret.encode("utf-8", errors="strict"))
    if len(encoded) > _MAX_SECRET_BYTES:
        _zero_bytearray(encoded)
        raise CredentialError("desktop_credential_value_invalid", "credential value exceeds the supported size")
    return encoded


def _zero_bytearray(value: bytearray) -> None:
    if value:
        ctypes.memset((ctypes.c_ubyte * len(value)).from_buffer(value), 0, len(value))


class WindowsCredentialVault:
    """Exact-target Generic Credential adapter backed by Windows Credential Manager."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialError("desktop_credential_platform_unsupported", "Windows Credential Manager is required")
        from ctypes import wintypes

        class CREDENTIALW(ctypes.Structure):
            pass

        self._credential_type = CREDENTIALW
        self._pcredential = ctypes.POINTER(CREDENTIALW)
        CREDENTIALW._fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

        class CREDUI_INFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hwndParent", wintypes.HWND),
                ("pszMessageText", wintypes.LPCWSTR),
                ("pszCaptionText", wintypes.LPCWSTR),
                ("hbmBanner", wintypes.HBITMAP),
            ]

        self._credui_info_type = CREDUI_INFOW
        self._advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self._credui = ctypes.WinDLL("credui", use_last_error=True)
        self._advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(self._pcredential)]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None
        self._credui.CredUIPromptForCredentialsW.argtypes = [
            ctypes.POINTER(CREDUI_INFOW), wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
            wintypes.LPWSTR, wintypes.ULONG, wintypes.LPWSTR, wintypes.ULONG,
            ctypes.POINTER(wintypes.BOOL), wintypes.DWORD,
        ]
        self._credui.CredUIPromptForCredentialsW.restype = wintypes.DWORD

    def inspect(self, descriptor: CredentialDescriptor) -> CredentialPresence:
        _validate_descriptor(descriptor)
        pointer = self._read_pointer(descriptor.target_name)
        if pointer is None:
            return CredentialPresence(False)
        self._advapi.CredFree(pointer)
        return CredentialPresence(True)

    def read(self, descriptor: CredentialDescriptor) -> str | None:
        _validate_descriptor(descriptor)
        pointer = self._read_pointer(descriptor.target_name)
        if pointer is None:
            return None
        try:
            credential = pointer.contents
            if credential.Type != _CRED_TYPE_GENERIC or credential.TargetName != descriptor.target_name:
                raise CredentialError("desktop_credential_record_invalid", "credential record does not match the requested slot")
            size = int(credential.CredentialBlobSize)
            if size <= 0 or size > _MAX_SECRET_BYTES or not credential.CredentialBlob:
                raise CredentialError("desktop_credential_record_invalid", "credential record has an invalid payload")
            payload = bytearray(ctypes.string_at(credential.CredentialBlob, size))
            try:
                return payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise CredentialError("desktop_credential_record_invalid", "credential record encoding is invalid") from exc
            finally:
                _zero_bytearray(payload)
        finally:
            self._advapi.CredFree(pointer)

    def write(self, descriptor: CredentialDescriptor, secret: str) -> None:
        _validate_descriptor(descriptor)
        payload = _validate_secret(secret)
        try:
            blob = (ctypes.c_ubyte * len(payload)).from_buffer(payload)
            credential = self._credential_type()
            credential.Flags = 0
            credential.Type = _CRED_TYPE_GENERIC
            credential.TargetName = descriptor.target_name
            credential.Comment = f"{APPLICATION_ID} managed Research credential"
            credential.CredentialBlobSize = len(payload)
            credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
            credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
            credential.AttributeCount = 0
            credential.Attributes = None
            credential.TargetAlias = None
            credential.UserName = _CREDENTIAL_USERNAME
            if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
                raise CredentialError("desktop_credential_write_failed", f"Windows Credential Manager write failed ({ctypes.get_last_error()})")
        finally:
            _zero_bytearray(payload)

    def delete(self, descriptor: CredentialDescriptor) -> bool:
        _validate_descriptor(descriptor)
        if self._advapi.CredDeleteW(descriptor.target_name, _CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return False
        raise CredentialError("desktop_credential_delete_failed", f"Windows Credential Manager delete failed ({error})")

    def prompt_and_replace(self, descriptor: CredentialDescriptor, *, parent_window: int = 0) -> CredentialPromptResult:
        _validate_descriptor(descriptor)
        from ctypes import wintypes

        user_name = ctypes.create_unicode_buffer(_CREDENTIAL_USERNAME, _CREDUI_MAX_USERNAME_LENGTH + 1)
        password = ctypes.create_unicode_buffer(_CREDUI_MAX_PASSWORD_LENGTH + 1)
        save = wintypes.BOOL(False)
        info = self._credui_info_type(
            ctypes.sizeof(self._credui_info_type),
            wintypes.HWND(parent_window or 0),
            "请在密码字段中输入 Research API 密钥。密钥仅保存到 Windows Credential Manager，不会发送到网页或日志。",
            "Agora Paper Agent Research 密钥",
            None,
        )
        flags = _CREDUI_FLAGS_DO_NOT_PERSIST | _CREDUI_FLAGS_ALWAYS_SHOW_UI | _CREDUI_FLAGS_PASSWORD_ONLY_OK | _CREDUI_FLAGS_GENERIC_CREDENTIALS | _CREDUI_FLAGS_KEEP_USERNAME
        try:
            result = int(self._credui.CredUIPromptForCredentialsW(
                ctypes.byref(info), descriptor.target_name, None, 0,
                user_name, len(user_name), password, len(password), ctypes.byref(save), flags,
            ))
            if result == _ERROR_CANCELLED:
                return CredentialPromptResult(changed=False, present=self.inspect(descriptor).present, cancelled=True)
            if result != 0:
                raise CredentialError("desktop_credential_prompt_failed", f"Windows credential prompt failed ({result})")
            self.write(descriptor, password.value)
            return CredentialPromptResult(changed=True, present=True, cancelled=False)
        finally:
            ctypes.memset(ctypes.addressof(password), 0, ctypes.sizeof(password))
            ctypes.memset(ctypes.addressof(user_name), 0, ctypes.sizeof(user_name))

    def _read_pointer(self, target_name: str):
        pointer = self._pcredential()
        if self._advapi.CredReadW(target_name, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return pointer
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return None
        raise CredentialError("desktop_credential_read_failed", f"Windows Credential Manager read failed ({error})")


def _native_command(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--parent-window", type=int, default=0)
    parser.add_argument("command", choices=("status", "replace", "delete"))
    try:
        args = parser.parse_args(argv)
        if args.command == "status":
            presence = inspect_slot(args.data_root)
            payload = {"ok": True, "code": "credential_status", "configured": presence.present}
        elif args.command == "replace":
            result = replace_slot_with_native_prompt(args.data_root, parent_window=args.parent_window)
            payload = {"ok": True, "code": "credential_cancelled" if result.cancelled else "credential_replaced", "configured": result.present, "changed": result.changed, "cancelled": result.cancelled}
        else:
            before = inspect_slot(args.data_root)
            presence = delete_slot(args.data_root)
            payload = {"ok": True, "code": "credential_deleted", "configured": presence.present, "changed": before.present, "cancelled": False}
    except CredentialError as exc:
        payload = {"ok": False, "code": exc.code, "configured": None, "changed": False, "cancelled": False}
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(_native_command())


__all__ = [
    "APPLICATION_ID",
    "FIXED_CREDENTIAL_ENV",
    "FIXED_CREDENTIAL_REF",
    "AppCredentialProvider",
    "CredentialDescriptor",
    "CredentialError",
    "CredentialPresence",
    "CredentialPromptResult",
    "CredentialSlot",
    "CredentialVault",
    "RESEARCH_CREDENTIAL_SLOT",
    "WindowsCredentialVault",
    "bind_launch_environment",
    "delete_slot",
    "descriptor_for",
    "inspect_slot",
    "read_slot_for_owner",
    "replace_slot_with_native_prompt",
]
