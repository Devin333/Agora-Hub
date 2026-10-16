from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.desktop_app.cli as cli_module
from scripts.desktop_app.cli import main
from scripts.local_desktop.common import OperationResult
from scripts.local_desktop.lifecycle_cancellation import check_startup_cancellation, startup_cancellation_context


class FakeRuntime:
    instances: list["FakeRuntime"] = []

    def __init__(self, config) -> None:
        self.config = config
        self.calls: list[tuple[str, object]] = []
        self.__class__.instances.append(self)

    def start(self, *, timeout: float):
        self.calls.append(("start", timeout))
        return OperationResult(True, "start", "READY", "desktop_started", "ready", {"secret": "safe"})

    def status(self):
        self.calls.append(("status", None))
        return OperationResult(True, "status", "STOPPED", "desktop_stopped", "stopped", {})

    def stop(self, *, timeout: float):
        self.calls.append(("stop", timeout))
        return OperationResult(True, "stop", "STOPPED", "desktop_stopped", "stopped", {})

    def doctor(self):
        self.calls.append(("doctor", None))
        return OperationResult(True, "doctor", "WARNING", "desktop_doctor_ready", "offline", {"network_probe": False})

    def backup(self, *, destination=None):
        self.calls.append(("backup", destination))
        return OperationResult(True, "backup", "READY", "desktop_backup_created", "created", {})

    def inventory(self):
        self.calls.append(("inventory", None))
        return OperationResult(True, "inventory", "UNINITIALIZED", "desktop_inventory_resolved", "resolved", {})

    def credential_inspect(self):
        self.calls.append(("credential-inspect", None))
        return OperationResult(True, "credential-inspect", "CONFIGURED", "desktop_credential_inspected", "inspected", {"configured": True})

    def credential_prompt(self, *, parent_window):
        self.calls.append(("credential-prompt", parent_window))
        return OperationResult(True, "credential-prompt", "CONFIGURED", "desktop_credential_replaced", "replaced", {"configured": True, "changed": True, "cancelled": False})

    def credential_delete(self, *, confirmation_token, timeout):
        self.calls.append(("credential-delete", {"confirmation_token": confirmation_token, "timeout": timeout}))
        return OperationResult(True, "credential-delete", "UNCONFIGURED", "desktop_credential_deleted", "deleted", {"configured": False})

    def config_show(self):
        self.calls.append(("config-show", None))
        return OperationResult(True, "config-show", "UNCONFIGURED", "desktop_runtime_config_read", "read", {})

    def config_preview(self, *, candidate):
        self.calls.append(("config-preview", candidate))
        return OperationResult(True, "config-preview", "CONFIRMATION_REQUIRED", "desktop_runtime_config_preview_ready", "preview", {"confirmation_token": "desktop-config:" + "a" * 32 + ":" + "b" * 16})

    def config_apply(self, **kwargs):
        self.calls.append(("config-apply", kwargs))
        return OperationResult(True, "config-apply", "APPLIED", "desktop_runtime_config_applied", "applied", {})

    def build(self, **kwargs):
        self.calls.append(("build", kwargs))
        return OperationResult(True, "build", "STAGED", "desktop_app_bundle_staged", "staged", {})

    def restore(self, **kwargs):
        self.calls.append(("restore", kwargs))
        return OperationResult(True, "restore", "READY", "desktop_restore_complete", "restored", {})


def _args(tmp_path: Path, command: str) -> list[str]:
    resource = tmp_path / "runtime"
    resource.mkdir()
    return [
        "--json",
        "--resource-root",
        str(resource),
        "--data-root",
        str(tmp_path / "user-data"),
        "--frontend-port",
        "3120",
        "--api-port",
        "8120",
        command,
    ]


def test_cli_emits_exactly_one_json_line_with_canonical_identity(tmp_path: Path, capfd) -> None:
    FakeRuntime.instances.clear()

    exit_code = main(_args(tmp_path, "status"), runtime_factory=FakeRuntime)

    captured = capfd.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload == {
        "code": "desktop_stopped",
        "command": "status",
        "data": {
            "api_base_url": "http://127.0.0.1:8120",
            "canonical_origin": "http://127.0.0.1:3120",
            "instance_key": "local-desktop:http://127.0.0.1:3120",
            "storage_mode": "owned-local",
        },
        "message": "stopped",
        "ok": True,
        "state": "STOPPED",
    }


def test_cli_doctor_is_offline_by_contract(tmp_path: Path, capfd) -> None:
    FakeRuntime.instances.clear()

    assert main(_args(tmp_path, "doctor"), runtime_factory=FakeRuntime) == 0

    payload = json.loads(capfd.readouterr().out)
    assert payload["data"]["network_probe"] is False
    assert FakeRuntime.instances[-1].calls == [("doctor", None)]


def test_cli_invalid_arguments_still_emit_one_error_json_line(capfd) -> None:
    exit_code = main(["status"], runtime_factory=FakeRuntime)

    captured = capfd.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["code"] == "desktop_app_cli_invalid"


def test_packaged_cli_rejects_inherited_python_paths_before_runtime_construction(
    tmp_path: Path,
    capfd,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path, "status")
    resource_root = Path(args[args.index("--resource-root") + 1])
    (resource_root / "desktop-bundle.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", r"C:\unsafe-modules")

    exit_code = main(args, runtime_factory=FakeRuntime)

    payload = json.loads(capfd.readouterr().out)
    assert exit_code != 0
    assert payload["code"] == "desktop_app_python_environment_unsafe"


def test_packaged_cli_requires_user_site_isolation_at_interpreter_start(
    tmp_path: Path,
    capfd,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path, "status")
    resource_root = Path(args[args.index("--resource-root") + 1])
    (resource_root / "desktop-bundle.json").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(cli_module.sys, "flags", SimpleNamespace(no_user_site=0))

    exit_code = main(args, runtime_factory=FakeRuntime)

    payload = json.loads(capfd.readouterr().out)
    assert exit_code != 0
    assert payload["code"] == "desktop_app_python_user_site_enabled"


def test_cli_restore_passes_bounded_personal_memory_confirmation(tmp_path: Path, capfd) -> None:
    FakeRuntime.instances.clear()
    args = _args(tmp_path, "restore")
    token = "restore-personal-memory:backup-20261014"
    args.extend(["--backup-path", str(tmp_path / "backup"), "--activate", "--personal-memory-confirmation", token])

    assert main(args, runtime_factory=FakeRuntime) == 0

    payload = json.loads(capfd.readouterr().out)
    assert payload["ok"] is True
    assert FakeRuntime.instances[-1].calls == [
        (
            "restore",
            {
                "backup": str(tmp_path / "backup"),
                "target_root": None,
                "activate": True,
                "personal_memory_confirmation": token,
            },
        )
    ]


def test_cli_restore_rejects_unbounded_personal_memory_confirmation(tmp_path: Path, capfd) -> None:
    args = _args(tmp_path, "restore")
    args.extend(["--backup-path", str(tmp_path / "backup"), "--personal-memory-confirmation", "x" * 513])

    assert main(args, runtime_factory=FakeRuntime) == 2

    payload = json.loads(capfd.readouterr().out)
    assert payload["code"] == "desktop_app_cli_invalid"


def test_cli_config_apply_passes_typed_candidate_and_bounded_confirmation(tmp_path: Path, capfd) -> None:
    FakeRuntime.instances.clear()
    candidate = {
        "llm": {"provider": "openai-compatible", "model": "research-model", "base_url": "https://api.example.test/v1"},
        "source": {"preset": "arxiv-default", "api_url": "https://export.arxiv.org/api/query", "timeout_seconds": 30},
    }
    token = "desktop-config:" + "a" * 32 + ":" + "b" * 16
    args = _args(tmp_path, "config-apply")
    args.extend(["--candidate-json", json.dumps(candidate), "--expected-revision", "0", "--confirmation-token", token])

    assert main(args, runtime_factory=FakeRuntime) == 0

    payload = json.loads(capfd.readouterr().out)
    assert payload["state"] == "APPLIED"
    assert FakeRuntime.instances[-1].calls == [
        (
            "config-apply",
            {"candidate": candidate, "expected_revision": 0, "confirmation_token": token, "timeout": 60.0},
        )
    ]


def test_cli_config_apply_rejects_unbounded_confirmation_before_runtime_construction(tmp_path: Path, capfd) -> None:
    FakeRuntime.instances.clear()
    args = _args(tmp_path, "config-apply")
    args.extend(["--candidate-json", "{}", "--expected-revision", "0", "--confirmation-token", "x" * 513])

    assert main(args, runtime_factory=FakeRuntime) == 2

    payload = json.loads(capfd.readouterr().out)
    assert payload["code"] == "desktop_app_cli_invalid"
    assert FakeRuntime.instances == []


def test_cli_credential_commands_use_bounded_native_parent_and_boolean_status(tmp_path: Path, capfd) -> None:
    FakeRuntime.instances.clear()
    for child in ("prompt", "inspect", "delete"):
        (tmp_path / child).mkdir()
    prompt_args = _args(tmp_path / "prompt", "credential-prompt") + ["--parent-window", "1234"]
    assert main(prompt_args, runtime_factory=FakeRuntime) == 0
    prompt_payload = json.loads(capfd.readouterr().out)
    assert prompt_payload["data"]["configured"] is True
    assert FakeRuntime.instances[-1].calls == [("credential-prompt", 1234)]

    inspect_args = _args(tmp_path / "inspect", "credential-inspect")
    assert main(inspect_args, runtime_factory=FakeRuntime) == 0
    inspect_payload = json.loads(capfd.readouterr().out)
    assert inspect_payload["data"]["configured"] is True

    delete_args = _args(tmp_path / "delete", "credential-delete") + ["--confirmation-token", "delete-research-credential"]
    assert main(delete_args, runtime_factory=FakeRuntime) == 0
    delete_payload = json.loads(capfd.readouterr().out)
    assert delete_payload["data"]["configured"] is False
    assert FakeRuntime.instances[-1].calls == [("credential-delete", {"confirmation_token": "delete-research-credential", "timeout": 60.0})]


def test_cli_credential_prompt_rejects_zero_negative_and_pointer_text(tmp_path: Path, capfd) -> None:
    for index, value in enumerate(("0", "-1", "0x1234", str(2**63))):
        FakeRuntime.instances.clear()
        case_root = tmp_path / str(index)
        case_root.mkdir()
        assert main(_args(case_root, "credential-prompt") + ["--parent-window", value], runtime_factory=FakeRuntime) == 2
        payload = json.loads(capfd.readouterr().out)
        assert payload["code"] == "desktop_app_cli_invalid"
        assert FakeRuntime.instances == []


def test_cli_start_cancellation_emits_one_final_json_and_keeps_diagnostics_on_stderr(
    tmp_path: Path,
    capfd,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancellableRuntime(FakeRuntime):
        def start(self, *, timeout: float):
            self.calls.append(("start", timeout))
            for _ in range(100):
                check_startup_cancellation()
                threading.Event().wait(0.001)
            raise AssertionError("startup cancellation was not observed")

    private_control = b'{"command":"cancel-start"}\n'
    monkeypatch.setattr(
        cli_module,
        "startup_cancellation_context",
        lambda: startup_cancellation_context(stream=io.BytesIO(private_control)),
    )

    exit_code = main(_args(tmp_path, "start"), runtime_factory=CancellableRuntime)

    captured = capfd.readouterr()
    assert exit_code == 4
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["state"] == "ERROR"
    assert payload["code"] == "desktop_start_cancelled"
    assert "cancel-start" not in captured.out
    assert captured.err == "desktop startup cancellation requested\n"
