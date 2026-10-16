from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from collections.abc import Callable, Sequence

from framework.shared.local_desktop import LocalDesktopConfigError, validate_lifecycle_timeout
from scripts.desktop_app.config import resolve_desktop_app_config
from scripts.desktop_app.owned_clear import OwnedDataClearOwner
from scripts.desktop_app.protocol import MAX_PROTOCOL_BYTES, CommandResponse, project_error, project_operation, read_bounded_stdin_json
from scripts.desktop_app.runtime import DesktopAppRuntime
from scripts.local_desktop.common import DesktopOperationError
from scripts.local_desktop.lifecycle_cancellation import check_startup_cancellation, startup_cancellation_context
from scripts.local_desktop.runtime_config import parse_candidate_json


_FINALIZER_READY_EMITTED = False


class CliUsageError(ValueError):
    reason_code = "desktop_app_cli_invalid"
    exit_code = 2


class JsonArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        if "color" in argparse.HelpFormatter.__init__.__code__.co_varnames:
            kwargs.setdefault("formatter_class", lambda prog: argparse.HelpFormatter(prog, color=False))
            kwargs.setdefault("color", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        raise CliUsageError(message)


def _timeout(value: str) -> float:
    try:
        return validate_lifecycle_timeout(value, field_name="timeout")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _bounded_confirmation(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 512 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise argparse.ArgumentTypeError("confirmation must be a non-empty bounded token")
    return value


def _config_candidate(value: str) -> dict[str, object]:
    try:
        return parse_candidate_json(value)
    except DesktopOperationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc



def _parent_window(value: str) -> int:
    try:
        parent = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("parent window must be a positive bounded integer") from exc
    if parent < 1 or parent > (2**63 - 1):
        raise argparse.ArgumentTypeError("parent window must be a positive bounded integer")
    return parent

def _non_negative_revision(value: str) -> int:
    try:
        revision = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected revision must be a non-negative integer") from exc
    if revision < 0:
        raise argparse.ArgumentTypeError("expected revision must be a non-negative integer")
    return revision


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="python -m scripts.desktop_app", add_help=False)
    parser.add_argument("--json", action="store_true", help="emit the bounded desktop host JSON protocol")
    parser.add_argument("--resource-root", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--frontend-port", type=int, default=3120)
    parser.add_argument("--api-port", type=int, default=8120)
    parser.add_argument("--timeout", type=_timeout, default=60.0)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start", add_help=False)
    commands.add_parser("status", add_help=False)
    commands.add_parser("data-clear-preview", add_help=False)
    commands.add_parser("data-clear-apply", add_help=False)
    commands.add_parser("data-clear-status", add_help=False)
    commands.add_parser("data-clear-finalize", add_help=False)
    commands.add_parser("stop", add_help=False)
    commands.add_parser("doctor", add_help=False)
    backup = commands.add_parser("backup", add_help=False)
    backup.add_argument("--destination")
    commands.add_parser("inventory", add_help=False)
    commands.add_parser("config-show", add_help=False)
    commands.add_parser("credential-inspect", add_help=False)
    credential_prompt = commands.add_parser("credential-prompt", add_help=False)
    credential_prompt.add_argument("--parent-window", required=True, type=_parent_window)
    credential_delete = commands.add_parser("credential-delete", add_help=False)
    credential_delete.add_argument("--confirmation-token", required=True, type=_bounded_confirmation)
    config_preview = commands.add_parser("config-preview", add_help=False)
    config_preview.add_argument("--candidate-json", required=True, type=_config_candidate)
    config_apply = commands.add_parser("config-apply", add_help=False)
    config_apply.add_argument("--candidate-json", required=True, type=_config_candidate)
    config_apply.add_argument("--expected-revision", required=True, type=_non_negative_revision)
    config_apply.add_argument("--confirmation-token", required=True, type=_bounded_confirmation)
    build = commands.add_parser("build", add_help=False)
    build.add_argument("--bundle-output")
    build.add_argument("--portable-python-root")
    build.add_argument("--portable-node-root")
    build.add_argument("--release")
    restore = commands.add_parser("restore", add_help=False)
    restore.add_argument("--backup-path", required=True)
    restore.add_argument("--target-root")
    restore.add_argument("--activate", action="store_true")
    restore.add_argument("--personal-memory-confirmation", type=_bounded_confirmation)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: Callable[..., DesktopAppRuntime] = DesktopAppRuntime,
    clear_owner_factory: Callable[..., OwnedDataClearOwner] = OwnedDataClearOwner,
) -> int:
    global _FINALIZER_READY_EMITTED
    _FINALIZER_READY_EMITTED = False
    raw = list(argv) if argv is not None else sys.argv[1:]
    command = _command_hint(raw)
    config = None
    canonical_origin = api_base_url = instance_key = None
    try:
        args = build_parser().parse_args(raw)
        command = str(args.command)
        canonical_origin = f"http://127.0.0.1:{int(args.frontend_port)}"
        api_base_url = f"http://127.0.0.1:{int(args.api_port)}"
        instance_key = f"local-desktop:{canonical_origin}"
        if command in {"data-clear-preview", "data-clear-apply", "data-clear-status", "data-clear-finalize"}:
            _require_packaged_resource_isolation(args.resource_root)
            owner = clear_owner_factory(resource_root=args.resource_root, data_root=args.data_root)
            if command == "data-clear-status":
                result = owner.status()
            elif command == "data-clear-apply":
                result = owner.apply(read_bounded_stdin_json())
            elif command == "data-clear-finalize":
                result = owner.finalize(read_bounded_stdin_json(), ready=_emit_finalizer_ready)
            else:
                registration = owner.validate_registered_profile(allow_partial=True)
                retry_preview = registration.get("state") == "PREVIEW" and bool(registration.get("retryOf"))
                if registration.get("state") in {"PREPARED", "FAILED_PARTIAL", "FINALIZING"} or retry_preview:
                    result = owner.preview(stop_runtime=None, timeout=args.timeout)
                else:
                    config = resolve_desktop_app_config(resource_root=args.resource_root, data_root=args.data_root, frontend_port=args.frontend_port, api_port=args.api_port)
                    _require_packaged_launch_isolation(config)
                    result = runtime_factory(config).data_clear_preview(timeout=args.timeout)
        else:
            startup_control = startup_cancellation_context() if command == "start" else nullcontext()
            with startup_control:
                result, config = _run_normal_command(args, command, runtime_factory=runtime_factory, clear_owner_factory=clear_owner_factory)
        response = project_operation(result, command=command, canonical_origin=config.desktop.canonical_origin if config else canonical_origin, api_base_url=config.desktop.api_base_url if config else api_base_url, instance_key=config.desktop.instance_key if config else instance_key, extra={"storage_mode": config.desktop.storage_mode if config else "owned-local"})
    except (DesktopOperationError, LocalDesktopConfigError, CliUsageError, ValueError) as exc:
        if command.startswith("data-clear-"):
            code = str(getattr(exc, "reason_code", "desktop_data_clear_failed"))
            response = CommandResponse(False, command, "ERROR", code, _clear_public_error_message(code), {"canonical_origin": config.desktop.canonical_origin if config else canonical_origin, "api_base_url": config.desktop.api_base_url if config else api_base_url, "instance_key": config.desktop.instance_key if config else instance_key, "details": getattr(exc, "details", {})}, int(getattr(exc, "exit_code", 2) or 2))
        else:
            response = project_error(exc, command=command, canonical_origin=config.desktop.canonical_origin if config else canonical_origin, api_base_url=config.desktop.api_base_url if config else api_base_url, instance_key=config.desktop.instance_key if config else instance_key)
    except Exception:
        response = CommandResponse(False, command, "ERROR", "desktop_app_internal_error", "desktop app command failed unexpectedly", {"canonical_origin": config.desktop.canonical_origin if config else canonical_origin, "api_base_url": config.desktop.api_base_url if config else api_base_url, "instance_key": config.desktop.instance_key if config else instance_key}, 1)
    _emit(response)
    return response.exit_code if not response.ok else 0


def _run_normal_command(args, command: str, *, runtime_factory, clear_owner_factory):
    if command == "start":
        check_startup_cancellation()
    fixed_layout = Path(args.data_root).is_absolute() and Path(args.data_root).name.casefold() == "runtime"
    owner = clear_owner_factory(resource_root=args.resource_root, data_root=args.data_root) if fixed_layout else None
    writer_commands = {"start", "stop", "backup", "config-preview", "config-apply", "credential-prompt", "credential-delete", "build", "restore"}
    lease = owner.normal_command_guard(writer=command in writer_commands) if owner is not None else nullcontext()
    with lease:
        if command == "start":
            check_startup_cancellation()
        config = resolve_desktop_app_config(resource_root=args.resource_root, data_root=args.data_root, frontend_port=args.frontend_port, api_port=args.api_port)
        _require_packaged_launch_isolation(config)
        if command == "start":
            check_startup_cancellation()
        runtime = runtime_factory(config)
        if owner is not None:
            setattr(runtime, "_desktop_clear_guard_held", True)
        if command == "start":
            check_startup_cancellation()
            result = runtime.start(timeout=args.timeout)
        elif command == "status":
            result = runtime.status()
        elif command == "stop":
            result = runtime.stop(timeout=args.timeout)
        elif command == "doctor":
            result = runtime.doctor()
        elif command == "backup":
            result = runtime.backup(destination=args.destination)
        elif command == "inventory":
            result = runtime.inventory()
        elif command == "config-show":
            result = runtime.config_show()
        elif command == "credential-inspect":
            result = runtime.credential_inspect()
        elif command == "credential-prompt":
            result = runtime.credential_prompt(parent_window=args.parent_window)
        elif command == "credential-delete":
            result = runtime.credential_delete(confirmation_token=args.confirmation_token, timeout=args.timeout)
        elif command == "config-preview":
            result = runtime.config_preview(candidate=args.candidate_json)
        elif command == "config-apply":
            result = runtime.config_apply(candidate=args.candidate_json, expected_revision=args.expected_revision, confirmation_token=args.confirmation_token, timeout=args.timeout)
        elif command == "build":
            result = runtime.build(bundle_output=args.bundle_output, portable_python_root=args.portable_python_root, portable_node_root=args.portable_node_root, release=args.release)
        elif command == "restore":
            result = runtime.restore(backup=args.backup_path, target_root=args.target_root, activate=args.activate, personal_memory_confirmation=args.personal_memory_confirmation)
        else:
            raise CliUsageError(f"unsupported command: {command}")
        return result, config

def _clear_public_error_message(code: str) -> str:
    messages = {
        "desktop_data_clear_marker_missing": "当前 profile 尚未建立可验证的清除所有权标记；未删除任何数据。",
        "desktop_data_clear_confirmation_mismatch": "确认文字不匹配；未删除任何数据。",
        "desktop_data_clear_preview_expired": "清除预览已过期，请重新预览并确认。",
        "desktop_data_clear_preview_replayed": "清除预览已失效或已使用，请重新预览并确认。",
        "desktop_data_clear_business_changed": "确认期间业务数据发生变化，请重新预览并确认。",
        "desktop_data_clear_snapshot_changed": "确认期间数据范围发生变化，请重新预览并确认。",
        "desktop_data_clear_start_blocked": "存在未完成的清除操作，正常启动已被安全阻断。",
        "desktop_data_clear_process_active": "仍有属于当前 profile 的进程在运行，无法安全清除。",
        "desktop_data_clear_process_not_owned": "发现无法确认所有权的进程，已拒绝清除。",
        "desktop_data_clear_reparse_rejected": "数据范围包含链接或重解析点，已拒绝清除。",
        "desktop_data_clear_hardlink_rejected": "数据范围包含硬链接文件，已拒绝清除。",
        "desktop_data_clear_delete_failed": "文件被占用、只读保护无法安全解除或存储拒绝操作；清除未完整完成。",
        "desktop_data_clear_finalizer_expired": "清除 finalizer 授权已过期，请重新预览并确认。",
        "desktop_data_clear_finalizer_capability_invalid": "清除 finalizer 授权无效。",
        "desktop_credential_delete_failed": "当前 profile 的专属凭据槽删除失败，清除未完整完成。",
    }
    return messages.get(code, "无法安全完成应用数据清除；未核验完成前不会报告成功。")

def _require_packaged_resource_isolation(resource_root: str | os.PathLike[str]) -> None:
    resource = Path(resource_root)
    manifest = resource / "desktop-bundle.json"
    if not manifest.is_file():
        return
    expected_python = resource / ".venv" / "Scripts" / "python.exe"
    if os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(os.path.abspath(expected_python)):
        raise DesktopOperationError("desktop_app_wrong_python", "packaged clear command must run with the bundled interpreter")
    inherited = [name for name in ("PYTHONHOME", "PYTHONPATH") if str(os.environ.get(name) or "").strip()]
    if inherited:
        raise DesktopOperationError("desktop_app_python_environment_unsafe", "packaged clear command must not inherit Python path overrides", details={"environment_names": inherited})
    if not sys.flags.no_user_site:
        raise DesktopOperationError("desktop_app_python_user_site_enabled", "packaged clear command requires user-site isolation")

def _require_packaged_launch_isolation(config) -> None:
    if not config.packaged:
        return
    inherited = [name for name in ("PYTHONHOME", "PYTHONPATH") if str(os.environ.get(name) or "").strip()]
    if inherited:
        raise DesktopOperationError(
            "desktop_app_python_environment_unsafe",
            "packaged desktop app must start without inherited PYTHONHOME or PYTHONPATH",
            details={"environment_names": inherited},
        )
    if not sys.flags.no_user_site:
        raise DesktopOperationError(
            "desktop_app_python_user_site_enabled",
            "packaged desktop app must start with -s or PYTHONNOUSERSITE=1",
        )


def _emit_finalizer_ready(operation_id: str, ready_nonce: str) -> None:
    global _FINALIZER_READY_EMITTED
    payload = {"event": "FINALIZER_READY", "operationId": operation_id, "readyNonce": ready_nonce}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROTOCOL_BYTES:
        raise DesktopOperationError("desktop_data_clear_protocol_invalid", "finalizer readiness event exceeded protocol bounds")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()
    _FINALIZER_READY_EMITTED = True

def _emit(response: CommandResponse) -> None:
    payload = response.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROTOCOL_BYTES:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        payload["data"] = {
            "canonical_origin": data.get("canonical_origin"),
            "api_base_url": data.get("api_base_url"),
            "instance_key": data.get("instance_key"),
            "truncated": True,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        sys.stdout.buffer.write(encoded + b"\n")
        sys.stdout.buffer.flush()
    except OSError as exc:
        if not (response.command == "data-clear-finalize" and _FINALIZER_READY_EMITTED and _is_broken_pipe(exc)):
            raise


def _is_broken_pipe(error: OSError) -> bool:
    return isinstance(error, BrokenPipeError) or getattr(error, "winerror", None) in {109, 232, 233} or getattr(error, "errno", None) == 32


def _command_hint(argv: Sequence[str]) -> str:
    known = {"start", "status", "stop", "doctor", "backup", "inventory", "config-show", "config-preview", "config-apply", "credential-inspect", "credential-prompt", "credential-delete", "build", "restore", "data-clear-preview", "data-clear-apply", "data-clear-status", "data-clear-finalize"}
    return next((item for item in reversed(argv) if item in known), "unknown")


__all__ = ["build_parser", "main"]
