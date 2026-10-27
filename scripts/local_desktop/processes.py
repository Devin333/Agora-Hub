from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from framework.shared.local_desktop import validate_lifecycle_timeout, LocalDesktopConfig, utc_timestamp
from framework.shared.redaction import redact_sensitive_values
from scripts.local_desktop.common import DesktopOperationError, OperationResult, atomic_write_json, exclusive_lock, read_json_object
from scripts.local_desktop.config import sanitized_environment
from scripts.local_desktop.control import ApplicationControlPlane
from scripts.local_desktop.doctor import LocalDesktopDoctor
from scripts.local_desktop.release import ReleaseManager

INSTANCE_SCHEMA = "newsroom.local-desktop-instance/v1"
PROCESS_SCHEMA = "newsroom.local-desktop-process/v1"
RUNNING_STATES = {"STARTING", "READY", "DEGRADED"}
# Readiness is polled on loopback, so a short fixed interval costs nothing and
# keeps the gap between "children ready" and "start returns" well under a second.
_READINESS_POLL_INTERVAL = 0.15
_READINESS_PROBE_TIMEOUT = 1.0


@dataclass(frozen=True)
class ProcessIdentity:
    role: str
    pid: int
    created: int
    executable: str
    argv_digest: str
    log_stdout: str
    log_stderr: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROCESS_SCHEMA,
            "role": self.role,
            "pid": self.pid,
            "created": self.created,
            "executable": self.executable,
            "argv_digest": self.argv_digest,
            "log_stdout": self.log_stdout,
            "log_stderr": self.log_stderr,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProcessIdentity":
        if value.get("schema") != PROCESS_SCHEMA:
            raise DesktopOperationError("desktop_process_record_invalid", "process record schema is unsupported")
        try:
            return cls(
                role=str(value["role"]),
                pid=int(value["pid"]),
                created=int(value["created"]),
                executable=str(value["executable"]),
                argv_digest=str(value["argv_digest"]),
                log_stdout=str(value["log_stdout"]),
                log_stderr=str(value["log_stderr"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DesktopOperationError("desktop_process_record_invalid", "process record is incomplete") from exc


class ProcessInspector:
    def identity(self, pid: int) -> tuple[int, str] | None:
        if os.name == "nt":
            return _windows_process_identity(pid)
        return _portable_process_identity(pid)

    def ownership(self, process: ProcessIdentity) -> str:
        actual = self.identity(process.pid)
        if actual is None:
            return "absent"
        created, executable = actual
        if created == process.created and os.path.normcase(executable) == os.path.normcase(process.executable):
            return "owned"
        return "foreign"

    def owns(self, process: ProcessIdentity) -> bool:
        return self.ownership(process) == "owned"

    def terminate(self, process: ProcessIdentity, *, grace_seconds: float) -> None:
        self.terminate_many([process], grace_seconds=grace_seconds)

    def terminate_many(self, processes: list[ProcessIdentity], *, grace_seconds: float) -> None:
        grace_seconds = validate_lifecycle_timeout(grace_seconds, field_name="process_timeout")
        active = self._owned_or_raise(
            processes,
            message="refusing to stop non-owned or reused PIDs",
        )
        if not active:
            return
        deadline = time.monotonic() + grace_seconds
        if os.name == "nt":
            # CREATE_NO_WINDOW children have no safe console-control channel. A CTRL_BREAK
            # event can target a shared console instead of the recorded child, so Windows
            # convergence is performed only through a revalidated process handle.
            for process in active:
                ownership = self._checked_ownership(process)
                if ownership == "foreign":
                    raise DesktopOperationError(
                        "desktop_process_not_owned",
                        f"refusing to terminate a reused Windows PID for {process.role}",
                    )
                if ownership == "owned":
                    _terminate_windows_process(process)
            self._wait_until_not_owned(active, deadline=deadline)
            still_owned = self._owned_or_raise(
                active,
                message="one or more Windows PIDs were reused during process termination",
            )
        else:
            for process in active:
                ownership = self._checked_ownership(process)
                if ownership == "foreign":
                    raise DesktopOperationError(
                        "desktop_process_not_owned",
                        f"refusing to signal a reused POSIX PID for {process.role}",
                    )
                if ownership == "absent":
                    continue
                try:
                    os.kill(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
            graceful_deadline = _graceful_process_deadline(deadline)
            self._wait_until_not_owned(active, deadline=graceful_deadline)
            remaining = self._owned_or_raise(
                active,
                message="one or more POSIX PIDs were reused during graceful termination",
            )
            for process in remaining:
                ownership = self._checked_ownership(process)
                if ownership == "foreign":
                    raise DesktopOperationError(
                        "desktop_process_not_owned",
                        f"refusing to force a reused POSIX PID for {process.role}",
                    )
                if ownership == "absent":
                    continue
                try:
                    os.kill(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    continue
            self._wait_until_not_owned(remaining, deadline=deadline)
            still_owned = self._owned_or_raise(
                remaining,
                message="one or more POSIX PIDs were reused during forced termination",
            )
        if still_owned:
            raise DesktopOperationError(
                "desktop_process_termination_failed",
                "owned processes did not exit before the shared termination deadline",
                details={"roles": [process.role for process in still_owned]},
            )

    def _checked_ownership(self, process: ProcessIdentity) -> str:
        state = str(self.ownership(process))
        if state not in {"absent", "owned", "foreign"}:
            raise DesktopOperationError(
                "desktop_process_inspection_failed",
                "process ownership inspection returned an unsupported state",
                details={"role": process.role, "state": state},
            )
        return state

    def _owned_or_raise(self, processes: list[ProcessIdentity], *, message: str) -> list[ProcessIdentity]:
        states = [(process, self._checked_ownership(process)) for process in processes]
        foreign = [process.role for process, state in states if state == "foreign"]
        if foreign:
            raise DesktopOperationError(
                "desktop_process_not_owned",
                message,
                details={"roles": foreign},
            )
        return [process for process, state in states if state == "owned"]

    def _wait_until_not_owned(self, processes: list[ProcessIdentity], *, deadline: float) -> None:
        while processes:
            states = [(process, self._checked_ownership(process)) for process in processes]
            foreign = [process.role for process, state in states if state == "foreign"]
            if foreign:
                raise DesktopOperationError(
                    "desktop_process_not_owned",
                    "one or more recorded PIDs were reused while waiting for process exit",
                    details={"roles": foreign},
                )
            if all(state == "absent" for _, state in states):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))


class LifecycleManager:
    def __init__(
        self,
        config: LocalDesktopConfig,
        *,
        release_manager: ReleaseManager | None = None,
        doctor: LocalDesktopDoctor | None = None,
        control_plane: ApplicationControlPlane | None = None,
        inspector: ProcessInspector | None = None,
        popen_factory: Callable[..., subprocess.Popen[Any]] | None = None,
        opener: Callable[[str], Any] | None = None,
        api_environment_provider: Callable[[], Mapping[str, str]] | None = None,
        api_environment_names: tuple[str, ...] = (),
    ) -> None:
        self.config = config
        self.release_manager = release_manager or ReleaseManager(config)
        self.doctor = doctor or LocalDesktopDoctor(config)
        self.control_plane = control_plane or ApplicationControlPlane(config)
        self.inspector = inspector or ProcessInspector()
        self.popen_factory = popen_factory or subprocess.Popen
        self.opener = opener or webbrowser.open
        self.api_environment_provider = api_environment_provider
        self.api_environment_names = tuple(dict.fromkeys(api_environment_names))
        if self.api_environment_provider is None and self.api_environment_names:
            raise ValueError("api_environment_names require an api_environment_provider")
        if self.api_environment_provider is not None and not self.api_environment_names:
            raise ValueError("api_environment_provider requires an explicit environment allow-list")
        if any(not name or name != name.strip() or "=" in name or "\x00" in name for name in self.api_environment_names):
            raise ValueError("api environment allow-list contains an invalid name")

    @property
    def instance_path(self) -> Path:
        return self.config.runtime_root / "instance.json"

    @property
    def lock_path(self) -> Path:
        return self.config.runtime_root / "instance.lock"

    @property
    def log_root(self) -> Path:
        return self.config.runtime_root / "logs"

    def start(self, *, open_browser: bool = False, readiness_timeout: float = 60.0) -> OperationResult:
        readiness_timeout = validate_lifecycle_timeout(readiness_timeout, field_name="readiness_timeout")
        with exclusive_lock(self.lock_path):
            current = self._instance()
            recorded = [ProcessIdentity.from_dict(raw) for raw in current.get("processes", [])]
            ownership = {process.role: self._process_ownership(process) for process in recorded}
            owned = [process for process in recorded if ownership[process.role] == "owned"]
            if recorded and len(owned) != len(recorded):
                self._record_state(
                    "FAILED",
                    recorded,
                    release=current.get("release"),
                    reason_code="desktop_process_ownership_incomplete",
                    boot_id=current.get("boot_id"),
                )
                raise DesktopOperationError(
                    "desktop_process_ownership_incomplete",
                    "one or more recorded child processes are absent, foreign, or no longer owned",
                )
            if owned and current.get("status") in RUNNING_STATES:
                expected = self._runtime_identity(current.get("release"), current.get("boot_id"))
                health = self._readiness_once(expected)
                if all(health[item] for item in ("api", "frontend", "bff", "raw_rewrite")):
                    state = "DEGRADED" if current.get("status") == "DEGRADED" or not health["runtime"] else "READY"
                    if open_browser:
                        self.opener(self.config.canonical_origin)
                    return OperationResult(True, "start", state, "desktop_instance_already_running", "owned local desktop instance is already running", self._public_instance(current))
                raise DesktopOperationError("desktop_instance_unhealthy", "owned instance is running but did not pass readiness", details=health)
            if owned:
                self._record_state(
                    "FAILED",
                    recorded,
                    release=current.get("release"),
                    reason_code="desktop_instance_recovery_required",
                    boot_id=current.get("boot_id"),
                )
                raise DesktopOperationError(
                    "desktop_instance_recovery_required",
                    "owned child processes remain from an incomplete lifecycle; run stop before starting again",
                    details={"roles": [process.role for process in owned]},
                )
            conflicts = [endpoint for endpoint in ((self.config.frontend_host, self.config.frontend_port), (self.config.api_host, self.config.api_port)) if _port_is_listening(*endpoint)]
            if conflicts:
                raise DesktopOperationError("desktop_port_owned_elsewhere", "a required desktop port is occupied by an unrelated process", details={"endpoints": [f"{host}:{port}" for host, port in conflicts]})
            release_path, manifest = self.release_manager.active()
            doctor_report = self.doctor.run(verified_release=(release_path, manifest))
            if doctor_report.status == "error":
                raise DesktopOperationError("desktop_doctor_blocked_start", "required local desktop checks failed", details={"doctor": doctor_report.to_dict()})
            self.config.runtime_root.mkdir(parents=True, exist_ok=True)
            self.log_root.mkdir(parents=True, exist_ok=True)
            boot_id = uuid4().hex
            release_identity = manifest.get("identity", {})
            runtime_identity = self._runtime_identity(release_identity, boot_id)
            environment = self.release_manager.bind_runtime_environment(release_path, manifest, sanitized_environment(self.config))
            environment.update({
                "PYTHONPATH": str(release_path / "app"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "NEXT_TELEMETRY_DISABLED": "1",
                "NEWSROOM_DESKTOP_INSTANCE_KEY": runtime_identity["instance_key"],
                "NEWSROOM_DESKTOP_RELEASE_ID": runtime_identity["release_id"],
                "NEWSROOM_DESKTOP_CONFIG_DIGEST": runtime_identity["config_digest"],
                "NEWSROOM_DESKTOP_BOOT_ID": runtime_identity["boot_id"],
            })
            for name in self.api_environment_names:
                environment.pop(name, None)
            children: list[tuple[subprocess.Popen[Any], ProcessIdentity]] = []
            self._record_state("STARTING", [], release=release_identity, reason_code="desktop_starting", boot_id=boot_id)
            try:
                check_startup_cancellation()
                api_command = [str(self.config.python_executable), *manifest["entrypoints"]["api"]]
                children.extend(self._launch("api", api_command, release_path / "app", environment))
                frontend_command = self._frontend_command(release_path, manifest)
                check_startup_cancellation()
                children.extend(self._launch("frontend", frontend_command, release_path / "frontend", environment))
                identities = [identity for _, identity in children]
                self._require_complete_process_roles(identities)
                self._record_state("STARTING", identities, release=release_identity, reason_code="desktop_waiting_for_readiness", boot_id=boot_id)
                readiness = self._wait_for_readiness(children, runtime_identity, deadline=time.monotonic() + readiness_timeout)
                check_startup_cancellation()
                if not all(readiness[item] for item in ("api", "frontend", "bff", "raw_rewrite")):
                    raise DesktopOperationError("desktop_readiness_deadline", "local desktop did not become ready before the deadline", details=readiness)
                status = "DEGRADED" if doctor_report.status == "warning" or not readiness["runtime"] else "READY"
                self.control_plane.resume_admission()
                self._record_state(status, identities, release=release_identity, reason_code="desktop_degraded" if status == "DEGRADED" else "desktop_ready", health=readiness, boot_id=boot_id)
                if open_browser:
                    self.opener(self.config.canonical_origin)
                return OperationResult(True, "start", status, "desktop_started", "local desktop transport is ready", {"origin": self.config.canonical_origin, "release": release_identity, "health": readiness})
            except Exception as exc:
                identities = [identity for _, identity in children]
                try:
                    self._stop_process_topology(identities, process_timeout=3.0)
                except Exception:
                    pass
                remaining = self._remaining_processes(identities)
                reason_code = (
                    "desktop_start_cancelled"
                    if isinstance(exc, DesktopOperationError) and exc.reason_code == "desktop_start_cancelled"
                    else "desktop_start_failed"
                )
                self._record_state("FAILED", remaining, release=release_identity, reason_code=reason_code, boot_id=boot_id)
                raise

    def stop(self, *, drain_timeout: float = 30.0, process_timeout: float = 10.0) -> OperationResult:
        drain_timeout = validate_lifecycle_timeout(drain_timeout, field_name="drain_timeout")
        process_timeout = validate_lifecycle_timeout(process_timeout, field_name="process_timeout")
        with exclusive_lock(self.lock_path):
            current = self._instance()
            processes = [ProcessIdentity.from_dict(raw) for raw in current.get("processes", [])]
            ownership = {process.role: self._process_ownership(process) for process in processes}
            foreign = [process for process in processes if ownership[process.role] == "foreign"]
            if foreign:
                self._record_state(
                    "FAILED",
                    processes,
                    release=current.get("release"),
                    reason_code="desktop_process_ownership_incomplete",
                    boot_id=current.get("boot_id"),
                )
                raise DesktopOperationError(
                    "desktop_process_ownership_incomplete",
                    "one or more recorded PIDs belong to foreign or reused processes; no process was stopped",
                    details={"roles": [process.role for process in foreign]},
                )
            owned = [process for process in processes if ownership[process.role] == "owned"]
            if not owned:
                self._require_stopped_endpoints(current, processes)
                if processes or current.get("status") != "STOPPED":
                    self._record_state(
                        "STOPPED",
                        [],
                        release=current.get("release"),
                        reason_code="desktop_stopped",
                        boot_id=current.get("boot_id"),
                    )
                return OperationResult(True, "stop", "STOPPED", "desktop_already_stopped", "local desktop is already stopped", {})
            self._record_state(
                "STOPPING",
                processes,
                release=current.get("release"),
                reason_code="desktop_draining",
                boot_id=current.get("boot_id"),
            )
            if ownership.get("api") == "owned":
                self.control_plane.begin_drain(timeout_seconds=drain_timeout)
            try:
                self._stop_process_topology(processes, process_timeout=process_timeout)
            except Exception:
                self._record_state(
                    "FAILED",
                    self._remaining_processes(processes),
                    release=current.get("release"),
                    reason_code="desktop_stop_failed",
                    boot_id=current.get("boot_id"),
                )
                raise
            remaining = [(process, self._process_ownership(process)) for process in processes]
            remaining_live = [process for process, state in remaining if state != "absent"]
            if remaining_live:
                remaining_foreign = [process for process, state in remaining if state == "foreign"]
                self._record_state(
                    "FAILED",
                    remaining_live,
                    release=current.get("release"),
                    reason_code="desktop_process_ownership_changed" if remaining_foreign else "desktop_stop_incomplete",
                    boot_id=current.get("boot_id"),
                )
                if remaining_foreign:
                    raise DesktopOperationError(
                        "desktop_process_not_owned",
                        "one or more recorded PIDs were reused while stopping; no signal was sent to the replacement processes",
                        details={"roles": [process.role for process in remaining_foreign]},
                    )
                raise DesktopOperationError(
                    "desktop_process_termination_failed",
                    "one or more owned local desktop processes remained after bounded termination",
                    details={"roles": [process.role for process in remaining_live]},
                )
            self._require_stopped_endpoints(current, processes)
            self._record_state(
                "STOPPED",
                [],
                release=current.get("release"),
                reason_code="desktop_stopped",
                boot_id=current.get("boot_id"),
            )
            return OperationResult(True, "stop", "STOPPED", "desktop_stopped", "owned local desktop processes stopped after drain", {})

    def _stop_process_topology(self, processes: list[ProcessIdentity], *, process_timeout: float) -> None:
        if not processes:
            return
        deadline = time.monotonic() + process_timeout
        services = [process for process in reversed(processes) if not _is_log_collector(process)]
        collectors = [process for process in reversed(processes) if _is_log_collector(process)]
        collector_reserve = _collector_shutdown_reserve(process_timeout) if collectors else 0.0
        service_deadline = deadline - collector_reserve
        self._terminate_process_group(services, deadline=service_deadline)
        collector_eof_deadline = min(service_deadline, time.monotonic() + _collector_eof_wait(process_timeout))
        self._wait_for_process_exit(collectors, deadline=collector_eof_deadline)
        self._terminate_process_group(collectors, deadline=deadline)

    def _terminate_process_group(self, processes: list[ProcessIdentity], *, deadline: float) -> None:
        active: list[ProcessIdentity] = []
        for process in processes:
            ownership = self._process_ownership(process)
            if ownership == "foreign":
                raise DesktopOperationError(
                    "desktop_process_not_owned",
                    f"refusing to stop non-owned or reused PID for {process.role}",
                )
            if ownership == "owned":
                active.append(process)
        if not active:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DesktopOperationError(
                "desktop_process_termination_failed",
                "shared process shutdown deadline expired before owned processes converged",
                details={"roles": [process.role for process in active]},
            )
        terminate_many = getattr(self.inspector, "terminate_many", None)
        if callable(terminate_many):
            terminate_many(active, grace_seconds=remaining)
        else:
            for process in active:
                ownership = self._process_ownership(process)
                if ownership == "absent":
                    continue
                if ownership == "foreign":
                    raise DesktopOperationError(
                        "desktop_process_not_owned",
                        f"refusing to stop non-owned or reused PID for {process.role}",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DesktopOperationError(
                        "desktop_process_termination_failed",
                        "shared process shutdown deadline expired before owned processes converged",
                        details={"roles": [item.role for item in active if self._process_ownership(item) == "owned"]},
                    )
                self.inspector.terminate(process, grace_seconds=remaining)
        states = {process.role: self._process_ownership(process) for process in active}
        foreign = [role for role, state in states.items() if state == "foreign"]
        if foreign:
            raise DesktopOperationError(
                "desktop_process_not_owned",
                "one or more recorded PIDs were reused during process termination",
                details={"roles": foreign},
            )
        still_owned = [role for role, state in states.items() if state == "owned"]
        if still_owned:
            raise DesktopOperationError(
                "desktop_process_termination_failed",
                "owned processes remained after bounded process termination",
                details={"roles": still_owned},
            )

    def _wait_for_process_exit(self, processes: list[ProcessIdentity], *, deadline: float) -> None:
        while processes:
            states = {process.role: self._process_ownership(process) for process in processes}
            foreign = [role for role, state in states.items() if state == "foreign"]
            if foreign:
                raise DesktopOperationError(
                    "desktop_process_not_owned",
                    "one or more recorded PIDs were reused while waiting for process exit",
                    details={"roles": foreign},
                )
            if all(state == "absent" for state in states.values()):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def _remaining_processes(self, processes: list[ProcessIdentity]) -> list[ProcessIdentity]:
        remaining: list[ProcessIdentity] = []
        for process in processes:
            try:
                ownership = self._process_ownership(process)
            except Exception:
                remaining.append(process)
                continue
            if ownership != "absent":
                remaining.append(process)
        return remaining

    def restart(self, *, readiness_timeout: float = 60.0, drain_timeout: float = 30.0) -> OperationResult:
        readiness_timeout = validate_lifecycle_timeout(readiness_timeout, field_name="readiness_timeout")
        drain_timeout = validate_lifecycle_timeout(drain_timeout, field_name="drain_timeout")
        stopped = self.stop(drain_timeout=drain_timeout)
        started = self.start(readiness_timeout=readiness_timeout)
        return OperationResult(True, "restart", started.status, "desktop_restarted", "local desktop restarted", {"stop": stopped.to_dict(), "start": started.to_dict()})

    def status(self) -> OperationResult:
        current = self._instance()
        processes = self._owned_processes(current)
        recorded_count = len(current.get("processes", []))
        if recorded_count and len(processes) != recorded_count:
            return OperationResult(False, "status", "FAILED", "desktop_process_ownership_incomplete", "one or more recorded child processes are absent or no longer owned", self._public_instance(current), exit_code=2)
        if not processes:
            state = "STOPPED" if not current.get("processes") else "FAILED"
            reason = "desktop_stopped" if state == "STOPPED" else "desktop_stale_process_record"
            return OperationResult(state == "STOPPED", "status", state, reason, "local desktop is stopped" if state == "STOPPED" else "process ownership record is stale", self._public_instance(current))
        try:
            self._require_complete_process_roles(processes)
            expected = self._runtime_identity(current.get("release"), current.get("boot_id"))
        except DesktopOperationError as exc:
            return OperationResult(False, "status", "FAILED", exc.reason_code, str(exc), self._public_instance(current), exit_code=2)
        health = self._readiness_once(expected)
        transport_ready = all(health[item] for item in ("api", "frontend", "bff", "raw_rewrite"))
        state = "READY" if transport_ready and health["runtime"] and current.get("status") != "DEGRADED" else "DEGRADED"
        return OperationResult(True, "status", state, "desktop_ready" if state == "READY" else "desktop_degraded", "local desktop health was checked", {**self._public_instance(current), "health": health})
    def open(self) -> OperationResult:
        status = self.status()
        if status.status not in {"READY", "DEGRADED"}:
            raise DesktopOperationError("desktop_not_running", "local desktop is not running; use start before open")
        self.opener(self.config.canonical_origin)
        return OperationResult(True, "open", status.status, "desktop_origin_opened", "opened the canonical local desktop origin", {"origin": self.config.canonical_origin})

    def logs(self, *, role: str | None = None, stream: str = "stderr", lines: int = 200) -> OperationResult:
        if lines < 1 or lines > 5000:
            raise DesktopOperationError("desktop_log_limit_invalid", "log line limit must be between 1 and 5000")
        current = self._instance()
        selected: dict[str, list[str]] = {}
        for raw in current.get("processes", []):
            process = ProcessIdentity.from_dict(raw)
            if ":log:" in process.role:
                continue
            if role and process.role != role:
                continue
            log_path = Path(process.log_stderr if stream == "stderr" else process.log_stdout)
            selected[f"{process.role}.{stream}"] = _tail_redacted(log_path, lines)
        return OperationResult(True, "logs", current.get("status", "STOPPED"), "desktop_logs_read", "bounded redacted operational logs", {"logs": selected})

    def _launch(self, role: str, argv: list[str], cwd: Path, environment: Mapping[str, str]) -> list[tuple[subprocess.Popen[Any], ProcessIdentity]]:
        stdout_path = self.log_root / f"{role}.stdout.log"
        stderr_path = self.log_root / f"{role}.stderr.log"
        _rotate_log(stdout_path)
        _rotate_log(stderr_path)
        common_environment = dict(environment)
        for name in self.api_environment_names:
            common_environment.pop(name, None)
        if role == "frontend":
            common_environment.pop("HOSTNAME", None)
            common_environment.pop("PORT", None)
        collectors: list[tuple[subprocess.Popen[Any], ProcessIdentity]] = []
        try:
            collectors.append(self._launch_log_collector(f"{role}:log:stdout", stdout_path, cwd, common_environment))
            collectors.append(self._launch_log_collector(f"{role}:log:stderr", stderr_path, cwd, common_environment))
        except Exception:
            for collector, _ in collectors:
                if collector.stdin is not None:
                    collector.stdin.close()
                collector.terminate()
            raise
        try:
            child_environment = self._child_environment(role, common_environment)
            if role == "frontend":
                child_environment["HOSTNAME"] = self.config.frontend_host
                child_environment["PORT"] = str(self.config.frontend_port)
        except Exception:
            for collector, _ in collectors:
                if collector.stdin is not None:
                    collector.stdin.close()
                collector.terminate()
            raise
        launch_argv, launch_environment = _direct_windows_venv_launch(argv, child_environment)
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = self.popen_factory(
                launch_argv,
                cwd=cwd,
                env=launch_environment,
                stdin=subprocess.DEVNULL,
                stdout=collectors[0][0].stdin,
                stderr=collectors[1][0].stdin,
                shell=False,
                close_fds=True,
                creationflags=creationflags,
            )
        except Exception:
            for collector, _ in collectors:
                if collector.stdin is not None:
                    collector.stdin.close()
                collector.terminate()
            raise
        for collector, _ in collectors:
            if collector.stdin is not None:
                collector.stdin.close()
        identity_raw = self.inspector.identity(process.pid)
        if identity_raw is None:
            process.terminate()
            for collector, _ in collectors:
                collector.terminate()
            raise DesktopOperationError("desktop_process_identity_unavailable", f"could not establish ownership for {role}")
        created, executable = identity_raw
        identity = ProcessIdentity(role, process.pid, created, executable, _argv_digest(launch_argv), str(stdout_path), str(stderr_path))
        return [*collectors, (process, identity)]


    def _child_environment(self, role: str, environment: Mapping[str, str]) -> dict[str, str]:
        child_environment = dict(environment)
        for name in self.api_environment_names:
            child_environment.pop(name, None)
        if role != "api" or self.api_environment_provider is None:
            return child_environment
        projected = dict(self.api_environment_provider())
        unexpected = sorted(set(projected) - set(self.api_environment_names))
        if unexpected:
            raise DesktopOperationError(
                "desktop_api_environment_projection_invalid",
                "API environment provider returned a name outside its fixed allow-list",
                details={"unexpected_environment_names": unexpected},
            )
        if any(not isinstance(value, str) or not value for value in projected.values()):
            raise DesktopOperationError(
                "desktop_api_environment_projection_invalid",
                "API environment provider returned an invalid value",
            )
        child_environment.update(projected)
        return child_environment

    def _launch_log_collector(
        self,
        role: str,
        log_path: Path,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> tuple[subprocess.Popen[Any], ProcessIdentity]:
        argv = [
            str(self.config.python_executable),
            "-m",
            "scripts.local_desktop.log_collector",
            "--path",
            str(log_path),
        ]
        launch_argv, launch_environment = _direct_windows_venv_launch(argv, environment)
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        process = self.popen_factory(
            launch_argv,
            cwd=cwd,
            env=launch_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
        )
        identity_raw = self.inspector.identity(process.pid)
        if identity_raw is None:
            process.terminate()
            raise DesktopOperationError("desktop_process_identity_unavailable", f"could not establish ownership for {role}")
        created, executable = identity_raw
        identity = ProcessIdentity(role, process.pid, created, executable, _argv_digest(launch_argv), str(log_path), str(log_path))
        return process, identity

    def _frontend_command(self, release_path: Path, manifest: Mapping[str, Any]) -> list[str]:
        entrypoints = manifest.get("entrypoints")
        raw_entrypoint = entrypoints.get("frontend") if isinstance(entrypoints, Mapping) else None
        if not isinstance(raw_entrypoint, list) or any(not isinstance(item, str) for item in raw_entrypoint):
            raise DesktopOperationError("desktop_release_manifest_invalid", "frontend entrypoint is invalid")
        entrypoint = list(raw_entrypoint)
        legacy_entrypoint = [
            "next",
            "start",
            "-H",
            self.config.frontend_host,
            "-p",
            str(self.config.frontend_port),
        ]
        if entrypoint not in (["node", "server.js"], legacy_entrypoint):
            raise DesktopOperationError("desktop_release_manifest_invalid", "frontend entrypoint is invalid")
        node = shutil.which("node")
        if not node:
            raise DesktopOperationError("desktop_frontend_runtime_missing", "controlled Node.js runtime is unavailable on PATH")
        frontend_root = release_path / "frontend"
        if entrypoint == ["node", "server.js"]:
            server = frontend_root / "server.js"
            closure = frontend_root / "production-closure.json"
            if not server.is_file() or not closure.is_file():
                raise DesktopOperationError(
                    "desktop_frontend_runtime_missing",
                    "verified standalone frontend runtime resources are unavailable",
                )
            return [node, str(server)]
        next_cli = frontend_root / "node_modules" / "next" / "dist" / "bin" / "next"
        if not next_cli.is_file():
            raise DesktopOperationError("desktop_frontend_runtime_missing", "release does not contain the prepared Next.js runtime")
        return [node, str(next_cli), *entrypoint[1:]]

    def _wait_for_readiness(self, children: list[tuple[subprocess.Popen[Any], ProcessIdentity]], expected: Mapping[str, str], *, deadline: float) -> dict[str, Any]:
        last = {"api": False, "frontend": False, "bff": False, "raw_rewrite": False, "runtime": False, "reason_code": "desktop_readiness_pending"}
        while time.monotonic() < deadline:
            check_startup_cancellation()
            exited = [identity.role for process, identity in children if process.poll() is not None]
            if exited:
                return {**last, "reason_code": "desktop_child_exited", "exited": exited}
            unowned = [identity.role for _, identity in children if not self.inspector.owns(identity)]
            if unowned:
                return {**last, "reason_code": "desktop_child_ownership_lost", "unowned": unowned}
            last = self._readiness_once(expected)
            if all(last[item] for item in ("api", "frontend", "bff", "raw_rewrite")):
                return last
            time.sleep(max(0.0, min(_READINESS_POLL_INTERVAL, deadline - time.monotonic())))
        return {**last, "reason_code": "desktop_readiness_deadline"}

    def _readiness_once(self, expected: Mapping[str, str]) -> dict[str, Any]:
        # The probes are independent loopback requests; while a child is still
        # booting each one waits out a refused connect, so run them together and
        # let one round cost a single probe's latency instead of five.
        api_base, origin = self.config.api_base_url, self.config.canonical_origin
        timeout = _READINESS_PROBE_TIMEOUT
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="desktop-readiness") as probes:
            api = probes.submit(_json_http, f"{api_base}/health", timeout=timeout)
            runtime = probes.submit(_json_http, f"{api_base}/health/ready", timeout=timeout)
            raw_rewrite = probes.submit(_json_http, f"{origin}/api/v1/auth/session", timeout=timeout)
            bff = probes.submit(_json_http, f"{origin}/api/auth/session", timeout=timeout)
            frontend = probes.submit(_html_ready, origin, timeout=timeout)
        return {
            "api": _api_health_ready(api.result(), expected, require_runtime=False),
            "runtime": _api_health_ready(runtime.result(), expected, require_runtime=True),
            "frontend": frontend.result(),
            "raw_rewrite": _auth_route_ready(raw_rewrite.result(), expected),
            "bff": _auth_route_ready(bff.result(), expected),
            "checked_at": utc_timestamp(),
        }

    def _runtime_identity(self, release: Mapping[str, Any] | None, boot_id: Any) -> dict[str, str]:
        identity = dict(release or {})
        values = {
            "deployment_profile": self.config.deployment_profile,
            "instance_key": self.config.instance_key,
            "release_id": str(identity.get("release_id") or ""),
            "config_digest": str(identity.get("config_digest") or ""),
            "boot_id": str(boot_id or ""),
        }
        if not values["release_id"] or len(values["config_digest"]) != 64 or len(values["boot_id"]) != 32:
            raise DesktopOperationError("desktop_runtime_identity_invalid", "release or boot identity is incomplete")
        return values

    @staticmethod
    def _require_complete_process_roles(processes: list[ProcessIdentity]) -> None:
        required = {"api", "api:log:stdout", "api:log:stderr", "frontend", "frontend:log:stdout", "frontend:log:stderr"}
        roles = [process.role for process in processes]
        if len(roles) != len(set(roles)) or set(roles) != required:
            raise DesktopOperationError("desktop_process_topology_incomplete", "local desktop process ownership topology is incomplete", details={"roles": sorted(set(roles))})
    def _instance(self) -> dict[str, Any]:
        value = read_json_object(self.instance_path, missing={"schema": INSTANCE_SCHEMA, "status": "STOPPED", "processes": []})
        if value.get("schema") != INSTANCE_SCHEMA:
            raise DesktopOperationError("desktop_instance_record_invalid", "instance record schema is unsupported")
        return value

    def _require_stopped_endpoints(self, current: Mapping[str, Any], processes: list[ProcessIdentity]) -> None:
        conflicts = [
            f"{host}:{port}"
            for host, port in (
                (self.config.frontend_host, self.config.frontend_port),
                (self.config.api_host, self.config.api_port),
            )
            if _port_is_listening(host, port)
        ]
        if not conflicts:
            return
        self._record_state(
            "FAILED",
            processes,
            release=current.get("release"),
            reason_code="desktop_stop_transport_active",
            boot_id=current.get("boot_id"),
        )
        raise DesktopOperationError(
            "desktop_process_termination_failed",
            "local desktop transport remained active after recorded processes exited",
            details={"endpoints": conflicts},
        )

    def _process_ownership(self, process: ProcessIdentity) -> str:
        ownership = getattr(self.inspector, "ownership", None)
        if callable(ownership):
            state = str(ownership(process))
            if state not in {"absent", "owned", "foreign"}:
                raise DesktopOperationError(
                    "desktop_process_inspection_failed",
                    "process ownership inspection returned an unsupported state",
                    details={"role": process.role, "state": state},
                )
            return state
        actual = self.inspector.identity(process.pid)
        if actual is None:
            return "absent"
        created, executable = actual
        if created == process.created and os.path.normcase(executable) == os.path.normcase(process.executable):
            return "owned"
        return "foreign"

    def _owned_processes(self, instance: Mapping[str, Any]) -> list[ProcessIdentity]:
        owned: list[ProcessIdentity] = []
        for raw in instance.get("processes", []):
            process = ProcessIdentity.from_dict(raw)
            if self.inspector.owns(process):
                owned.append(process)
        return owned

    def _record_state(self, status: str, processes: list[ProcessIdentity], *, release: Mapping[str, Any] | None = None, reason_code: str, health: Mapping[str, Any] | None = None, boot_id: Any = None) -> None:
        atomic_write_json(self.instance_path, {
            "schema": INSTANCE_SCHEMA,
            "instance_key": self.config.instance_key,
            "status": status,
            "reason_code": reason_code,
            "updated_at": utc_timestamp(),
            "canonical_origin": self.config.canonical_origin,
            "api_base_url": self.config.api_base_url,
            "release": dict(release or {}),
            "boot_id": str(boot_id) if boot_id else None,
            "processes": [process.to_dict() for process in processes],
            "health": dict(health or {}),
        })

    @staticmethod
    def _public_instance(value: Mapping[str, Any]) -> dict[str, Any]:
        return redact_sensitive_values({
            "instance_key": value.get("instance_key"),
            "status": value.get("status", "STOPPED"),
            "reason_code": value.get("reason_code"),
            "canonical_origin": value.get("canonical_origin"),
            "api_base_url": value.get("api_base_url"),
            "release": value.get("release", {}),
            "processes": [{"role": item.get("role"), "pid": item.get("pid"), "created": item.get("created"), "executable": item.get("executable")} for item in value.get("processes", [])],
            "updated_at": value.get("updated_at"),
        })


def check_startup_cancellation() -> None:
    # Import lazily: lifecycle_cancellation depends on desktop_app.protocol, while the
    # desktop_app package imports this module through its runtime composition root.
    from scripts.local_desktop.lifecycle_cancellation import check_startup_cancellation as check

    check()


def _is_log_collector(process: ProcessIdentity) -> bool:
    return ":log:" in process.role


def _collector_shutdown_reserve(process_timeout: float) -> float:
    return min(2.0, process_timeout * 0.25)


def _collector_eof_wait(process_timeout: float) -> float:
    return min(1.0, process_timeout * 0.1)


def _graceful_process_deadline(deadline: float) -> float:
    now = time.monotonic()
    remaining = max(0.0, deadline - now)
    forced_exit_reserve = min(1.0, remaining * 0.2)
    return deadline - forced_exit_reserve


def _argv_digest(argv: list[str]) -> str:
    return sha256(json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _port_is_listening(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def _json_http(url: str, *, timeout: float, max_bytes: int = 64 * 1024) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"User-Agent": "AgoraHub-LocalDesktop/1", "Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > max_bytes:
                return None
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                return None
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                return None
            return {"payload": payload, "content_type": response.headers.get("Content-Type", "")}
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def _runtime_identity_matches(value: Any, expected: Mapping[str, str]) -> bool:
    if not isinstance(value, Mapping):
        return False
    return all(value.get(key) == expected_value for key, expected_value in expected.items())


def _api_health_ready(response: Mapping[str, Any] | None, expected: Mapping[str, str], *, require_runtime: bool) -> bool:
    payload = response.get("payload") if response else None
    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        return False
    data = payload.get("data")
    if not isinstance(data, Mapping) or data.get("status") != "ok" or data.get("service") != "agora-hub-api":
        return False
    if require_runtime and data.get("ready") is not True:
        return False
    return _runtime_identity_matches(data.get("local_desktop"), expected)


def _auth_route_ready(response: Mapping[str, Any] | None, expected: Mapping[str, str]) -> bool:
    payload = response.get("payload") if response else None
    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        return False
    data = payload.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("initialized"), bool):
        return False
    session = data.get("session")
    if session is not None and not isinstance(session, Mapping):
        return False
    return _runtime_identity_matches(data.get("local_desktop"), expected)


def _html_ready(url: str, *, timeout: float, max_bytes: int = 256 * 1024) -> bool:
    request = urllib.request.Request(url, headers={"User-Agent": "AgoraHub-LocalDesktop/1", "Accept": "text/html"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200 or "text/html" not in response.headers.get("Content-Type", "").casefold():
                return False
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > max_bytes:
                return False
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                return False
            html = body.decode("utf-8", errors="strict").casefold()
            return "<html" in html and "</html>" in html
    except (OSError, ValueError, UnicodeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False

def _rotate_log(path: Path, *, max_bytes: int = 10 * 1024 * 1024, retain: int = 5) -> None:
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    for index in range(retain - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        newer = path.with_name(f"{path.name}.{index + 1}")
        if older.exists():
            os.replace(older, newer)
    os.replace(path, path.with_name(f"{path.name}.1"))


def _tail_redacted(path: Path, lines: int) -> list[str]:
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = bytearray()
        while position > 0 and data.count(b"\n") <= lines:
            amount = min(8192, position)
            position -= amount
            handle.seek(position)
            data[:0] = handle.read(amount)
            if len(data) > 2 * 1024 * 1024:
                break
    text = data.decode("utf-8", errors="replace").splitlines()[-lines:]
    return [_redact_log_line(line)[:4096] for line in text]


def _redact_log_line(line: str) -> str:
    redacted = str(redact_sensitive_values(line))
    redacted = re.sub(r"(?i)\b(authorization|cookie|set-cookie|x-api-key|api[_-]?key|token|password)\b\s*[:=]\s*[^,;\s]+", r"\1=***REDACTED***", redacted)
    if len(redacted.encode("utf-8", errors="replace")) > 4096:
        redacted = redacted.encode("utf-8", errors="replace")[:4096].decode("utf-8", errors="ignore")
    return redacted

def _direct_windows_venv_launch(argv: list[str], environment: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
    launch_argv = list(argv)
    launch_environment = dict(environment)
    if os.name != "nt" or not launch_argv:
        return launch_argv, launch_environment
    venv_python = Path(launch_argv[0])
    pyvenv_config = venv_python.parent.parent / "pyvenv.cfg"
    if not pyvenv_config.is_file():
        return launch_argv, launch_environment
    values: dict[str, str] = {}
    try:
        for line in pyvenv_config.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip().lower()] = value.strip()
    except OSError as exc:
        raise DesktopOperationError("desktop_python_runtime_invalid", "could not read the configured Windows virtual environment") from exc
    home = values.get("home")
    if not home:
        raise DesktopOperationError("desktop_python_runtime_invalid", "Windows virtual environment does not declare its base interpreter home")
    base_python = Path(home) / "python.exe"
    if not base_python.is_file():
        raise DesktopOperationError(
            "desktop_python_runtime_invalid",
            "Windows virtual environment base interpreter is unavailable",
            details={"base_python": str(base_python)},
        )
    launch_argv[0] = str(base_python)
    launch_environment["__PYVENV_LAUNCHER__"] = str(venv_python)
    return launch_argv, launch_environment


def _portable_process_identity(pid: int) -> tuple[int, str] | None:
    proc = Path("/proc") / str(pid)
    try:
        executable = str((proc / "exe").resolve(strict=True))
        created = proc.stat().st_ctime_ns
    except (OSError, RuntimeError):
        return None
    return created, executable


class _WindowsFileTime(ctypes.Structure):
    _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_uint32))


_WINDOWS_KERNEL32: Any | None = None
_WINDOWS_PROCESS_NOT_FOUND = {87, 1168}


def _windows_kernel32() -> Any:
    global _WINDOWS_KERNEL32
    if _WINDOWS_KERNEL32 is not None:
        return _WINDOWS_KERNEL32
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsFileTime),
        ctypes.POINTER(_WindowsFileTime),
        ctypes.POINTER(_WindowsFileTime),
        ctypes.POINTER(_WindowsFileTime),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GenerateConsoleCtrlEvent.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    _WINDOWS_KERNEL32 = kernel32
    return kernel32


def _windows_process_identity(pid: int) -> tuple[int, str] | None:
    kernel32 = _windows_kernel32()
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        error = ctypes.get_last_error()
        if error in _WINDOWS_PROCESS_NOT_FOUND:
            return None
        raise DesktopOperationError(
            "desktop_process_inspection_failed",
            "Windows denied access while verifying process ownership",
            details={"winerror": error},
        )
    try:
        if not _windows_process_is_active(kernel32, process):
            return None
        try:
            return _windows_handle_identity(kernel32, process)
        except DesktopOperationError:
            if not _windows_process_is_active(kernel32, process):
                return None
            raise
    finally:
        kernel32.CloseHandle(process)


def _windows_process_is_active(kernel32: Any, process: Any) -> bool:
    exit_code = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
        raise DesktopOperationError(
            "desktop_process_inspection_failed",
            "Windows could not read the process exit status",
            details={"winerror": ctypes.get_last_error()},
        )
    return exit_code.value == 259


def _windows_handle_identity(kernel32: Any, process: Any) -> tuple[int, str]:
    creation = _WindowsFileTime()
    exit_time = _WindowsFileTime()
    kernel_time = _WindowsFileTime()
    user_time = _WindowsFileTime()
    if not kernel32.GetProcessTimes(process, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
        raise DesktopOperationError(
            "desktop_process_inspection_failed",
            "Windows could not read the process start time",
            details={"winerror": ctypes.get_last_error()},
        )
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
        raise DesktopOperationError(
            "desktop_process_inspection_failed",
            "Windows could not read the process executable identity",
            details={"winerror": ctypes.get_last_error()},
        )
    created = (int(creation.high) << 32) | int(creation.low)
    return created, str(Path(buffer.value).resolve(strict=False))


def _send_windows_break(identity: ProcessIdentity) -> bool:
    """Send CTRL_BREAK to the owned process group without CPython os.kill fallback."""
    if not ProcessInspector().owns(identity):
        return False
    kernel32 = _windows_kernel32()
    return bool(kernel32.GenerateConsoleCtrlEvent(1, identity.pid))

def _terminate_windows_process(identity: ProcessIdentity) -> None:
    kernel32 = _windows_kernel32()
    process = kernel32.OpenProcess(0x1001, False, identity.pid)
    if not process:
        error = ctypes.get_last_error()
        if error in _WINDOWS_PROCESS_NOT_FOUND:
            return
        raise DesktopOperationError(
            "desktop_process_termination_failed",
            "Windows denied access while terminating an owned process",
            details={"winerror": error},
        )
    try:
        if not _windows_process_is_active(kernel32, process):
            return
        try:
            created, executable = _windows_handle_identity(kernel32, process)
        except DesktopOperationError:
            if not _windows_process_is_active(kernel32, process):
                return
            raise
        if created != identity.created or os.path.normcase(executable) != os.path.normcase(identity.executable):
            raise DesktopOperationError("desktop_process_not_owned", "refusing to terminate a reused or unowned Windows process handle")
        if not kernel32.TerminateProcess(process, 1):
            raise DesktopOperationError(
                "desktop_process_termination_failed",
                "Windows failed to terminate an owned process",
                details={"winerror": ctypes.get_last_error()},
            )
    finally:
        kernel32.CloseHandle(process)


__all__ = ["INSTANCE_SCHEMA", "LifecycleManager", "ProcessIdentity", "ProcessInspector"]
