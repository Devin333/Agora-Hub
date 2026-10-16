from __future__ import annotations

import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from framework.shared.local_desktop import resolve_local_desktop_config
from scripts.local_desktop.common import DesktopOperationError
from scripts.local_desktop.doctor import DoctorReport
from scripts.local_desktop.processes import (
    INSTANCE_SCHEMA,
    LifecycleManager,
    ProcessIdentity,
    ProcessInspector,
    _api_health_ready,
    _auth_route_ready,
    _direct_windows_venv_launch,
    _windows_kernel32,
    _windows_process_identity,
)


@pytest.fixture(autouse=True)
def _isolate_desktop_ports(monkeypatch):
    monkeypatch.setattr("scripts.local_desktop.processes._port_is_listening", lambda _host, _port: False)


class FakeInspector:
    def __init__(self, owned: set[int]) -> None:
        self.owned = owned
        self.terminated: list[int] = []

    def owns(self, process: ProcessIdentity) -> bool:
        return process.pid in self.owned

    def identity(self, pid: int):
        return (100, "C:/owned.exe") if pid in self.owned else None

    def terminate(self, process: ProcessIdentity, *, grace_seconds: float) -> None:
        if process.pid not in self.owned:
            raise DesktopOperationError("desktop_process_not_owned", "not owned")
        self.terminated.append(process.pid)
        self.owned.remove(process.pid)


class FakeControl:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.drained = False

    def begin_drain(self, *, timeout_seconds: float):
        if self.busy:
            raise DesktopOperationError("desktop_drain_busy", "busy", exit_code=4)
        self.drained = True
        return {"status": "drained", "active_count": 0}

    def resume_admission(self):
        return {"status": "accepting"}


class FakeDoctor:
    def run(self):
        return DoctorReport("ok", (), ())


def _manager(tmp_path: Path, *, owned: set[int], busy: bool = False) -> tuple[LifecycleManager, FakeInspector]:
    config = resolve_local_desktop_config(project_root=tmp_path)
    inspector = FakeInspector(owned)
    manager = LifecycleManager(
        config,
        release_manager=SimpleNamespace(),
        doctor=FakeDoctor(),
        control_plane=FakeControl(busy=busy),
        inspector=inspector,
    )
    config.runtime_root.mkdir(parents=True)
    return manager, inspector


def _process(pid: int) -> ProcessIdentity:
    return ProcessIdentity("api", pid, 100, "C:/owned.exe", "digest", "out.log", "err.log")


def test_stop_drains_before_terminating_owned_process(tmp_path: Path) -> None:
    manager, inspector = _manager(tmp_path, owned={42})
    manager._record_state("READY", [_process(42)], release={}, reason_code="desktop_ready")

    result = manager.stop()

    assert result.status == "STOPPED"
    assert inspector.terminated == [42]


def test_stop_busy_preserves_owned_process_and_business_record(tmp_path: Path) -> None:
    manager, inspector = _manager(tmp_path, owned={42}, busy=True)
    manager._record_state("READY", [_process(42)], release={}, reason_code="desktop_ready", boot_id="b" * 32)

    with pytest.raises(DesktopOperationError) as raised:
        manager.stop()

    assert raised.value.reason_code == "desktop_drain_busy"
    assert inspector.terminated == []
    instance = manager._instance()
    assert instance["status"] == "STOPPING"
    assert instance["boot_id"] == "b" * 32


def test_absent_process_record_converges_to_stopped_without_signalling(tmp_path: Path) -> None:
    manager, inspector = _manager(tmp_path, owned=set())
    manager._record_state("STOPPING", [_process(42)], release={}, reason_code="desktop_stop_failed", boot_id="b" * 32)

    result = manager.stop()

    assert result.status == "STOPPED"
    assert inspector.terminated == []
    instance = manager._instance()
    assert instance["processes"] == []
    assert instance["boot_id"] == "b" * 32


def test_absent_records_do_not_mask_an_untracked_listening_child(tmp_path: Path, monkeypatch) -> None:
    manager, inspector = _manager(tmp_path, owned=set())
    manager._record_state("STOPPING", [_process(42)], release={}, reason_code="desktop_stop_failed", boot_id="b" * 32)
    monkeypatch.setattr(
        "scripts.local_desktop.processes._port_is_listening",
        lambda _host, port: port == manager.config.api_port,
    )

    with pytest.raises(DesktopOperationError) as raised:
        manager.stop()

    assert raised.value.reason_code == "desktop_process_termination_failed"
    assert raised.value.details == {"endpoints": [f"{manager.config.api_host}:{manager.config.api_port}"]}
    assert inspector.terminated == []
    instance = manager._instance()
    assert instance["status"] == "FAILED"
    assert instance["reason_code"] == "desktop_stop_transport_active"
    assert instance["boot_id"] == "b" * 32


def test_stale_pid_record_never_stops_reused_process(tmp_path: Path) -> None:
    manager, inspector = _manager(tmp_path, owned=set())
    manager._record_state("READY", [_process(42)], release={}, reason_code="desktop_ready", boot_id="b" * 32)
    inspector.identity = lambda pid: (101, "C:/foreign.exe") if pid == 42 else None

    with pytest.raises(DesktopOperationError) as raised:
        manager.stop()

    assert raised.value.reason_code == "desktop_process_ownership_incomplete"
    assert inspector.terminated == []
    instance = manager._instance()
    assert instance["processes"][0]["pid"] == 42
    assert instance["boot_id"] == "b" * 32


def test_unknown_ownership_state_is_not_treated_as_stopped(tmp_path: Path) -> None:
    class UnknownInspector(FakeInspector):
        def ownership(self, process: ProcessIdentity) -> str:
            return "unknown"

    config = resolve_local_desktop_config(project_root=tmp_path)
    config.runtime_root.mkdir(parents=True)
    inspector = UnknownInspector({42})
    manager = LifecycleManager(
        config,
        release_manager=SimpleNamespace(),
        doctor=FakeDoctor(),
        control_plane=FakeControl(),
        inspector=inspector,
    )
    manager._record_state("READY", [_process(42)], release={}, reason_code="desktop_ready")

    with pytest.raises(DesktopOperationError) as raised:
        manager.stop()

    assert raised.value.reason_code == "desktop_process_inspection_failed"
    assert inspector.terminated == []
    instance = manager._instance()
    assert instance["status"] == "READY"
    assert instance["processes"][0]["pid"] == 42


def test_stop_tolerates_collectors_that_exit_after_service_closes_their_pipes(tmp_path: Path) -> None:
    collectors_by_service = {"frontend": {44, 45}, "api": {41, 42}}

    class CollectorRaceInspector(FakeInspector):
        def terminate(self, process: ProcessIdentity, *, grace_seconds: float) -> None:
            if process.pid not in self.owned:
                raise AssertionError(f"absent process was sent to terminate: {process.role}")
            self.terminated.append(process.pid)
            self.owned.remove(process.pid)
            self.owned.difference_update(collectors_by_service.get(process.role, set()))

    config = resolve_local_desktop_config(project_root=tmp_path)
    config.runtime_root.mkdir(parents=True)
    inspector = CollectorRaceInspector(set(range(41, 47)))
    manager = LifecycleManager(
        config,
        release_manager=SimpleNamespace(),
        doctor=FakeDoctor(),
        control_plane=FakeControl(),
        inspector=inspector,
    )
    roles = ("api:log:stdout", "api:log:stderr", "api", "frontend:log:stdout", "frontend:log:stderr", "frontend")
    processes = [
        ProcessIdentity(role, pid, 100, "C:/owned.exe", "digest", "out.log", "err.log")
        for role, pid in zip(roles, range(41, 47), strict=True)
    ]
    manager._record_state("READY", processes, release={}, reason_code="desktop_ready", boot_id="b" * 32)

    result = manager.stop()

    assert result.status == "STOPPED"
    assert inspector.terminated == [46, 43]
    assert manager._instance()["boot_id"] == "b" * 32


def test_stop_batches_services_before_collectors_and_allows_eof_flush(tmp_path: Path) -> None:
    collector_pids = {"frontend": {44, 45}, "api": {41, 42}}
    control = FakeControl()

    class CoordinatedInspector(FakeInspector):
        def __init__(self, owned: set[int]) -> None:
            super().__init__(owned)
            self.batches: list[list[str]] = []

        def terminate_many(self, processes: list[ProcessIdentity], *, grace_seconds: float) -> None:
            assert grace_seconds > 0
            assert control.drained is True
            self.batches.append([process.role for process in processes])
            for process in processes:
                self.owned.remove(process.pid)
                self.owned.difference_update(collector_pids.get(process.role, set()))

    config = resolve_local_desktop_config(project_root=tmp_path)
    config.runtime_root.mkdir(parents=True)
    inspector = CoordinatedInspector(set(range(41, 47)))
    manager = LifecycleManager(
        config,
        release_manager=SimpleNamespace(),
        doctor=FakeDoctor(),
        control_plane=control,
        inspector=inspector,
    )
    roles = ("api:log:stdout", "api:log:stderr", "api", "frontend:log:stdout", "frontend:log:stderr", "frontend")
    processes = [
        ProcessIdentity(role, pid, 100, "C:/owned.exe", "digest", "out.log", "err.log")
        for role, pid in zip(roles, range(41, 47), strict=True)
    ]
    manager._record_state("READY", processes, release={}, reason_code="desktop_ready")

    result = manager.stop(process_timeout=4.0)

    assert result.status == "STOPPED"
    assert inspector.batches == [["frontend", "api"]]


def test_stop_forces_only_collectors_that_remain_after_bounded_eof_wait(tmp_path: Path) -> None:
    class CoordinatedInspector(FakeInspector):
        def __init__(self, owned: set[int]) -> None:
            super().__init__(owned)
            self.batches: list[list[str]] = []
            self.grace_periods: list[float] = []

        def terminate_many(self, processes: list[ProcessIdentity], *, grace_seconds: float) -> None:
            self.batches.append([process.role for process in processes])
            self.grace_periods.append(grace_seconds)
            self.owned.difference_update(process.pid for process in processes)

    config = resolve_local_desktop_config(project_root=tmp_path)
    config.runtime_root.mkdir(parents=True)
    inspector = CoordinatedInspector(set(range(41, 47)))
    manager = LifecycleManager(
        config,
        release_manager=SimpleNamespace(),
        doctor=FakeDoctor(),
        control_plane=FakeControl(),
        inspector=inspector,
    )
    roles = ("api:log:stdout", "api:log:stderr", "api", "frontend:log:stdout", "frontend:log:stderr", "frontend")
    processes = [
        ProcessIdentity(role, pid, 100, "C:/owned.exe", "digest", "out.log", "err.log")
        for role, pid in zip(roles, range(41, 47), strict=True)
    ]
    manager._record_state("READY", processes, release={}, reason_code="desktop_ready")

    result = manager.stop(process_timeout=0.4)

    assert result.status == "STOPPED"
    assert inspector.batches == [
        ["frontend", "api"],
        ["frontend:log:stderr", "frontend:log:stdout", "api:log:stderr", "api:log:stdout"],
    ]
    assert len(inspector.grace_periods) == 2
    assert 0 < inspector.grace_periods[0] < inspector.grace_periods[1]
    assert inspector.grace_periods[1] <= 0.4


def test_stop_converges_mixed_absent_and_owned_records_without_touching_absent_pid(tmp_path: Path) -> None:
    manager, inspector = _manager(tmp_path, owned={43})
    processes = [
        ProcessIdentity("api:log:stdout", 41, 100, "C:/owned.exe", "digest", "out.log", "err.log"),
        ProcessIdentity("api:log:stderr", 42, 100, "C:/owned.exe", "digest", "out.log", "err.log"),
        ProcessIdentity("api", 43, 100, "C:/owned.exe", "digest", "out.log", "err.log"),
    ]
    manager._record_state("STOPPING", processes, release={}, reason_code="desktop_stop_failed")

    result = manager.stop()

    assert result.status == "STOPPED"
    assert inspector.terminated == [43]


def test_open_stopped_instance_does_not_launch_browser(tmp_path: Path) -> None:
    opened: list[str] = []
    manager, _ = _manager(tmp_path, owned=set())
    manager.opener = opened.append

    with pytest.raises(DesktopOperationError) as raised:
        manager.open()

    assert raised.value.reason_code == "desktop_not_running"
    assert opened == []


def _runtime_identity() -> dict[str, str]:
    return {
        "deployment_profile": "local-desktop",
        "instance_key": "local-desktop:http://127.0.0.1:3000",
        "release_id": "release-1",
        "config_digest": "a" * 64,
        "boot_id": "b" * 32,
    }


def test_readiness_requires_exact_runtime_identity_and_business_schema() -> None:
    expected = _runtime_identity()
    health = {
        "payload": {
            "success": True,
            "data": {
                "status": "ok",
                "service": "agora-hub-api",
                "ready": True,
                "local_desktop": expected,
            },
        }
    }
    auth = {
        "payload": {
            "success": True,
            "data": {"initialized": False, "session": None, "local_desktop": expected},
        }
    }

    assert _api_health_ready(health, expected, require_runtime=True) is True
    assert _auth_route_ready(auth, expected) is True

    spoofed = {**expected, "boot_id": "c" * 32}
    assert _api_health_ready(health, spoofed, require_runtime=False) is False
    assert _auth_route_ready(auth, spoofed) is False


def test_process_topology_requires_both_services_and_all_collectors(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path, owned=set())
    complete = [
        ProcessIdentity(role, index, 100, "C:/owned.exe", "digest", "out.log", "err.log")
        for index, role in enumerate(("api", "api:log:stdout", "api:log:stderr", "frontend", "frontend:log:stdout", "frontend:log:stderr"), start=1)
    ]

    manager._require_complete_process_roles(complete)
    with pytest.raises(DesktopOperationError) as raised:
        manager._require_complete_process_roles(complete[:-1])
    assert raised.value.reason_code == "desktop_process_topology_incomplete"


def test_windows_venv_launch_tracks_direct_base_interpreter_without_losing_venv_identity(tmp_path: Path, monkeypatch) -> None:
    import scripts.local_desktop.processes as process_module

    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"launcher")
    base_home = tmp_path / "base-python"
    base_home.mkdir()
    base_python = base_home / "python.exe"
    base_python.write_bytes(b"runtime")
    (tmp_path / ".venv" / "pyvenv.cfg").write_text(f"home = {base_home}\n", encoding="utf-8")
    monkeypatch.setattr(process_module.os, "name", "nt")

    argv, environment = _direct_windows_venv_launch([str(venv_python), "-m", "worker"], {"SAFE": "1"})

    assert argv == [str(base_python), "-m", "worker"]
    assert environment == {"SAFE": "1", "__PYVENV_LAUNCHER__": str(venv_python)}


@pytest.mark.skipif(os.name != "nt", reason="Win32 process identity contract")
def test_windows_process_identity_uses_pointer_sized_handle_and_reads_current_process() -> None:
    kernel32 = _windows_kernel32()
    assert kernel32.OpenProcess.restype is wintypes.HANDLE

    identity = _windows_process_identity(os.getpid())

    assert identity is not None
    created, executable = identity
    assert created > 0
    expected_executable = getattr(sys, "_base_executable", sys.executable)
    assert os.path.normcase(str(Path(executable).resolve())) == os.path.normcase(str(Path(expected_executable).resolve()))



@pytest.mark.skipif(os.name != "nt", reason="Win32 exited-process identity contract")
def test_windows_process_identity_treats_exited_process_with_retained_handle_as_absent() -> None:
    child = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", "pass"])
    child.wait(timeout=5)

    assert _windows_process_identity(child.pid) is None


class StartReleaseManager:
    def __init__(self, release_path: Path) -> None:
        self.release_path = release_path
        self.manifest = {
            "identity": {"release_id": "release-1", "config_digest": "a" * 64},
            "entrypoints": {
                "api": ["-m", "interfaces.cli.news", "api", "serve", "--host", "127.0.0.1", "--port", "8000"],
                "frontend": ["next", "start", "-H", "127.0.0.1", "-p", "3000"],
            },
        }

    def active(self):
        return self.release_path, self.manifest

    def bind_runtime_environment(self, release_path, manifest, environment):
        assert release_path == self.release_path
        assert manifest is self.manifest
        return dict(environment)


class FakeChild:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self):
        return None


class StartInspector(FakeInspector):
    def terminate(self, process: ProcessIdentity, *, grace_seconds: float) -> None:
        self.terminated.append(process.pid)
        self.owned.discard(process.pid)


def _start_manager(tmp_path: Path) -> tuple[LifecycleManager, StartInspector]:
    config = resolve_local_desktop_config(project_root=tmp_path)
    (config.python_executable.parent).mkdir(parents=True, exist_ok=True)
    config.python_executable.write_bytes(b"python")
    release_path = config.release_root / "release-1"
    (release_path / "app").mkdir(parents=True)
    (release_path / "frontend" / "node_modules" / "next" / "dist" / "bin").mkdir(parents=True)
    (release_path / "frontend" / "node_modules" / "next" / "dist" / "bin" / "next").write_text("runtime", encoding="utf-8")
    inspector = StartInspector(set())
    manager = LifecycleManager(config, release_manager=StartReleaseManager(release_path), doctor=FakeDoctor(), control_plane=FakeControl(), inspector=inspector)
    return manager, inspector


def _fake_launch(role: str, *, start_pid: int) -> tuple[list[tuple[FakeChild, ProcessIdentity]], int]:
    processes = []
    pid = start_pid
    for actual_role in (f"{role}:log:stdout", f"{role}:log:stderr", role):
        identity = ProcessIdentity(actual_role, pid, 100, "C:/owned.exe", "digest", "out.log", "err.log")
        processes.append((FakeChild(pid), identity))
        pid += 1
    return processes, pid


def test_frontend_command_uses_verified_standalone_server_and_controlled_node(tmp_path: Path, monkeypatch) -> None:
    manager, _ = _start_manager(tmp_path)
    frontend_root = manager.release_manager.release_path / "frontend"
    server = frontend_root / "server.js"
    closure = frontend_root / "production-closure.json"
    server.write_text("server", encoding="utf-8")
    closure.write_text("{}", encoding="utf-8")
    controlled_node = str(tmp_path / "controlled" / "node.exe")
    monkeypatch.setattr("scripts.local_desktop.processes.shutil.which", lambda name: controlled_node if name == "node" else None)
    manifest = {"entrypoints": {"frontend": ["node", "server.js"]}}

    command = manager._frontend_command(manager.release_manager.release_path, manifest)

    assert command == [controlled_node, str(server)]


def test_frontend_command_keeps_exact_legacy_next_start_entrypoint(tmp_path: Path, monkeypatch) -> None:
    manager, _ = _start_manager(tmp_path)
    controlled_node = str(tmp_path / "controlled" / "node.exe")
    monkeypatch.setattr("scripts.local_desktop.processes.shutil.which", lambda name: controlled_node if name == "node" else None)
    manifest = {
        "entrypoints": {
            "frontend": [
                "next",
                "start",
                "-H",
                manager.config.frontend_host,
                "-p",
                str(manager.config.frontend_port),
            ]
        }
    }
    next_cli = manager.release_manager.release_path / "frontend" / "node_modules" / "next" / "dist" / "bin" / "next"

    command = manager._frontend_command(manager.release_manager.release_path, manifest)

    assert command == [
        controlled_node,
        str(next_cli),
        "start",
        "-H",
        manager.config.frontend_host,
        "-p",
        str(manager.config.frontend_port),
    ]


@pytest.mark.parametrize(
    "entrypoint",
    [
        ["node", "other.js"],
        ["node", "server.js", "--port", "9999"],
        ["next", "dev"],
        ["server.js"],
    ],
)
def test_frontend_command_rejects_unknown_entrypoint_without_guessing_from_server(
    tmp_path: Path,
    entrypoint: list[str],
) -> None:
    manager, _ = _start_manager(tmp_path)
    frontend_root = manager.release_manager.release_path / "frontend"
    (frontend_root / "server.js").write_text("server", encoding="utf-8")
    (frontend_root / "production-closure.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager._frontend_command(
            manager.release_manager.release_path,
            {"entrypoints": {"frontend": entrypoint}},
        )

    assert raised.value.reason_code == "desktop_release_manifest_invalid"


def test_frontend_launch_uses_standalone_command_and_overrides_host_port_environment(tmp_path: Path, monkeypatch) -> None:
    manager, inspector = _start_manager(tmp_path)
    frontend_root = manager.release_manager.release_path / "frontend"
    server = frontend_root / "server.js"
    closure = frontend_root / "production-closure.json"
    server.write_text("server", encoding="utf-8")
    closure.write_text("{}", encoding="utf-8")
    controlled_node = str(tmp_path / "controlled" / "node.exe")
    monkeypatch.setattr("scripts.local_desktop.processes.shutil.which", lambda name: controlled_node if name == "node" else None)
    command = manager._frontend_command(
        manager.release_manager.release_path,
        {"entrypoints": {"frontend": ["node", "server.js"]}},
    )
    spawned: list[tuple[list[str], dict[str, str], object]] = []
    next_pid = 900

    class Pipe:
        def close(self) -> None:
            return None

    class SpawnedChild(FakeChild):
        def __init__(self, pid: int, *, has_stdin: bool) -> None:
            super().__init__(pid)
            self.stdin = Pipe() if has_stdin else None

        def terminate(self) -> None:
            return None

    def popen(argv, **kwargs):
        nonlocal next_pid
        child = SpawnedChild(next_pid, has_stdin=kwargs.get("stdin") == subprocess.PIPE)
        inspector.owned.add(next_pid)
        next_pid += 1
        spawned.append((list(argv), dict(kwargs["env"]), kwargs.get("stdin")))
        return child

    manager.popen_factory = popen

    children = manager._launch(
        "frontend",
        command,
        frontend_root,
        {"HOSTNAME": "attacker.invalid", "PORT": "65535", "SAFE_DEFAULT": "retained"},
    )

    assert len(children) == 3
    assert all("HOSTNAME" not in environment and "PORT" not in environment for _, environment, _ in spawned[:2])
    service_argv, service_environment, service_stdin = spawned[2]
    assert service_argv == [controlled_node, str(server)]
    assert service_environment["HOSTNAME"] == manager.config.frontend_host
    assert service_environment["PORT"] == str(manager.config.frontend_port)
    assert service_environment["SAFE_DEFAULT"] == "retained"
    assert service_stdin == subprocess.DEVNULL


def test_start_child_environment_prevents_packaged_python_bytecode(tmp_path: Path, monkeypatch) -> None:
    manager, _ = _start_manager(tmp_path)
    release_app = manager.release_manager.release_path / "app"
    (release_app / "packaged_helper.py").write_text("VALUE = 'loaded'\n", encoding="utf-8")
    api_children, next_pid = _fake_launch("api", start_pid=50)
    frontend_children, _ = _fake_launch("frontend", start_pid=next_pid)
    environments: list[dict[str, str]] = []

    def launch(role, _argv, _cwd, environment):
        environments.append(dict(environment))
        return api_children if role == "api" else frontend_children

    monkeypatch.setattr(manager, "_launch", launch)
    monkeypatch.setattr("scripts.local_desktop.processes._port_is_listening", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_frontend_command", lambda *_args: ["node", "next"])
    monkeypatch.setattr(
        manager,
        "_wait_for_readiness",
        lambda *_args, **_kwargs: {
            "api": True,
            "frontend": True,
            "bff": True,
            "raw_rewrite": True,
            "runtime": True,
        },
    )

    result = manager.start(readiness_timeout=0.1)

    assert result.status == "READY"
    assert len(environments) == 2
    assert all(environment["PYTHONDONTWRITEBYTECODE"] == "1" for environment in environments)
    imported = subprocess.run(
        [sys.executable, "-c", "import packaged_helper; assert packaged_helper.VALUE == 'loaded'"],
        cwd=release_app,
        env=environments[0],
        capture_output=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr.decode("utf-8", errors="strict")
    assert not list(release_app.rglob("*.pyc"))
    assert not list(release_app.rglob("__pycache__"))


def test_start_cleans_every_owned_child_when_frontend_launch_fails(tmp_path: Path, monkeypatch) -> None:
    manager, inspector = _start_manager(tmp_path)
    api_children, _ = _fake_launch("api", start_pid=100)
    inspector.owned.update(identity.pid for _, identity in api_children)

    def launch(role, argv, cwd, environment):
        if role == "api":
            return api_children
        raise DesktopOperationError("desktop_frontend_launch_failed", "frontend failed")

    monkeypatch.setattr(manager, "_launch", launch)
    monkeypatch.setattr("scripts.local_desktop.processes._port_is_listening", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_frontend_command", lambda *_args: ["node", "next"])

    with pytest.raises(DesktopOperationError) as raised:
        manager.start(readiness_timeout=0.1)
    assert raised.value.reason_code == "desktop_frontend_launch_failed"
    assert inspector.terminated == [102, 101, 100]
    assert manager._instance()["status"] == "FAILED"


def test_start_cancellation_before_first_child_records_cancelled_without_launch(tmp_path: Path, monkeypatch) -> None:
    manager, inspector = _start_manager(tmp_path)
    monkeypatch.setattr(
        "scripts.local_desktop.processes.check_startup_cancellation",
        lambda: (_ for _ in ()).throw(DesktopOperationError("desktop_start_cancelled", "cancelled")),
    )
    monkeypatch.setattr(manager, "_launch", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cancelled start must not launch")))

    with pytest.raises(DesktopOperationError) as raised:
        manager.start()

    assert raised.value.reason_code == "desktop_start_cancelled"
    assert inspector.terminated == []
    instance = manager._instance()
    assert instance["status"] == "FAILED"
    assert instance["reason_code"] == "desktop_start_cancelled"
    assert instance["processes"] == []


def test_readiness_cancellation_cleans_owned_services_then_collectors_and_records_truth(tmp_path: Path, monkeypatch) -> None:
    manager, inspector = _start_manager(tmp_path)
    api_children, next_pid = _fake_launch("api", start_pid=700)
    frontend_children, _ = _fake_launch("frontend", start_pid=next_pid)
    all_children = [*api_children, *frontend_children]
    inspector.owned.update(identity.pid for _, identity in all_children)
    launched: list[str] = []
    batches: list[list[str]] = []
    checks = 0

    def launch(role, *_args):
        launched.append(role)
        return api_children if role == "api" else frontend_children

    def terminate_many(processes: list[ProcessIdentity], *, grace_seconds: float) -> None:
        assert grace_seconds > 0
        batches.append([process.role for process in processes])
        inspector.owned.difference_update(process.pid for process in processes)

    def check_cancellation() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise DesktopOperationError("desktop_start_cancelled", "cancelled during readiness")

    inspector.terminate_many = terminate_many  # type: ignore[attr-defined]
    monkeypatch.setattr(manager, "_launch", launch)
    monkeypatch.setattr(manager, "_frontend_command", lambda *_args: ["node", "next"])
    monkeypatch.setattr("scripts.local_desktop.processes.check_startup_cancellation", check_cancellation)

    with pytest.raises(DesktopOperationError) as raised:
        manager.start(readiness_timeout=30.0)

    assert raised.value.reason_code == "desktop_start_cancelled"
    assert launched == ["api", "frontend"]
    assert batches == [
        ["frontend", "api"],
        ["frontend:log:stderr", "frontend:log:stdout", "api:log:stderr", "api:log:stdout"],
    ]
    instance = manager._instance()
    assert instance["status"] == "FAILED"
    assert instance["reason_code"] == "desktop_start_cancelled"
    assert instance["processes"] == []


def test_cancelled_start_keeps_owned_records_when_cleanup_cannot_confirm_exit(tmp_path: Path, monkeypatch) -> None:
    manager, inspector = _start_manager(tmp_path)
    api_children, _ = _fake_launch("api", start_pid=800)
    inspector.owned.update(identity.pid for _, identity in api_children)
    checks = 0

    def check_cancellation() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise DesktopOperationError("desktop_start_cancelled", "cancelled before frontend")

    def terminate_many(_processes: list[ProcessIdentity], *, grace_seconds: float) -> None:
        assert grace_seconds > 0
        raise RuntimeError("cleanup transport failed")

    inspector.terminate_many = terminate_many  # type: ignore[attr-defined]
    monkeypatch.setattr(manager, "_launch", lambda role, *_args: api_children if role == "api" else (_ for _ in ()).throw(AssertionError("frontend must not launch")))
    monkeypatch.setattr("scripts.local_desktop.processes.check_startup_cancellation", check_cancellation)

    with pytest.raises(DesktopOperationError) as raised:
        manager.start()

    assert raised.value.reason_code == "desktop_start_cancelled"
    instance = manager._instance()
    assert instance["status"] == "FAILED"
    assert instance["reason_code"] == "desktop_start_cancelled"
    assert [item["pid"] for item in instance["processes"]] == [800, 801, 802]


def test_repeated_start_returns_existing_owned_instance_without_launch(tmp_path: Path, monkeypatch) -> None:
    manager, inspector = _start_manager(tmp_path)
    children, _ = _fake_launch("api", start_pid=200)
    frontend, _ = _fake_launch("frontend", start_pid=203)
    identities = [identity for _, identity in [*children, *frontend]]
    inspector.owned.update(identity.pid for identity in identities)
    release = {"release_id": "release-1", "config_digest": "a" * 64}
    manager._record_state("READY", identities, release=release, reason_code="desktop_ready", boot_id="b" * 32)
    monkeypatch.setattr(manager, "_readiness_once", lambda _expected: {"api": True, "frontend": True, "bff": True, "raw_rewrite": True, "runtime": True})
    monkeypatch.setattr(manager, "_launch", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch")))

    result = manager.start()
    assert result.reason_code == "desktop_instance_already_running"
    assert result.status == "READY"


def test_start_rejects_reused_recorded_pid_without_overwriting_ownership_evidence(tmp_path: Path, monkeypatch) -> None:
    manager, inspector = _start_manager(tmp_path)
    process = _process(42)
    manager._record_state("FAILED", [process], release={}, reason_code="desktop_stop_failed", boot_id="b" * 32)
    inspector.identity = lambda pid: (101, "C:/foreign.exe") if pid == 42 else None
    manager.release_manager.active = lambda: (_ for _ in ()).throw(AssertionError("foreign record must block launch"))

    with pytest.raises(DesktopOperationError) as raised:
        manager.start()

    assert raised.value.reason_code == "desktop_process_ownership_incomplete"
    instance = manager._instance()
    assert instance["processes"][0]["pid"] == 42
    assert instance["boot_id"] == "b" * 32


def test_start_requires_explicit_recovery_for_absent_recorded_topology(tmp_path: Path) -> None:
    manager, _ = _start_manager(tmp_path)
    process = _process(42)
    manager._record_state("FAILED", [process], release={}, reason_code="desktop_stop_failed", boot_id="b" * 32)
    manager.release_manager.active = lambda: (_ for _ in ()).throw(AssertionError("stale topology must block launch until stop recovery"))

    with pytest.raises(DesktopOperationError) as raised:
        manager.start()

    assert raised.value.reason_code == "desktop_process_ownership_incomplete"
    instance = manager._instance()
    assert instance["status"] == "FAILED"
    assert instance["processes"][0]["pid"] == 42


def test_start_requires_stop_recovery_for_owned_failed_topology(tmp_path: Path) -> None:
    manager, inspector = _start_manager(tmp_path)
    process = _process(42)
    inspector.owned.add(42)
    manager._record_state("FAILED", [process], release={}, reason_code="desktop_start_failed", boot_id="b" * 32)
    manager.release_manager.active = lambda: (_ for _ in ()).throw(AssertionError("owned failed topology must block relaunch"))

    with pytest.raises(DesktopOperationError) as raised:
        manager.start()

    assert raised.value.reason_code == "desktop_instance_recovery_required"
    instance = manager._instance()
    assert instance["status"] == "FAILED"
    assert instance["reason_code"] == "desktop_instance_recovery_required"
    assert instance["processes"][0]["pid"] == 42


def test_start_rejects_foreign_listening_port_before_loading_release(tmp_path: Path, monkeypatch) -> None:
    manager, _ = _start_manager(tmp_path)
    monkeypatch.setattr("scripts.local_desktop.processes._port_is_listening", lambda host, port: port == manager.config.frontend_port)
    manager.release_manager.active = lambda: (_ for _ in ()).throw(AssertionError("release must not load with foreign port"))

    with pytest.raises(DesktopOperationError) as raised:
        manager.start()
    assert raised.value.reason_code == "desktop_port_owned_elsewhere"


def test_start_readiness_deadline_cleans_owned_processes(tmp_path: Path, monkeypatch) -> None:
    manager, inspector = _start_manager(tmp_path)
    api_children, next_pid = _fake_launch("api", start_pid=300)
    frontend_children, _ = _fake_launch("frontend", start_pid=next_pid)
    all_children = [*api_children, *frontend_children]
    inspector.owned.update(identity.pid for _, identity in all_children)
    monkeypatch.setattr("scripts.local_desktop.processes._port_is_listening", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_frontend_command", lambda *_args: ["node", "next"])
    monkeypatch.setattr(manager, "_launch", lambda role, *_args: api_children if role == "api" else frontend_children)
    monkeypatch.setattr(manager, "_wait_for_readiness", lambda *_args, **_kwargs: {"api": False, "frontend": False, "bff": False, "raw_rewrite": False, "runtime": False, "reason_code": "desktop_readiness_pending"})

    with pytest.raises(DesktopOperationError) as raised:
        manager.start(readiness_timeout=0.01)
    assert raised.value.reason_code == "desktop_readiness_deadline"
    assert inspector.terminated == [305, 302, 304, 303, 301, 300]


def test_instance_lock_serializes_concurrent_start_operations(tmp_path: Path, monkeypatch) -> None:
    manager, _ = _start_manager(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr("scripts.local_desktop.processes._port_is_listening", lambda _host, _port: False)

    def active():
        entered.set()
        assert release.wait(timeout=2)
        raise DesktopOperationError("desktop_test_stop", "stop after lock observation")

    manager.release_manager.active = active
    first_error: list[Exception] = []

    def first_start():
        try:
            manager.start()
        except Exception as exc:
            first_error.append(exc)

    thread = threading.Thread(target=first_start)
    thread.start()
    assert entered.wait(timeout=2)
    second_entered = threading.Event()

    def second_start():
        try:
            manager.start()
        except Exception:
            second_entered.set()

    contender = threading.Thread(target=second_start)
    contender.start()
    time.sleep(0.05)
    assert second_entered.is_set() is False
    release.set()
    thread.join(timeout=2)
    contender.join(timeout=2)
    assert first_error and isinstance(first_error[0], DesktopOperationError)



def test_process_inspector_treats_absent_process_as_idempotently_stopped(monkeypatch) -> None:
    inspector = ProcessInspector()
    monkeypatch.setattr(inspector, "identity", lambda _pid: None)

    inspector.terminate(_process(42), grace_seconds=0.01)


def test_process_inspector_fails_closed_when_wait_reports_unknown_ownership(monkeypatch) -> None:
    import scripts.local_desktop.processes as process_module

    identity = _process(42)

    class UnknownDuringWaitInspector(ProcessInspector):
        def __init__(self) -> None:
            self.lookups = 0

        def ownership(self, process: ProcessIdentity) -> str:
            self.lookups += 1
            return "owned" if self.lookups <= 2 else "unknown"

    monkeypatch.setattr(process_module.os, "name", "posix")
    monkeypatch.setattr(process_module.os, "kill", lambda _pid, _signal: None)

    with pytest.raises(DesktopOperationError) as raised:
        UnknownDuringWaitInspector().terminate(identity, grace_seconds=1.0)

    assert raised.value.reason_code == "desktop_process_inspection_failed"


def test_posix_sigterm_rechecks_each_pid_before_signalling(monkeypatch) -> None:
    import scripts.local_desktop.processes as process_module

    first = ProcessIdentity("api", 41, 100, "C:/owned.exe", "digest", "out.log", "err.log")
    second = ProcessIdentity("frontend", 42, 100, "C:/owned.exe", "digest", "out.log", "err.log")

    class ReuseBeforeSignalInspector(ProcessInspector):
        def __init__(self) -> None:
            self.lookups: dict[int, int] = {}

        def ownership(self, process: ProcessIdentity) -> str:
            count = self.lookups.get(process.pid, 0) + 1
            self.lookups[process.pid] = count
            if process.pid == second.pid and count >= 2:
                return "foreign"
            return "owned"

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(process_module.os, "name", "posix")
    monkeypatch.setattr(process_module.os, "kill", lambda pid, requested_signal: signals.append((pid, requested_signal)))

    with pytest.raises(DesktopOperationError) as raised:
        ReuseBeforeSignalInspector().terminate_many([first, second], grace_seconds=1.0)

    assert raised.value.reason_code == "desktop_process_not_owned"
    assert signals == [(first.pid, process_module.signal.SIGTERM)]


def test_posix_sigkill_rechecks_pid_after_grace_period(monkeypatch) -> None:
    import scripts.local_desktop.processes as process_module

    identity = _process(42)

    class ReuseBeforeForceInspector(ProcessInspector):
        def __init__(self) -> None:
            self.lookups = 0

        def ownership(self, process: ProcessIdentity) -> str:
            self.lookups += 1
            return "owned" if self.lookups <= 4 else "foreign"

    signals: list[int] = []
    clock = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(process_module.os, "name", "posix")
    monkeypatch.setattr(process_module.os, "kill", lambda _pid, requested_signal: signals.append(requested_signal))
    monkeypatch.setattr(process_module.time, "monotonic", lambda: next(clock))

    with pytest.raises(DesktopOperationError) as raised:
        ReuseBeforeForceInspector().terminate(identity, grace_seconds=1.0)

    assert raised.value.reason_code == "desktop_process_not_owned"
    assert signals == [process_module.signal.SIGTERM]


def test_posix_shutdown_preserves_sigterm_graceful_path(monkeypatch) -> None:
    import scripts.local_desktop.processes as process_module

    identity = _process(42)
    inspector = ProcessInspector()
    alive = {42}
    signals: list[int] = []
    monkeypatch.setattr(inspector, "identity", lambda pid: (100, "C:/owned.exe") if pid in alive else None)
    monkeypatch.setattr(process_module.os, "name", "posix")

    def kill(pid: int, requested_signal: int) -> None:
        signals.append(requested_signal)
        if requested_signal == process_module.signal.SIGTERM:
            alive.discard(pid)

    monkeypatch.setattr(process_module.os, "kill", kill)

    inspector.terminate(identity, grace_seconds=5.0)

    assert signals == [process_module.signal.SIGTERM]


def test_windows_shutdown_uses_only_revalidated_process_handle_without_console_signal(monkeypatch) -> None:
    import scripts.local_desktop.processes as process_module

    identity = _process(42)
    inspector = ProcessInspector()
    calls: list[str] = []
    alive = {42}
    monkeypatch.setattr(inspector, "identity", lambda pid: (100, "C:/owned.exe") if pid in alive else None)
    monkeypatch.setattr(process_module.os, "name", "nt")
    monkeypatch.setattr(
        process_module,
        "_send_windows_break",
        lambda _value: (_ for _ in ()).throw(AssertionError("Windows shutdown must not broadcast console signals")),
    )
    monkeypatch.setattr(
        process_module,
        "_terminate_windows_process",
        lambda value: (calls.append(f"terminate:{value.pid}"), alive.discard(value.pid)),
    )
    monkeypatch.setattr(process_module.os, "kill", lambda *_args: (_ for _ in ()).throw(AssertionError("Windows lifecycle must not call os.kill")))
    monkeypatch.setattr(process_module.time, "sleep", lambda _seconds: (_ for _ in ()).throw(AssertionError("verified handle termination should converge without grace wait")))

    inspector.terminate(identity, grace_seconds=30.0)

    assert calls == ["terminate:42"]


def test_windows_pid_reuse_before_handle_termination_is_rejected(monkeypatch) -> None:
    import scripts.local_desktop.processes as process_module

    identity = _process(42)

    class ReuseInspector(ProcessInspector):
        def __init__(self) -> None:
            self.lookups = 0

        def identity(self, pid: int):
            self.lookups += 1
            if self.lookups == 1:
                return (100, "C:/owned.exe")
            return (101, "C:/reused.exe")

    terminated: list[int] = []
    monkeypatch.setattr(process_module.os, "name", "nt")
    monkeypatch.setattr(
        process_module,
        "_send_windows_break",
        lambda _value: (_ for _ in ()).throw(AssertionError("Windows shutdown must not broadcast console signals")),
    )
    monkeypatch.setattr(process_module, "_terminate_windows_process", lambda value: terminated.append(value.pid))

    with pytest.raises(DesktopOperationError) as raised:
        ReuseInspector().terminate(identity, grace_seconds=1.0)

    assert raised.value.reason_code == "desktop_process_not_owned"
    assert terminated == []


def test_windows_process_handle_revalidates_created_time_and_executable(monkeypatch) -> None:
    import scripts.local_desktop.processes as process_module

    class Kernel32:
        def __init__(self) -> None:
            self.terminated: list[tuple[object, int]] = []
            self.closed: list[object] = []

        def OpenProcess(self, _access: int, _inherit: bool, _pid: int) -> object:
            return object()

        def TerminateProcess(self, process: object, exit_code: int) -> bool:
            self.terminated.append((process, exit_code))
            return True

        def CloseHandle(self, process: object) -> bool:
            self.closed.append(process)
            return True

    kernel32 = Kernel32()
    monkeypatch.setattr(process_module, "_windows_kernel32", lambda: kernel32)
    monkeypatch.setattr(process_module, "_windows_process_is_active", lambda _kernel32, _process: True)
    monkeypatch.setattr(process_module, "_windows_handle_identity", lambda _kernel32, _process: (101, "C:/reused.exe"))

    with pytest.raises(DesktopOperationError) as raised:
        process_module._terminate_windows_process(_process(42))

    assert raised.value.reason_code == "desktop_process_not_owned"
    assert kernel32.terminated == []
    assert len(kernel32.closed) == 1


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 301])
def test_lifecycle_rejects_invalid_timeouts_before_side_effects(tmp_path: Path, monkeypatch, value: float) -> None:
    manager, inspector = _start_manager(tmp_path)
    monkeypatch.setattr(
        "scripts.local_desktop.processes._port_is_listening",
        lambda *_args: (_ for _ in ()).throw(AssertionError("timeout must fail first")),
    )

    with pytest.raises(ValueError, match="finite number"):
        manager.start(readiness_timeout=value)
    with pytest.raises(ValueError, match="finite number"):
        manager.stop(drain_timeout=value)
    with pytest.raises(ValueError, match="finite number"):
        manager.stop(process_timeout=value)
    with pytest.raises(ValueError, match="finite number"):
        manager.restart(readiness_timeout=value)
    with pytest.raises(ValueError, match="finite number"):
        manager.restart(drain_timeout=value)

    assert inspector.terminated == []


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 301])
def test_process_inspector_rejects_invalid_grace_timeout_before_identity_lookup(monkeypatch, value: float) -> None:
    inspector = ProcessInspector()
    monkeypatch.setattr(
        inspector,
        "identity",
        lambda _pid: (_ for _ in ()).throw(AssertionError("timeout must fail first")),
    )

    with pytest.raises(ValueError, match="finite number"):
        inspector.terminate(_process(42), grace_seconds=value)


def test_start_injects_fixed_credential_only_at_actual_api_spawn(tmp_path: Path, monkeypatch) -> None:
    manager, inspector = _start_manager(tmp_path)
    credential_name = "NEWSROOM_DESKTOP_RESEARCH_API_KEY"
    credential_value = "secret-never-serialized"
    manager.api_environment_provider = lambda: {credential_name: credential_value}
    manager.api_environment_names = (credential_name,)
    spawned: list[tuple[list[str], dict[str, str]]] = []
    next_pid = 550

    class Pipe:
        def close(self) -> None:
            return None

    class SpawnedChild(FakeChild):
        def __init__(self, pid: int, *, has_stdin: bool) -> None:
            super().__init__(pid)
            self.stdin = Pipe() if has_stdin else None
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

    def popen(argv, **kwargs):
        nonlocal next_pid
        child = SpawnedChild(next_pid, has_stdin=kwargs.get("stdin") == subprocess.PIPE)
        inspector.owned.add(next_pid)
        next_pid += 1
        spawned.append((list(argv), dict(kwargs["env"])))
        return child

    manager.popen_factory = popen

    def bind_environment(_release_path, _manifest, environment):
        return {**environment, "SAFE_DEFAULT": "retained", credential_name: "stale-parent-value"}

    manager.release_manager.bind_runtime_environment = bind_environment
    monkeypatch.setattr(manager, "_frontend_command", lambda *_args: ["node", "next"])
    monkeypatch.setattr(
        manager,
        "_wait_for_readiness",
        lambda *_args, **_kwargs: {
            "api": True,
            "frontend": True,
            "bff": True,
            "raw_rewrite": True,
            "runtime": True,
        },
    )

    result = manager.start(readiness_timeout=0.1)

    assert result.status == "READY"
    assert len(spawned) == 6
    api_spawn = spawned[2][1]
    collector_spawns = [spawned[index][1] for index in (0, 1, 3, 4)]
    frontend_spawn = spawned[5][1]
    assert all("scripts.local_desktop.log_collector" in spawned[index][0] for index in (0, 1, 3, 4))
    assert api_spawn[credential_name] == credential_value
    assert all(credential_name not in environment for environment in collector_spawns)
    assert credential_name not in frontend_spawn
    assert all(environment["SAFE_DEFAULT"] == "retained" for _argv, environment in spawned)
    assert credential_value not in str(result.details)
    assert credential_value not in manager.instance_path.read_text(encoding="utf-8")


def test_launch_rejects_api_environment_name_outside_allow_list_and_cleans_collectors(tmp_path: Path) -> None:
    manager, inspector = _start_manager(tmp_path)
    manager.api_environment_provider = lambda: {"ATTACKER_ENV": "secret"}
    manager.api_environment_names = ("NEWSROOM_DESKTOP_RESEARCH_API_KEY",)
    spawned: list[object] = []
    next_pid = 650

    class Pipe:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class SpawnedCollector(FakeChild):
        def __init__(self, pid: int) -> None:
            super().__init__(pid)
            self.stdin = Pipe()
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

    def popen(_argv, **_kwargs):
        nonlocal next_pid
        child = SpawnedCollector(next_pid)
        inspector.owned.add(next_pid)
        next_pid += 1
        spawned.append(child)
        return child

    manager.popen_factory = popen

    with pytest.raises(DesktopOperationError) as raised:
        manager._launch("api", [str(manager.config.python_executable), "-m", "api"], tmp_path, {"SAFE": "1"})

    assert raised.value.reason_code == "desktop_api_environment_projection_invalid"
    assert len(spawned) == 2
    assert all(child.stdin.closed and child.terminated for child in spawned)
