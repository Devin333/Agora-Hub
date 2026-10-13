from __future__ import annotations

import os
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
    _windows_kernel32,
    _windows_process_identity,
)


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
    manager._record_state("READY", [_process(42)], release={}, reason_code="desktop_ready")

    with pytest.raises(DesktopOperationError) as raised:
        manager.stop()

    assert raised.value.reason_code == "desktop_drain_busy"
    assert inspector.terminated == []
    assert manager._instance()["status"] == "STOPPING"


def test_stale_pid_record_never_stops_reused_process(tmp_path: Path) -> None:
    manager, inspector = _manager(tmp_path, owned=set())
    manager._record_state("READY", [_process(42)], release={}, reason_code="desktop_ready")

    with pytest.raises(DesktopOperationError) as raised:
        manager.stop()

    assert raised.value.reason_code == "desktop_process_ownership_incomplete"
    assert inspector.terminated == []


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
    assert inspector.terminated == [305, 304, 303, 302, 301, 300]


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



def test_windows_graceful_signal_failure_uses_explicit_owned_termination_without_os_kill(monkeypatch) -> None:
    import scripts.local_desktop.processes as process_module

    identity = _process(42)
    inspector = ProcessInspector()
    calls: list[str] = []
    monkeypatch.setattr(inspector, "identity", lambda pid: (100, "C:/owned.exe") if pid == 42 else None)
    monkeypatch.setattr(process_module.os, "name", "nt")
    monkeypatch.setattr(process_module, "_send_windows_break", lambda value: calls.append(f"break:{value.pid}") or False)
    monkeypatch.setattr(process_module, "_terminate_windows_process", lambda value: calls.append(f"terminate:{value.pid}"))
    monkeypatch.setattr(process_module.os, "kill", lambda *_args: (_ for _ in ()).throw(AssertionError("Windows lifecycle must not call os.kill")))

    inspector.terminate(identity, grace_seconds=1e-9)

    assert calls == ["break:42", "terminate:42"]


def test_windows_pid_reuse_after_failed_break_is_not_terminated(monkeypatch) -> None:
    import scripts.local_desktop.processes as process_module

    identity = _process(42)
    identities = iter([(100, "C:/owned.exe"), (101, "C:/reused.exe")])

    class ReuseInspector(ProcessInspector):
        def identity(self, pid: int):
            return next(identities)

    terminated: list[int] = []
    monkeypatch.setattr(process_module.os, "name", "nt")
    monkeypatch.setattr(process_module, "_send_windows_break", lambda _value: False)
    monkeypatch.setattr(process_module, "_terminate_windows_process", lambda value: terminated.append(value.pid))
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(process_module.time, "monotonic", lambda: next(clock))

    ReuseInspector().terminate(identity, grace_seconds=1.0)

    assert terminated == []


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
