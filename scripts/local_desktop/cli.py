from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from framework.shared.local_desktop import LocalDesktopConfigError, ProbeBudget, validate_lifecycle_timeout
from framework.shared.redaction import redact_sensitive_values
from scripts.local_desktop.autostart import AutostartManager
from scripts.local_desktop.common import DesktopOperationError, OperationResult
from scripts.local_desktop.config import load_config, sanitized_environment
from scripts.local_desktop.desktop_entry import DesktopEntryManager
from scripts.local_desktop.doctor import LocalDesktopDoctor
from scripts.local_desktop.processes import LifecycleManager
from scripts.local_desktop.recovery import BackupManager, RestoreManager, StorageInventoryResolver
from scripts.local_desktop.release import ReleaseManager


_CONFIG_ARGUMENTS = (
    (("--profile", "--deployment-profile"), "deployment_profile", {"help": "部署 profile，必须为 local-desktop"}),
    (("--frontend-host",), "frontend_host", {"help": "前端监听地址，只允许 127.0.0.1"}),
    (("--frontend-port",), "frontend_port", {"type": int, "help": "前端监听端口"}),
    (("--api-host",), "api_host", {"help": "API 监听地址，只允许 127.0.0.1"}),
    (("--api-port",), "api_port", {"type": int, "help": "API 监听端口"}),
    (("--canonical-origin",), "canonical_origin", {"help": "与前端监听端点一致的 canonical origin"}),
    (("--api-base-url",), "api_base_url", {"help": "与 API 监听端点一致的 base URL"}),
    (("--runtime-root",), "runtime_root", {"help": "本实例运行元数据目录"}),
    (("--release-root",), "release_root", {"help": "不可变 production release 目录"}),
    (("--backup-root",), "backup_root", {"help": "默认备份目录"}),
    (("--control-path",), "control_path", {"help": "application admission control metadata 文件"}),
    (("--parser-mode",), "parser_mode", {"choices": ("pymupdf", "marker", "mineru"), "help": "选定的 PDF parser"}),
    (("--data-root", "--research-root"), "research_root", {"help": "Research durable data 根目录"}),
    (("--artifact-root",), "artifact_root", {"help": "Research/Harness artifact 根目录"}),
)


def _add_config_arguments(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    for flags, destination, options in _CONFIG_ARGUMENTS:
        kwargs = dict(options)
        kwargs["dest"] = destination
        if suppress_defaults:
            kwargs["default"] = argparse.SUPPRESS
        parser.add_argument(*flags, **kwargs)


def _config_cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
        destination: getattr(args, destination)
        for _, destination, _ in _CONFIG_ARGUMENTS
        if hasattr(args, destination) and getattr(args, destination) is not None
    }


def _lifecycle_timeout(field_name: str):
    def parse(value: str) -> float:
        return validate_lifecycle_timeout(value, field_name=field_name)

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.local_desktop", description="Agora Hub Windows 本机 Research 运行、诊断和恢复工具")
    parser.add_argument("--json", action="store_true", dest="json_output", help="输出稳定的 JSON 结果")
    _add_config_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="离线检查配置、依赖、端口、存储和能力")
    doctor.add_argument("--probe-network", action="store_true", help="显式执行有界真实 source/provider 可达性探测")
    doctor.add_argument("--timeout", type=float, default=10.0)
    doctor.add_argument("--max-requests", type=int, default=3)
    doctor.add_argument("--max-input-tokens", type=int, default=256)
    doctor.add_argument("--max-output-tokens", type=int, default=64)
    doctor.add_argument("--max-cost-usd", type=float, default=0.02)

    subparsers.add_parser("build", help="显式准备并原子激活 production release")
    start = subparsers.add_parser("start", help="启动已准备且校验通过的 production release")
    start.add_argument("--open", action="store_true", dest="open_browser")
    start.add_argument("--readiness-timeout", type=_lifecycle_timeout("readiness_timeout"), default=60.0)
    stop = subparsers.add_parser("stop", help="停止 admission，等待业务收敛后停止自有进程")
    stop.add_argument("--drain-timeout", type=_lifecycle_timeout("drain_timeout"), default=30.0)
    stop.add_argument("--process-timeout", type=_lifecycle_timeout("process_timeout"), default=10.0)
    restart = subparsers.add_parser("restart", help="受控停止并重新启动")
    restart.add_argument("--drain-timeout", type=_lifecycle_timeout("drain_timeout"), default=30.0)
    restart.add_argument("--readiness-timeout", type=_lifecycle_timeout("readiness_timeout"), default=60.0)
    subparsers.add_parser("status", help="显示实例、release、进程和健康状态")
    subparsers.add_parser("open", help="打开正在运行的 canonical origin")
    logs = subparsers.add_parser("logs", help="有界读取脱敏后的轮换日志")
    logs.add_argument("--role", choices=("api", "frontend"))
    logs.add_argument("--stream", choices=("stdout", "stderr"), default="stderr")
    logs.add_argument("--lines", type=int, default=200)
    backup = subparsers.add_parser("backup", help="为已停止实例创建一致性、有校验清单的备份")
    backup.add_argument("--output")
    subparsers.add_parser("inventory", help="显示脱敏 storage owner 清单")
    restore = subparsers.add_parser("restore", help="在隔离 root 校验备份；显式 --activate 后切换")
    restore.add_argument("backup")
    restore.add_argument("--target-root")
    restore.add_argument("--activate", action="store_true")
    rollback = subparsers.add_parser("rollback", help="切换到已验证且数据兼容的 production release")
    rollback.add_argument("release")
    rollback.add_argument("--compatible-backup")
    desktop_entry = subparsers.add_parser("desktop-entry", help="管理当前用户桌面入口")
    desktop_entry.add_argument("action", choices=("status", "install", "remove"))
    autostart = subparsers.add_parser("autostart", help="管理当前用户登录启动；默认关闭")
    autostart.add_argument("action", choices=("status", "enable", "disable"))
    for command_parser in subparsers.choices.values():
        command_parser.add_argument("--json", action="store_true", dest="json_output", default=argparse.SUPPRESS, help="输出稳定的 JSON 结果")
        _add_config_arguments(command_parser, suppress_defaults=True)
    return parser



def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(cli_overrides=_config_cli_overrides(args))
        result = dispatch(args, config)
        _emit(result.to_dict() if isinstance(result, OperationResult) else result, json_output=args.json_output)
        return result.exit_code if isinstance(result, OperationResult) else (0 if result.get("status") != "error" else 2)
    except LocalDesktopConfigError as exc:
        payload = {"ok": False, **exc.to_dict()}
        _emit(payload, json_output=getattr(args, "json_output", False), error=True)
        return 2
    except DesktopOperationError as exc:
        _emit(exc.to_dict(), json_output=getattr(args, "json_output", False), error=True)
        return exc.exit_code
    except ValueError as exc:
        _emit({"ok": False, "reason_code": "desktop_argument_invalid", "message": str(exc)}, json_output=getattr(args, "json_output", False), error=True)
        return 2
    except KeyboardInterrupt:
        _emit({"ok": False, "reason_code": "desktop_interrupted", "message": "操作已中断"}, json_output=getattr(args, "json_output", False), error=True)
        return 130


def dispatch(args: argparse.Namespace, config) -> OperationResult | dict[str, Any]:
    environment = sanitized_environment(config)
    lifecycle = LifecycleManager(config, doctor=LocalDesktopDoctor(config, environ=environment))
    if args.command == "doctor":
        budget = ProbeBudget(args.timeout, args.max_requests, args.max_input_tokens, args.max_output_tokens, args.max_cost_usd)
        return lifecycle.doctor.run(probe_network=args.probe_network, budget=budget).to_dict()
    if args.command == "build":
        manifest = lifecycle.release_manager.build()
        return OperationResult(True, "build", "ACTIVE", "desktop_release_built", "production release built, verified, and activated", {"identity": manifest["identity"]})
    if args.command == "start":
        return lifecycle.start(open_browser=args.open_browser, readiness_timeout=args.readiness_timeout)
    if args.command == "stop":
        return lifecycle.stop(drain_timeout=args.drain_timeout, process_timeout=args.process_timeout)
    if args.command == "restart":
        return lifecycle.restart(readiness_timeout=args.readiness_timeout, drain_timeout=args.drain_timeout)
    if args.command == "status":
        return lifecycle.status()
    if args.command == "open":
        return lifecycle.open()
    if args.command == "logs":
        return lifecycle.logs(role=args.role, stream=args.stream, lines=args.lines)
    if args.command == "inventory":
        inventory = StorageInventoryResolver(config, environ=environment).resolve(require_existing=False)
        return {"ok": True, "status": "ok", "schema": inventory.schema, "owners": [{**item.to_dict(), "source_path": str(item.path)} for item in inventory.owners]}
    if args.command == "backup":
        return BackupManager(config, inventory_resolver=StorageInventoryResolver(config, environ=environment)).create(destination=args.output)
    if args.command == "restore":
        return RestoreManager(config, inventory_resolver=StorageInventoryResolver(config, environ=environment)).restore(args.backup, target_root=args.target_root, activate=args.activate)
    if args.command == "rollback":
        return RestoreManager(config, inventory_resolver=StorageInventoryResolver(config, environ=environment)).rollback_release(args.release, compatible_backup=args.compatible_backup)
    if args.command == "desktop-entry":
        manager = DesktopEntryManager(config)
        return getattr(manager, args.action)()
    if args.command == "autostart":
        manager = AutostartManager(config)
        return getattr(manager, args.action)()
    raise DesktopOperationError("desktop_command_unknown", "unknown local desktop command")


def _emit(payload: dict[str, Any], *, json_output: bool, error: bool = False) -> None:
    safe = redact_sensitive_values(payload)
    if json_output:
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        return
    stream = sys.stderr if error else sys.stdout
    print(str(safe.get("message") or safe.get("status") or safe.get("reason_code") or "完成"), file=stream)
    print(f"reason={safe.get('reason_code', 'ok')}", file=stream)
    details = safe.get("details")
    if details:
        print(json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


__all__ = ["build_parser", "dispatch", "main"]
