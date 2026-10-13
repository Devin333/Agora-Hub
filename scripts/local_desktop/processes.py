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
RUNNING_STATES = {"STARTING", "READY", "DEGRADED", "STOPPING"}


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

    def owns(self, process: ProcessIdentity) -> bool:
        actual = self.identity(process.pid)
        if actual is None:
            return False
        created, executable = actual
        return created == process.created and os.path.normcase(executable) == os.path.normcase(process.executable)

    def terminate(self, process: ProcessIdentity, *, grace_seconds: float) -> None:
        grace_seconds = validate_lifecycle_timeout(grace_seconds, field_name="process_timeout")
        if not self.owns(process):
            raise DesktopOperationError("desktop_process_not_owned", f"refusing to stop non-owned or reused PID for {process.role}")
        if os.name == "nt":
            _send_windows_break(process)
        else:
            try:
                os.kill(process.pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if self.identity(process.pid) is None:
                return
            time.sleep(0.1)
        if not self.owns(process):
            return
        if os.name == "nt":
            _terminate_windows_process(process)
        else:
            os.kill(process.pid, signal.SIGKILL)


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
    ) -> None:
        self.config = config
        self.release_manager = release_manager or ReleaseManager(config)
        self.doctor = doctor or LocalDesktopDoctor(config)
        self.control_plane = control_plane or ApplicationControlPlane(config)
        self.inspector = inspector or ProcessInspector()
        self.popen_factory = popen_factory or subprocess.Popen
        self.opener = opener or webbrowser.open

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
            owned = self._owned_processes(current)
            recorded_count = len(current.get("processes", []))
            if recorded_count and len(owned) != recorded_count:
                self._record_state("FAILED", owned, release=current.get("release"), reason_code="desktop_process_ownership_incomplete", boot_id=current.get("boot_id"))
                raise DesktopOperationError("desktop_process_ownership_incomplete", "one or more recorded child processes are absent or no longer owned")
            if owned and current.get("status") in RUNNING_STATES:
                expected = self._runtime_identity(current.get("release"), current.get("boot_id"))
                health = self._readiness_once(expected)
                if all(health[item] for item in ("api", "frontend", "bff", "raw_rewrite")):
                    state = "DEGRADED" if current.get("status") == "DEGRADED" or not health["runtime"] else "READY"
                    if open_browser:
                        self.opener(self.config.canonical_origin)
                    return OperationResult(True, "start", state, "desktop_instance_already_running", "owned local desktop instance is already running", self._public_instance(current))
                raise DesktopOperationError("desktop_instance_unhealthy", "owned instance is running but did not pass readiness", details=health)
            if current.get("processes") and not owned:
                self._record_state("FAILED", [], reason_code="desktop_stale_process_record")
            conflicts = [endpoint for endpoint in ((self.config.frontend_host, self.config.frontend_port), (self.config.api_host, self.config.api_port)) if _port_is_listening(*endpoint)]
            if conflicts:
                raise DesktopOperationError("desktop_port_owned_elsewhere", "a required desktop port is occupied by an unrelated process", details={"endpoints": [f"{host}:{port}" for host, port in conflicts]})
            release_path, manifest = self.release_manager.active()
            doctor_report = self.doctor.run()
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
                "NEXT_TELEMETRY_DISABLED": "1",
                "NEWSROOM_DESKTOP_INSTANCE_KEY": runtime_identity["instance_key"],
                "NEWSROOM_DESKTOP_RELEASE_ID": runtime_identity["release_id"],
                "NEWSROOM_DESKTOP_CONFIG_DIGEST": runtime_identity["config_digest"],
                "NEWSROOM_DESKTOP_BOOT_ID": runtime_identity["boot_id"],
            })
            children: list[tuple[subprocess.Popen[Any], ProcessIdentity]] = []
            self._record_state("STARTING", [], release=release_identity, reason_code="desktop_starting", boot_id=boot_id)
            try:
                api_command = [str(self.config.python_executable), *manifest["entrypoints"]["api"]]
                children.extend(self._launch("api", api_command, release_path / "app", environment))
                frontend_command = self._frontend_command(release_path, manifest)
                children.extend(self._launch("frontend", frontend_command, release_path / "frontend", environment))
                identities = [identity for _, identity in children]
                self._require_complete_process_roles(identities)
                self._record_state("STARTING", identities, release=release_identity, reason_code="desktop_waiting_for_readiness", boot_id=boot_id)
                readiness = self._wait_for_readiness(children, runtime_identity, deadline=time.monotonic() + readiness_timeout)
                if not all(readiness[item] for item in ("api", "frontend", "bff", "raw_rewrite")):
                    raise DesktopOperationError("desktop_readiness_deadline", "local desktop did not become ready before the deadline", details=readiness)
                status = "DEGRADED" if doctor_report.status == "warning" or not readiness["runtime"] else "READY"
                self.control_plane.resume_admission()
                self._record_state(status, identities, release=release_identity, reason_code="desktop_degraded" if status == "DEGRADED" else "desktop_ready", health=readiness, boot_id=boot_id)
                if open_browser:
                    self.opener(self.config.canonical_origin)
                return OperationResult(True, "start", status, "desktop_started", "local desktop transport is ready", {"origin": self.config.canonical_origin, "release": release_identity, "health": readiness})
            except Exception:
                for _, identity in reversed(children):
                    try:
                        self.inspector.terminate(identity, grace_seconds=3)
                    except DesktopOperationError:
                        pass
                self._record_state("FAILED", [], release=release_identity, reason_code="desktop_start_failed", boot_id=boot_id)
                raise
    def stop(self, *, drain_timeout: float = 30.0, process_timeout: float = 10.0) -> OperationResult:
        drain_timeout = validate_lifecycle_timeout(drain_timeout, field_name="drain_timeout")
        process_timeout = validate_lifecycle_timeout(process_timeout, field_name="process_timeout")
        with exclusive_lock(self.lock_path):
            current = self._instance()
            processes = self._owned_processes(current)
            recorded_count = len(current.get("processes", []))
            if recorded_count and len(processes) != recorded_count:
                self._record_state("FAILED", processes, release=current.get("release"), reason_code="desktop_process_ownership_incomplete")
                raise DesktopOperationError("desktop_process_ownership_incomplete", "one or more recorded child processes are absent or no longer owned; no process was stopped")
            if not processes:
                if current.get("processes"):
                    self._record_state("FAILED", [], reason_code="desktop_stale_process_record")
                    raise DesktopOperationError("desktop_process_not_owned", "recorded processes are absent or no longer owned; no process was stopped")
                return OperationResult(True, "stop", "STOPPED", "desktop_already_stopped", "local desktop is already stopped", {})
            self._record_state("STOPPING", processes, release=current.get("release"), reason_code="desktop_draining")
            self.control_plane.begin_drain(timeout_seconds=drain_timeout)
            for process in reversed(processes):
                self.inspector.terminate(process, grace_seconds=process_timeout)
            self._record_state("STOPPED", [], release=current.get("release"), reason_code="desktop_stopped")
            return OperationResult(True, "stop", "STOPPED", "desktop_stopped", "owned local desktop processes stopped after drain", {})

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
        collectors: list[tuple[subprocess.Popen[Any], ProcessIdentity]] = []
        try:
            collectors.append(self._launch_log_collector(f"{role}:log:stdout", stdout_path, cwd, environment))
            collectors.append(self._launch_log_collector(f"{role}:log:stderr", stderr_path, cwd, environment))
        except Exception:
            for collector, _ in collectors:
                if collector.stdin is not None:
                    collector.stdin.close()
                collector.terminate()
            raise
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = self.popen_factory(
                argv,
                cwd=cwd,
                env=dict(environment),
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
        identity = ProcessIdentity(role, process.pid, created, executable, _argv_digest(argv), str(stdout_path), str(stderr_path))
        return [*collectors, (process, identity)]

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
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        process = self.popen_factory(
            argv,
            cwd=cwd,
            env=dict(environment),
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
        identity = ProcessIdentity(role, process.pid, created, executable, _argv_digest(argv), str(log_path), str(log_path))
        return process, identity

    def _frontend_command(self, release_path: Path, manifest: Mapping[str, Any]) -> list[str]:
        node = shutil.which("node")
        next_cli = release_path / "frontend" / "node_modules" / "next" / "dist" / "bin" / "next"
        if not node or not next_cli.is_file():
            raise DesktopOperationError("desktop_frontend_runtime_missing", "release does not contain the prepared Next.js runtime")
        entrypoint = list(manifest["entrypoints"]["frontend"])
        if not entrypoint or entrypoint[0] != "next":
            raise DesktopOperationError("desktop_release_manifest_invalid", "frontend entrypoint is invalid")
        return [node, str(next_cli), *entrypoint[1:]]

    def _wait_for_readiness(self, children: list[tuple[subprocess.Popen[Any], ProcessIdentity]], expected: Mapping[str, str], *, deadline: float) -> dict[str, Any]:
        last = {"api": False, "frontend": False, "bff": False, "raw_rewrite": False, "runtime": False, "reason_code": "desktop_readiness_pending"}
        delay = 0.1
        while time.monotonic() < deadline:
            exited = [identity.role for process, identity in children if process.poll() is not None]
            if exited:
                return {**last, "reason_code": "desktop_child_exited", "exited": exited}
            unowned = [identity.role for _, identity in children if not self.inspector.owns(identity)]
            if unowned:
                return {**last, "reason_code": "desktop_child_ownership_lost", "unowned": unowned}
            last = self._readiness_once(expected)
            if all(last[item] for item in ("api", "frontend", "bff", "raw_rewrite")):
                return last
            time.sleep(delay)
            delay = min(delay * 1.5, 1.0)
        return {**last, "reason_code": "desktop_readiness_deadline"}

    def _readiness_once(self, expected: Mapping[str, str]) -> dict[str, Any]:
        api = _json_http(f"{self.config.api_base_url}/health", timeout=1.0)
        runtime = _json_http(f"{self.config.api_base_url}/health/ready", timeout=1.0)
        raw_rewrite = _json_http(f"{self.config.canonical_origin}/api/v1/auth/session", timeout=1.0)
        bff = _json_http(f"{self.config.canonical_origin}/api/auth/session", timeout=1.0)
        return {
            "api": _api_health_ready(api, expected, require_runtime=False),
            "runtime": _api_health_ready(runtime, expected, require_runtime=True),
            "frontend": _html_ready(self.config.canonical_origin, timeout=1.0),
            "raw_rewrite": _auth_route_ready(raw_rewrite, expected),
            "bff": _auth_route_ready(bff, expected),
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
        return _windows_handle_identity(kernel32, process)
    finally:
        kernel32.CloseHandle(process)


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
        created, executable = _windows_handle_identity(kernel32, process)
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
