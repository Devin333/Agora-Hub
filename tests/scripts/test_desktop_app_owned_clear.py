from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path

import pytest

import scripts.desktop_app.cli as cli_module
import scripts.desktop_app.owned_clear as owned_clear_module
from scripts.desktop_app.cli import main as cli_main
from scripts.desktop_app.config import resolve_desktop_app_config
from scripts.desktop_app.credentials import CredentialError, CredentialPresence
from scripts.desktop_app.owned_clear import CLEAR_CONFIRMATION_TEXT, MARKER_NAME, OwnedDataClearOwner, fence_normal_initialization
from scripts.desktop_app.protocol import MAX_PROTOCOL_BYTES, read_bounded_stdin_json
from scripts.local_desktop.common import DesktopOperationError, OperationResult, atomic_write_json
from scripts.local_desktop.processes import ProcessIdentity


class FakeVault:
    def __init__(self, *, present: bool = True, fail_delete: bool = False) -> None:
        self.present = present
        self.fail_delete = fail_delete
        self.reads = 0

    def inspect(self, descriptor):
        return CredentialPresence(self.present)

    def read(self, descriptor):
        self.reads += 1
        raise AssertionError("clear flow must never read a credential value")

    def write(self, descriptor, secret):
        raise AssertionError("clear flow must never write a credential")

    def delete(self, descriptor):
        if self.fail_delete:
            raise CredentialError("desktop_credential_delete_failed", "vault refused deletion")
        changed = self.present
        self.present = False
        return changed

    def prompt_and_replace(self, descriptor, *, parent_window=0):
        raise AssertionError("clear flow must not prompt for credentials")


class SequencedInspector:
    def __init__(self, identities):
        self.identities = list(identities)
        self.calls = 0

    def identity(self, pid: int):
        index = min(self.calls, len(self.identities) - 1)
        self.calls += 1
        return self.identities[index]

    def ownership(self, process: ProcessIdentity) -> str:
        actual = self.identity(process.pid)
        if actual is None:
            return "absent"
        if actual == (process.created, process.executable):
            return "owned"
        return "foreign"


def _fixture(tmp_path: Path, *, vault: FakeVault | None = None, inspector=None, now=lambda: 1000.0):
    resource = tmp_path / "resources"
    resource.mkdir()
    profile = tmp_path / "userData"
    data_root = profile / "runtime"
    owner = OwnedDataClearOwner(
        resource_root=resource,
        data_root=data_root,
        credential_vault=vault,
        process_inspector=inspector,
        now=now,
        sleep=lambda _: None,
        finalizer_wait_seconds=1,
    )
    assert owner.register_for_start() is True
    return owner, resource, profile, data_root


def _stop(_: float) -> OperationResult:
    return OperationResult(True, "stop", "STOPPED", "desktop_stopped", "stopped", {})


def _apply_request(preview_id: str, *, phrase: str = CLEAR_CONFIRMATION_TEXT) -> dict[str, object]:
    return {"version": 1, "previewId": preview_id, "confirmationText": phrase, "electron": {"pid": 4242, "creationTime": "90071992547409931"}}


def _finalize_request(operation_id: str, capability: str) -> dict[str, object]:
    return {"version": 1, "operationId": operation_id, "finalizerCapability": capability, "electron": {"pid": 4242, "creationTime": "90071992547409931"}, "readyNonce": "ready-1"}


def test_full_clear_allows_bounded_host_volatile_settle_and_preserves_external_files(tmp_path: Path) -> None:
    identity = (90071992547409931, "C:/Program Files/Agora/Agora.exe")
    inspector = SequencedInspector([identity, identity, (123456, "C:/Python/python.exe"), None])
    vault = FakeVault()
    owner, resource, profile, data_root = _fixture(tmp_path, vault=vault, inspector=inspector)
    business_file = data_root / "business.db"
    business_file.write_bytes(b"business")
    business_file.chmod(stat.S_IREAD)
    cache = profile / "Cache"
    cache.mkdir()
    (cache / "entry").write_bytes(b"before")
    external = tmp_path / "external-export.zip"
    external.write_bytes(b"keep")

    preview = owner.preview(stop_runtime=_stop, timeout=10)
    assert preview.status == "CONFIRMATION_REQUIRED"
    assert "CAS" in " ".join(preview.details["scope"]["includes"])
    assert "profile" in " ".join(preview.details["scope"]["includes"])
    (cache / "entry").write_bytes(b"after-preview-host-write")
    (cache / "new-entry").write_bytes(b"new-host-cache")

    applied = owner.apply(_apply_request(preview.details["previewId"]))
    assert applied.status == "PREPARED"
    assert applied.details["exitRequired"] is True
    assert profile.exists()
    ready = []
    finalized = owner.finalize(
        _finalize_request(applied.details["operationId"], applied.details["finalizerCapability"]),
        ready=lambda operation, nonce: ready.append((operation, nonce)),
    )

    assert finalized.status == "CLEARED"
    assert ready == [(applied.details["operationId"], "ready-1")]
    assert not profile.exists()
    assert resource.is_dir()
    assert external.read_bytes() == b"keep"
    assert vault.present is False
    assert vault.reads == 0
    assert owner.status().status == "CLEARED"
    receipt = json.loads(owner.paths.receipt.read_text(encoding="utf-8"))
    assert receipt["contentRetained"] is False
    assert "snapshot" not in json.dumps(receipt).lower()
    assert "profilePath" not in receipt


def test_business_runtime_change_fails_cas_but_profile_cache_change_does_not(tmp_path: Path) -> None:
    identity = (90071992547409931, "C:/Agora.exe")
    owner, _resource, profile, data_root = _fixture(tmp_path, vault=FakeVault(), inspector=SequencedInspector([identity]))
    business = data_root / "business.json"
    business.write_text("one", encoding="utf-8")
    cache = profile / "Cache"
    cache.mkdir()
    (cache / "entry").write_text("one", encoding="utf-8")
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    (cache / "entry").write_text("two", encoding="utf-8")
    business.write_text("two", encoding="utf-8")

    with pytest.raises(DesktopOperationError, match="business runtime changed") as error:
        owner.apply(_apply_request(preview.details["previewId"]))
    assert error.value.reason_code == "desktop_data_clear_business_changed"
    assert profile.exists()


def test_phrase_mismatch_expiry_and_replay_preserve_profile(tmp_path: Path) -> None:
    clock = [1000.0]
    identity = (90071992547409931, "C:/Agora.exe")
    owner, _resource, profile, _data_root = _fixture(tmp_path, vault=FakeVault(), inspector=SequencedInspector([identity]), now=lambda: clock[0])
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    with pytest.raises(DesktopOperationError) as mismatch:
        owner.apply(_apply_request(preview.details["previewId"], phrase="wrong"))
    assert mismatch.value.reason_code == "desktop_data_clear_confirmation_mismatch"
    clock[0] = 2000.0
    with pytest.raises(DesktopOperationError) as expired:
        owner.apply(_apply_request(preview.details["previewId"]))
    assert expired.value.reason_code == "desktop_data_clear_preview_expired"
    assert profile.exists()

    clock[0] = 2100.0
    preview2 = owner.preview(stop_runtime=_stop, timeout=10)
    applied = owner.apply(_apply_request(preview2.details["previewId"]))
    assert applied.status == "PREPARED"
    with pytest.raises(DesktopOperationError) as replay:
        owner.apply(_apply_request(preview2.details["previewId"]))
    assert replay.value.reason_code == "desktop_data_clear_preview_replayed"
    assert profile.exists()


def test_unknown_legacy_runtime_starts_compatibly_but_clear_does_not_adopt_it(tmp_path: Path) -> None:
    resource = tmp_path / "resources"
    resource.mkdir()
    profile = tmp_path / "userData"
    runtime = profile / "runtime"
    runtime.mkdir(parents=True)
    legacy = runtime / "legacy.db"
    legacy.write_bytes(b"legacy")
    owner = OwnedDataClearOwner(resource_root=resource, data_root=runtime, credential_vault=FakeVault())

    assert owner.register_for_start() is False
    assert not (profile / MARKER_NAME).exists()
    assert legacy.read_bytes() == b"legacy"
    with pytest.raises(DesktopOperationError) as error:
        owner.preview(stop_runtime=_stop, timeout=10)
    assert error.value.reason_code == "desktop_data_clear_marker_missing"


def test_nonfixed_portable_data_root_keeps_existing_config_contract(tmp_path: Path) -> None:
    resource = tmp_path / "resources"
    resource.mkdir()
    portable = tmp_path / "paper-agent-portable-k"
    config = resolve_desktop_app_config(resource_root=resource, data_root=portable, environ={})
    assert config.data_root == portable.resolve()
    with pytest.raises(DesktopOperationError) as error:
        OwnedDataClearOwner(resource_root=resource, data_root=portable)
    assert error.value.reason_code == "desktop_data_clear_layout_invalid"


def test_pending_and_failed_partial_states_fence_before_config_recovery(tmp_path: Path) -> None:
    identity = (90071992547409931, "C:/Agora.exe")
    owner, resource, _profile, data_root = _fixture(tmp_path, vault=FakeVault(), inspector=SequencedInspector([identity]))
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    owner.apply(_apply_request(preview.details["previewId"]))
    with pytest.raises(DesktopOperationError) as pending:
        resolve_desktop_app_config(resource_root=resource, data_root=data_root, environ={})
    assert pending.value.reason_code == "desktop_data_clear_start_blocked"
    journal = json.loads(owner.paths.journal.read_text(encoding="utf-8"))
    journal.update({"revision": journal["revision"] + 1, "state": "FAILED_PARTIAL"})
    atomic_write_json(owner.paths.journal, journal)
    with pytest.raises(DesktopOperationError) as partial:
        fence_normal_initialization(data_root)
    assert partial.value.details["state"] == "FAILED_PARTIAL"


def test_vault_failure_is_truthful_failed_partial_and_does_not_report_cleared(tmp_path: Path) -> None:
    identity = (90071992547409931, "C:/Agora.exe")
    vault = FakeVault(fail_delete=True)
    inspector = SequencedInspector([identity, identity, (123456, "C:/Python/python.exe"), None])
    owner, _resource, profile, _data_root = _fixture(tmp_path, vault=vault, inspector=inspector)
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    applied = owner.apply(_apply_request(preview.details["previewId"]))
    with pytest.raises(DesktopOperationError) as error:
        owner.finalize(_finalize_request(applied.details["operationId"], applied.details["finalizerCapability"]), ready=lambda *_: None)
    assert error.value.reason_code == "desktop_credential_delete_failed"
    assert profile.exists()
    assert owner.status().status == "FAILED_PARTIAL"
    with pytest.raises(DesktopOperationError):
        fence_normal_initialization(owner.paths.runtime_root)


def test_profile_replacement_hardlink_and_unowned_process_fail_closed(tmp_path: Path) -> None:
    identity = (90071992547409931, "C:/Agora.exe")
    owner, _resource, profile, data_root = _fixture(tmp_path, vault=FakeVault(), inspector=SequencedInspector([identity]))
    (data_root / "business.db").write_bytes(b"one")
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    original = tmp_path / "original-profile"
    profile.rename(original)
    shutil.copytree(original, profile)
    with pytest.raises(DesktopOperationError) as replaced:
        owner.apply(_apply_request(preview.details["previewId"]))
    assert replaced.value.reason_code == "desktop_data_clear_profile_replaced"

    shutil.rmtree(profile)
    original.rename(profile)
    first = data_root / "first.bin"
    second = data_root / "second.bin"
    first.write_bytes(b"same inode")
    os.link(first, second)
    with pytest.raises(DesktopOperationError) as hardlink:
        owner.preview(stop_runtime=_stop, timeout=10)
    assert hardlink.value.reason_code == "desktop_data_clear_hardlink_rejected"
    second.unlink()
    first.unlink()

    process = ProcessIdentity("api", 77, 100, "C:/owned.exe", "digest", "out", "err")
    atomic_write_json(data_root / "instance.json", {"schema": "newsroom.local-desktop-instance/v1", "status": "READY", "processes": [process.to_dict()]})
    owner.process_inspector = SequencedInspector([(101, "C:/foreign.exe")])
    with pytest.raises(DesktopOperationError) as unowned:
        owner.preview(stop_runtime=_stop, timeout=10)
    assert unowned.value.reason_code == "desktop_data_clear_process_not_owned"


def test_bounded_stdin_rejects_duplicate_keys_oversize_and_non_object() -> None:
    with pytest.raises(DesktopOperationError):
        read_bounded_stdin_json(stream=io.BytesIO(b'{"version":1,"version":1}'))
    with pytest.raises(DesktopOperationError):
        read_bounded_stdin_json(stream=io.BytesIO(b"[]"))
    with pytest.raises(DesktopOperationError):
        read_bounded_stdin_json(stream=io.BytesIO(b"{" + b"x" * MAX_PROTOCOL_BYTES + b"}"))


@pytest.mark.parametrize("name,is_directory", [
    ("GPUPersistentCache", True),
    ("ShaderCache", True),
    ("Local State", False),
    ("declarative_performance_observer.db", False),
    ("declarative_performance_observer.db-journal", False),
    ("declarative_performance_observer.db-wal", False),
    ("declarative_performance_observer.db-shm", False),
    ("host-preferences", True),
])
def test_electron_44_confirmed_top_level_owners_are_accepted_exactly(tmp_path: Path, name: str, is_directory: bool) -> None:
    owner, _resource, profile, _data_root = _fixture(tmp_path, vault=FakeVault())
    target = profile / name
    if is_directory:
        target.mkdir()
        (target / "entry").write_bytes(b"cache")
    else:
        target.write_bytes(b"metadata")
    result = owner.preview(stop_runtime=_stop, timeout=10)
    assert result.status == "CONFIRMATION_REQUIRED"


def test_prepared_without_finalizer_can_be_explicitly_repreviewed_and_cleared(tmp_path: Path) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    inspector = SequencedInspector([electron, electron, electron, (123456, "C:/Python/python.exe"), None])
    owner, _resource, profile, data_root = _fixture(tmp_path, vault=FakeVault(), inspector=inspector)
    (data_root / "business.db").write_bytes(b"business")
    first_preview = owner.preview(stop_runtime=_stop, timeout=10)
    first_apply = owner.apply(_apply_request(first_preview.details["previewId"]))
    with pytest.raises(DesktopOperationError) as blocked:
        fence_normal_initialization(data_root)
    assert blocked.value.reason_code == "desktop_data_clear_start_blocked"

    second_preview = owner.preview(stop_runtime=None, timeout=10)
    assert second_preview.status == "CONFIRMATION_REQUIRED"
    with pytest.raises(DesktopOperationError) as old_capability:
        owner.finalize(_finalize_request(first_apply.details["operationId"], first_apply.details["finalizerCapability"]), ready=lambda *_: None)
    assert old_capability.value.reason_code == "desktop_data_clear_finalizer_replayed"
    second_apply = owner.apply(_apply_request(second_preview.details["previewId"]))
    result = owner.finalize(_finalize_request(second_apply.details["operationId"], second_apply.details["finalizerCapability"]), ready=lambda *_: None)
    assert result.status == "CLEARED"
    assert not profile.exists()


def test_active_exact_finalizer_cannot_be_replaced_by_repreview(tmp_path: Path) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    owner, _resource, _profile, _data_root = _fixture(tmp_path, vault=FakeVault(), inspector=SequencedInspector([electron]))
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    applied = owner.apply(_apply_request(preview.details["previewId"]))
    journal = json.loads(owner.paths.journal.read_text(encoding="utf-8"))
    journal.update({
        "revision": journal["revision"] + 1,
        "state": "FINALIZING",
        "finalizerProcess": {"pid": 88, "creationTime": "123", "executable": "C:/Python/python.exe"},
    })
    atomic_write_json(owner.paths.journal, journal)
    owner.process_inspector = SequencedInspector([(123, "C:/Python/python.exe")])
    with pytest.raises(DesktopOperationError) as blocked:
        owner.preview(stop_runtime=None, timeout=10)
    assert blocked.value.reason_code == "desktop_data_clear_start_blocked"
    assert owner.status().status == "FINALIZING"
    assert applied.status == "PREPARED"


def test_normal_writer_lease_invalidates_preview_before_apply_can_prepare(tmp_path: Path) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    owner, _resource, _profile, _data_root = _fixture(tmp_path, vault=FakeVault(), inspector=SequencedInspector([electron]))
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    writer_entered = threading.Event()
    release_writer = threading.Event()
    apply_started = threading.Event()
    outcome: list[object] = []

    def writer() -> None:
        with owner.normal_command_guard(writer=True):
            writer_entered.set()
            release_writer.wait(timeout=5)

    def apply() -> None:
        apply_started.set()
        try:
            outcome.append(owner.apply(_apply_request(preview.details["previewId"])))
        except Exception as exc:
            outcome.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert writer_entered.wait(timeout=5)
    apply_thread = threading.Thread(target=apply)
    apply_thread.start()
    assert apply_started.wait(timeout=5)
    release_writer.set()
    writer_thread.join(timeout=5)
    apply_thread.join(timeout=5)
    assert len(outcome) == 1
    assert isinstance(outcome[0], DesktopOperationError)
    assert outcome[0].reason_code == "desktop_data_clear_preview_replayed"
    assert owner.status().status == "IDLE"


class ReadOnlyRuntime:
    def __init__(self, config) -> None:
        self.config = config

    def status(self):
        return OperationResult(True, "status", "STOPPED", "desktop_stopped", "stopped", {})


def test_preview_then_readonly_cli_status_does_not_consume_preview(tmp_path: Path, capfd) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    owner, resource, _profile, data_root = _fixture(tmp_path, vault=FakeVault(), inspector=SequencedInspector([electron]))
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    args = ["--json", "--resource-root", str(resource), "--data-root", str(data_root), "status"]
    assert cli_main(args, runtime_factory=ReadOnlyRuntime) == 0
    capfd.readouterr()
    applied = owner.apply(_apply_request(preview.details["previewId"]))
    assert applied.status == "PREPARED"


def test_clear_root_rejects_relative_device_namespace_ads_and_reserved_components(tmp_path: Path) -> None:
    resource = tmp_path / "resources"
    resource.mkdir()
    with pytest.raises(DesktopOperationError):
        OwnedDataClearOwner(resource_root=resource, data_root=Path("relative") / "runtime")
    if os.name == "nt":
        with pytest.raises(DesktopOperationError):
            OwnedDataClearOwner(resource_root=resource, data_root=r"\\?\C:\unsafe\runtime")
        with pytest.raises(DesktopOperationError):
            OwnedDataClearOwner(resource_root=resource, data_root=str(tmp_path / "userData:alias" / "runtime"))
        with pytest.raises(DesktopOperationError):
            OwnedDataClearOwner(resource_root=resource, data_root=str(tmp_path / "CON" / "runtime"))


def test_nonfinite_deadlines_and_wait_budgets_fail_closed(tmp_path: Path) -> None:
    owner, _resource, _profile, _data_root = _fixture(tmp_path, vault=FakeVault())
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    journal = json.loads(owner.paths.journal.read_text(encoding="utf-8"))
    for field in ("expiresEpoch", "finalizerExpiresEpoch"):
        for bad in (float("nan"), float("inf"), -1.0, 253402300800.0):
            corrupted = dict(journal)
            corrupted[field] = bad
            atomic_write_json(owner.paths.journal, corrupted)
            with pytest.raises(DesktopOperationError) as error:
                owner.status()
            assert error.value.reason_code == "desktop_data_clear_journal_invalid"
    for bad in (float("nan"), float("inf"), -1.0, 1000.0):
        with pytest.raises(ValueError):
            OwnedDataClearOwner(resource_root=owner.paths.resource_root, data_root=owner.paths.runtime_root, finalizer_wait_seconds=bad)


def test_finalizer_cleanup_remains_authoritative_after_ready_pipe_break(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    inspector = SequencedInspector([electron, electron, (123456, "C:/Python/python.exe"), None])
    owner, resource, profile, data_root = _fixture(tmp_path, vault=FakeVault(), inspector=inspector)
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    applied = owner.apply(_apply_request(preview.details["previewId"]))
    request = json.dumps(_finalize_request(applied.details["operationId"], applied.details["finalizerCapability"])).encode("utf-8")

    class BrokenAfterReady:
        def __init__(self) -> None:
            self.writes = 0
        def write(self, value: bytes) -> int:
            self.writes += 1
            if self.writes > 1:
                raise BrokenPipeError(32, "parent pipe closed")
            return len(value)
        def flush(self) -> None:
            return None

    output = BrokenAfterReady()
    monkeypatch.setattr(cli_module.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request)))
    monkeypatch.setattr(cli_module.sys, "stdout", SimpleNamespace(buffer=output))
    args = ["--json", "--resource-root", str(resource), "--data-root", str(data_root), "data-clear-finalize"]
    assert cli_main(args, clear_owner_factory=lambda **_: owner) == 0
    assert output.writes == 2
    assert not profile.exists()
    assert owner.status().status == "CLEARED"


def _clear_cli_args(resource: Path, data_root: Path, command: str) -> list[str]:
    return ["--json", "--resource-root", str(resource), "--data-root", str(data_root), command]


def test_packaged_clear_rejects_wrong_interpreter_before_owner_or_data_initialization(tmp_path: Path, capfd) -> None:
    resource = tmp_path / "resources"
    resource.mkdir()
    (resource / "desktop-bundle.json").write_text("{}", encoding="utf-8")
    data_root = tmp_path / "userData" / "runtime"
    constructed = []
    code = cli_main(_clear_cli_args(resource, data_root, "data-clear-status"), clear_owner_factory=lambda **kwargs: constructed.append(kwargs))
    payload = json.loads(capfd.readouterr().out)
    assert code != 0
    assert payload["code"] == "desktop_app_wrong_python"
    assert constructed == []
    assert not data_root.parent.exists()


def test_packaged_clear_rejects_pythonpath_and_user_site_injection(tmp_path: Path, capfd, monkeypatch: pytest.MonkeyPatch) -> None:
    resource = tmp_path / "resources"
    expected = resource / ".venv" / "Scripts" / "python.exe"
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"python")
    (resource / "desktop-bundle.json").write_text("{}", encoding="utf-8")
    data_root = tmp_path / "userData" / "runtime"
    monkeypatch.setattr(cli_module.sys, "executable", str(expected))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "injected"))
    code = cli_main(_clear_cli_args(resource, data_root, "data-clear-apply"), clear_owner_factory=lambda **_: pytest.fail("owner must not be constructed"))
    payload = json.loads(capfd.readouterr().out)
    assert code != 0
    assert payload["code"] == "desktop_app_python_environment_unsafe"

    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.setattr(cli_module.sys, "flags", SimpleNamespace(no_user_site=0))
    code = cli_main(_clear_cli_args(resource, data_root, "data-clear-finalize"), clear_owner_factory=lambda **_: pytest.fail("owner must not be constructed"))
    payload = json.loads(capfd.readouterr().out)
    assert code != 0
    assert payload["code"] == "desktop_app_python_user_site_enabled"


class BlockingConfigRuntime:
    entered = threading.Event()
    release = threading.Event()

    def __init__(self, config) -> None:
        self.config = config

    def config_apply(self, **kwargs):
        self.__class__.entered.set()
        self.__class__.release.wait(timeout=5)
        return OperationResult(True, "config-apply", "APPLIED", "desktop_runtime_config_applied", "applied", {})


def test_cli_config_apply_lease_cannot_continue_after_clear_prepare(tmp_path: Path, capfd) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    owner, resource, _profile, data_root = _fixture(tmp_path, vault=FakeVault(), inspector=SequencedInspector([electron]))
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    BlockingConfigRuntime.entered = threading.Event()
    BlockingConfigRuntime.release = threading.Event()
    candidate = {"research": {"llm": None, "source": {"provider": "arxiv", "api_url": "https://export.arxiv.org/api/query", "timeout_seconds": 30}}}
    args = [
        "--json", "--resource-root", str(resource), "--data-root", str(data_root), "config-apply",
        "--candidate-json", json.dumps(candidate), "--expected-revision", "0",
        "--confirmation-token", "desktop-config:" + "a" * 32 + ":" + "b" * 16,
    ]
    cli_result: list[int] = []
    apply_result: list[object] = []

    def run_writer() -> None:
        cli_result.append(cli_main(args, runtime_factory=BlockingConfigRuntime, clear_owner_factory=lambda **_: owner))

    def run_apply() -> None:
        try:
            apply_result.append(owner.apply(_apply_request(preview.details["previewId"])))
        except Exception as exc:
            apply_result.append(exc)

    writer = threading.Thread(target=run_writer)
    writer.start()
    assert BlockingConfigRuntime.entered.wait(timeout=5)
    clearer = threading.Thread(target=run_apply)
    clearer.start()
    BlockingConfigRuntime.release.set()
    writer.join(timeout=5)
    clearer.join(timeout=5)
    capfd.readouterr()
    assert cli_result == [0]
    assert len(apply_result) == 1
    assert isinstance(apply_result[0], DesktopOperationError)
    assert apply_result[0].reason_code == "desktop_data_clear_preview_replayed"
    assert owner.status().status == "IDLE"


def test_profile_reparse_point_is_rejected_without_touching_target(tmp_path: Path) -> None:
    owner, _resource, profile, _data_root = _fixture(tmp_path, vault=FakeVault())
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    link = profile / "linked-outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink fixture unavailable: {exc}")

    with pytest.raises(DesktopOperationError) as error:
        owner.preview(stop_runtime=_stop, timeout=10)

    assert error.value.reason_code == "desktop_data_clear_reparse_rejected"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert outside.is_dir()


def test_failed_partial_retry_allows_missing_runtime_and_marker_only_after_reconfirmation(tmp_path: Path) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    finalizer = (123456, "C:/Python/python.exe")
    inspector = SequencedInspector([electron, electron, electron, finalizer, None])
    owner, _resource, profile, data_root = _fixture(tmp_path, vault=FakeVault(), inspector=inspector)
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    prepared = owner.apply(_apply_request(preview.details["previewId"]))

    shutil.rmtree(data_root)
    (profile / MARKER_NAME).unlink()

    retried = owner.preview(stop_runtime=None, timeout=10)
    assert retried.status == "CONFIRMATION_REQUIRED"
    prepared_again = owner.apply(_apply_request(retried.details["previewId"]))
    finalized = owner.finalize(
        _finalize_request(prepared_again.details["operationId"], prepared_again.details["finalizerCapability"]),
        ready=lambda _operation, _nonce: None,
    )

    assert prepared_again.details["operationId"] != prepared.details["operationId"]
    assert finalized.status == "CLEARED"
    assert not profile.exists()



def test_retry_preview_remains_maintenance_blocking_and_preserves_original_retry(tmp_path: Path, capfd) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    owner, resource, _profile, data_root = _fixture(tmp_path, vault=FakeVault(), inspector=SequencedInspector([electron, electron]))
    first = owner.preview(stop_runtime=_stop, timeout=10)
    prepared = owner.apply(_apply_request(first.details["previewId"]))
    retry = owner.preview(stop_runtime=None, timeout=10)
    before_status = owner.paths.journal.read_bytes()

    status = owner.status()
    assert status.status == "FAILED_PARTIAL"
    assert status.details["operationId"] == prepared.details["operationId"]
    assert owner.paths.journal.read_bytes() == before_status
    with pytest.raises(DesktopOperationError) as fenced:
        fence_normal_initialization(data_root)
    assert fenced.value.reason_code == "desktop_data_clear_start_blocked"

    fail_runtime = lambda _config: pytest.fail("blocked maintenance command must not construct a runtime")
    common = ["--json", "--resource-root", str(resource), "--data-root", str(data_root)]
    assert cli_main([*common, "start"], runtime_factory=fail_runtime, clear_owner_factory=lambda **_: owner) != 0
    start_payload = json.loads(capfd.readouterr().out)
    assert start_payload["code"] == "desktop_data_clear_start_blocked"
    candidate = {"research": {"llm": None, "source": {"provider": "arxiv", "api_url": "https://export.arxiv.org/api/query", "timeout_seconds": 30}}}
    config_args = [
        *common, "config-apply", "--candidate-json", json.dumps(candidate), "--expected-revision", "0",
        "--confirmation-token", "desktop-config:" + "a" * 32 + ":" + "b" * 16,
    ]
    assert cli_main(config_args, runtime_factory=fail_runtime, clear_owner_factory=lambda **_: owner) != 0
    config_payload = json.loads(capfd.readouterr().out)
    assert config_payload["code"] == "desktop_data_clear_start_blocked"

    clear_preview_args = [*common, "data-clear-preview"]
    assert cli_main(clear_preview_args, runtime_factory=fail_runtime, clear_owner_factory=lambda **_: owner) == 0
    repeated_payload = json.loads(capfd.readouterr().out)
    journal = json.loads(owner.paths.journal.read_text(encoding="utf-8"))
    assert repeated_payload["state"] == "CONFIRMATION_REQUIRED"
    assert isinstance(repeated_payload["data"]["previewId"], str)
    assert journal["retryOf"] == prepared.details["operationId"]
    assert owner.status().status == "FAILED_PARTIAL"


def test_delete_collection_root_replacement_never_touches_external_sentinel_and_is_durable_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    finalizer = (123456, "C:/Python/python.exe")
    inspector = SequencedInspector([electron, electron, finalizer, None])
    owner, _resource, profile, _data_root = _fixture(tmp_path, vault=FakeVault(), inspector=inspector)
    (profile / "Cache").mkdir()
    (profile / "Cache" / "entry").write_bytes(b"owned")
    sentinel = tmp_path / "external-sentinel.bin"
    sentinel.write_bytes(b"outside")
    sentinel.chmod(stat.S_IREAD)
    expected_mode = sentinel.stat().st_mode
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    prepared = owner.apply(_apply_request(preview.details["previewId"]))
    original_delete = owned_clear_module._delete_collected_entry
    injected = False

    def replace_root_then_delete(root, root_identity, directory_records, record, *, deadline):
        nonlocal injected
        if not injected:
            injected = True
            moved = root.with_name(root.name + "-moved-during-delete")
            root.rename(moved)
            root.mkdir()
        return original_delete(root, root_identity, directory_records, record, deadline=deadline)

    monkeypatch.setattr(owned_clear_module, "_delete_collected_entry", replace_root_then_delete)
    try:
        with pytest.raises(DesktopOperationError):
            owner.finalize(
                _finalize_request(prepared.details["operationId"], prepared.details["finalizerCapability"]),
                ready=lambda *_: None,
            )
        assert sentinel.read_bytes() == b"outside"
        assert sentinel.stat().st_mode == expected_mode
        assert owner.status().status == "FAILED_PARTIAL"
    finally:
        sentinel.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_scan_deadline_failure_records_durable_partial_without_deleting_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    finalizer = (123456, "C:/Python/python.exe")
    inspector = SequencedInspector([electron, electron, finalizer, None])
    owner, _resource, profile, _data_root = _fixture(tmp_path, vault=FakeVault(), inspector=inspector)
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    prepared = owner.apply(_apply_request(preview.details["previewId"]))
    clock = [100.0]
    original_scan = owned_clear_module._scan_tree
    monkeypatch.setattr(owned_clear_module.time, "monotonic", lambda: clock[0])

    def expired_scan(root, *, deadline=None):
        assert deadline == 100.0 + owned_clear_module.FINALIZER_DELETE_SECONDS
        clock[0] = deadline + 1.0
        return original_scan(root, deadline=deadline)

    monkeypatch.setattr(owned_clear_module, "_scan_tree", expired_scan)
    with pytest.raises(DesktopOperationError) as error:
        owner.finalize(
            _finalize_request(prepared.details["operationId"], prepared.details["finalizerCapability"]),
            ready=lambda *_: None,
        )

    assert error.value.reason_code == "desktop_data_clear_scan_timeout"
    assert profile.exists()
    assert owner.status().status == "FAILED_PARTIAL"


def test_delete_loop_deadline_expires_before_first_destructive_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "deadline-owned-profile"
    root.mkdir()
    clock = [100.0]
    original_delete = owned_clear_module._delete_collected_entry
    monkeypatch.setattr(owned_clear_module.time, "monotonic", lambda: clock[0])

    def expire_before_first_delete(root, root_identity, directory_records, record, *, deadline):
        assert deadline == 100.0 + owned_clear_module.FINALIZER_DELETE_SECONDS
        clock[0] = deadline + 1.0
        return original_delete(root, root_identity, directory_records, record, deadline=deadline)

    monkeypatch.setattr(owned_clear_module, "_delete_collected_entry", expire_before_first_delete)
    with pytest.raises(DesktopOperationError) as error:
        owned_clear_module._delete_tree(root)

    assert error.value.reason_code == "desktop_data_clear_delete_timeout"
    assert root.is_dir()



def _assert_private_fixture_path(path: Path, fixture_root: Path) -> None:
    assert fixture_root.is_absolute()
    assert path.is_absolute()
    assert ".." not in path.parts
    assert path != fixture_root
    assert path.is_relative_to(fixture_root)


def _create_private_windows_junction(junction: Path, target: Path, *, fixture_root: Path) -> None:
    """Create only a contained fixture junction; never use the shell for cleanup."""
    _assert_private_fixture_path(junction, fixture_root)
    _assert_private_fixture_path(target, fixture_root)
    assert not os.path.lexists(junction)
    assert target.is_dir()
    for value in (os.fspath(junction), os.fspath(target)):
        assert not any(character in value for character in '&|<>^%!"\r\n')
    comspec = Path(os.environ["COMSPEC"])
    assert comspec.is_absolute() and comspec.name.casefold() == "cmd.exe"
    completed = subprocess.run(
        [str(comspec), "/d", "/c", "mklink", "/J", str(junction), str(target)],
        shell=False,
        capture_output=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    metadata = junction.lstat()
    assert metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    assert metadata.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT


def _remove_private_windows_junction(junction: Path, *, fixture_root: Path) -> None:
    _assert_private_fixture_path(junction, fixture_root)
    if not os.path.lexists(junction):
        return
    metadata = junction.lstat()
    assert metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    assert metadata.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    os.rmdir(junction)
    assert not os.path.lexists(junction)


def _sentinel_snapshot(path: Path) -> tuple[bytes, int, int]:
    metadata = path.stat()
    return path.read_bytes(), metadata.st_mode, metadata.st_file_attributes


@pytest.mark.skipif(os.name != "nt", reason="real directory junction requires Windows")
def test_windows_real_junction_is_rejected_before_clear_without_changing_external_sentinel(tmp_path: Path) -> None:
    owner, _resource, profile, _data_root = _fixture(tmp_path, vault=FakeVault())
    outside = tmp_path / "external-junction-target"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"external-junction-sentinel")
    sentinel.chmod(stat.S_IREAD)
    expected = _sentinel_snapshot(sentinel)
    junction = profile / "Cache"
    try:
        _create_private_windows_junction(junction, outside, fixture_root=tmp_path)
        with pytest.raises(DesktopOperationError) as error:
            owner.preview(stop_runtime=_stop, timeout=10)

        assert error.value.reason_code == "desktop_data_clear_reparse_rejected"
        assert _sentinel_snapshot(sentinel) == expected
        assert profile.is_dir()
        assert (profile / MARKER_NAME).is_file()
        assert owner.status().status == "IDLE"
    finally:
        _remove_private_windows_junction(junction, fixture_root=tmp_path)
        sentinel.chmod(stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="real directory junction requires Windows")
@pytest.mark.parametrize("replacement", ["ancestor-directory", "profile-root"])
def test_windows_real_junction_injected_after_inventory_preserves_external_bytes_and_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str,
) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    finalizer = (123456, "C:/Python/python.exe")
    inspector = SequencedInspector([electron, electron, finalizer, None])
    owner, _resource, profile, _data_root = _fixture(tmp_path, vault=FakeVault(), inspector=inspector)
    cache = profile / "Cache"
    cache.mkdir()
    (cache / "sentinel.bin").write_bytes(b"owned-cache")
    outside = tmp_path / "external-injected-junction-target"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"external-injected-junction-sentinel")
    sentinel.chmod(stat.S_IREAD)
    expected = _sentinel_snapshot(sentinel)
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    prepared = owner.apply(_apply_request(preview.details["previewId"]))
    original_delete = owned_clear_module._delete_collected_entry
    junction = profile if replacement == "profile-root" else cache
    injected = False

    def inject_junction_before_first_delete(root, root_identity, directory_records, record, *, deadline):
        nonlocal injected
        if not injected:
            injected = True
            moved = junction.with_name(junction.name + "-verified-original")
            _assert_private_fixture_path(moved, tmp_path)
            junction.rename(moved)
            _create_private_windows_junction(junction, outside, fixture_root=tmp_path)
        return original_delete(root, root_identity, directory_records, record, deadline=deadline)

    monkeypatch.setattr(owned_clear_module, "_delete_collected_entry", inject_junction_before_first_delete)
    try:
        with pytest.raises(DesktopOperationError) as error:
            owner.finalize(
                _finalize_request(prepared.details["operationId"], prepared.details["finalizerCapability"]),
                ready=lambda *_: None,
            )

        assert injected
        assert error.value.reason_code == "desktop_reparse_point_rejected"
        assert _sentinel_snapshot(sentinel) == expected
        journal = json.loads(owner.paths.journal.read_text(encoding="utf-8"))
        assert journal["state"] == "FAILED_PARTIAL"
        assert journal["failure"]["code"] == "desktop_reparse_point_rejected"
        assert not owner.paths.receipt.exists()
    finally:
        _remove_private_windows_junction(junction, fixture_root=tmp_path)
        sentinel.chmod(stat.S_IREAD | stat.S_IWRITE)


_WINDOWS_NO_DELETE_SHARE_HOLDER = r"""
import ctypes
import sys
import threading
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL
handle = kernel32.CreateFileW(sys.argv[1], 0x80000000, 0x00000001 | 0x00000002, None, 3, 0x80, None)
if handle in (None, 0, ctypes.c_void_p(-1).value):
    raise ctypes.WinError(ctypes.get_last_error())
try:
    released = threading.Event()
    def await_release():
        sys.stdin.buffer.read(1)
        released.set()
    threading.Thread(target=await_release, daemon=True).start()
    print("LOCK_READY", flush=True)
    if not released.wait(timeout=20):
        raise TimeoutError("bounded lock fixture was not released")
finally:
    kernel32.CloseHandle(handle)
"""


@contextmanager
def _private_windows_no_delete_share_handle(path: Path, *, fixture_root: Path):
    _assert_private_fixture_path(path, fixture_root)
    assert path.is_file()
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-u", "-c", _WINDOWS_NO_DELETE_SHARE_HOLDER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=fixture_root,
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    ready_lines = queue.Queue()
    reader = threading.Thread(target=lambda: ready_lines.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert ready_lines.get(timeout=5) == b"LOCK_READY\r\n"
        reader.join(timeout=1)
        assert not reader.is_alive()
        assert process.poll() is None
        yield process
    finally:
        try:
            output, errors = process.communicate(input=b"\n", timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                output, errors = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, errors = process.communicate(timeout=5)
            pytest.fail(f"exact lock fixture subprocess did not release within its timeout: {output!r} {errors!r}")
        reader.join(timeout=1)
        assert process.returncode == 0, (output, errors)


@pytest.mark.skipif(os.name != "nt", reason="nonshared delete file handle requires Windows")
def test_windows_external_no_delete_share_handle_requires_partial_retry_and_explicit_confirmation(tmp_path: Path) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    finalizer = (123456, "C:/Python/python.exe")
    inspector = SequencedInspector([electron, electron, finalizer, None, electron, electron, finalizer, None])
    vault = FakeVault()
    owner, resource, profile, data_root = _fixture(tmp_path, vault=vault, inspector=inspector)
    locked_file = data_root / "locked-business.bin"
    locked_file.write_bytes(b"owned-locked-business")
    external = tmp_path / "external-lock-case-sentinel.bin"
    external.write_bytes(b"keep-external")
    expected_external = _sentinel_snapshot(external)
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    prepared = owner.apply(_apply_request(preview.details["previewId"]))

    with _private_windows_no_delete_share_handle(locked_file, fixture_root=tmp_path) as holder:
        assert holder.pid != os.getpid()
        with pytest.raises(DesktopOperationError) as error:
            owner.finalize(
                _finalize_request(prepared.details["operationId"], prepared.details["finalizerCapability"]),
                ready=lambda *_: None,
            )

        assert error.value.reason_code == "desktop_data_clear_delete_failed"
        assert error.value.details["errno"] == 32
        assert holder.poll() is None
        assert owner.status().status == "FAILED_PARTIAL"
        journal = json.loads(owner.paths.journal.read_text(encoding="utf-8"))
        assert journal["state"] == "FAILED_PARTIAL"
        assert journal["failure"]["code"] == "desktop_data_clear_delete_failed"
        assert locked_file.read_bytes() == b"owned-locked-business"
        assert not owner.paths.receipt.exists()
        with pytest.raises(DesktopOperationError) as fenced:
            fence_normal_initialization(data_root)
        assert fenced.value.reason_code == "desktop_data_clear_start_blocked"

    resumed = OwnedDataClearOwner(
        resource_root=resource, data_root=data_root, credential_vault=vault,
        process_inspector=inspector, now=lambda: 1000.0, sleep=lambda _: None,
        finalizer_wait_seconds=1,
    )
    assert resumed.status().status == "FAILED_PARTIAL"
    retry = resumed.preview(stop_runtime=None, timeout=10)
    assert retry.status == "CONFIRMATION_REQUIRED"
    assert resumed.status().status == "FAILED_PARTIAL"
    with pytest.raises(DesktopOperationError) as wrong_phrase:
        resumed.apply(_apply_request(retry.details["previewId"], phrase="wrong"))
    assert wrong_phrase.value.reason_code == "desktop_data_clear_confirmation_mismatch"
    assert locked_file.read_bytes() == b"owned-locked-business"
    retried = resumed.apply(_apply_request(retry.details["previewId"]))
    assert retried.details["operationId"] != prepared.details["operationId"]
    completed = resumed.finalize(
        _finalize_request(retried.details["operationId"], retried.details["finalizerCapability"]),
        ready=lambda *_: None,
    )

    assert completed.status == "CLEARED"
    assert resumed.status().status == "CLEARED"
    assert not profile.exists()
    assert _sentinel_snapshot(external) == expected_external
    assert vault.present is False
    assert vault.reads == 0



def _write_real_scale_fixture_manifest(path: Path) -> str:
    """Write a valid, private fixture at the reported packaged manifest size."""
    size = 16_174_784
    prefix = b'{"resources":[],"padding":"'
    suffix = b'"}\n'
    remaining = size - len(prefix) - len(suffix)
    digest = hashlib.sha256()
    with path.open("wb") as handle:
        handle.write(prefix)
        digest.update(prefix)
        chunk = b"a" * (256 * 1024)
        while remaining:
            content = chunk[:min(remaining, len(chunk))]
            handle.write(content)
            digest.update(content)
            remaining -= len(content)
        handle.write(suffix)
        digest.update(suffix)
    assert path.stat().st_size == size
    return digest.hexdigest()


class _TrackedManifestReader:
    def __init__(self, handle, reads: list[int]) -> None:
        self.handle = handle
        self.reads = reads

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def fileno(self):
        return self.handle.fileno()

    def read(self, size: int = -1):
        assert 0 < size <= owned_clear_module.RELEASE_MANIFEST_CHUNK_BYTES
        self.reads.append(size)
        return self.handle.read(size)


def _track_owned_manifest_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    reads: list[int] = []

    def tracked_open(path, mode="r", *args, **kwargs):
        handle = builtins.open(path, mode, *args, **kwargs)
        if mode == "rb" and os.fspath(path).casefold().endswith("desktop-bundle.json"):
            return _TrackedManifestReader(handle, reads)
        return handle

    monkeypatch.setattr(owned_clear_module, "open", tracked_open, raising=False)
    return reads


def test_real_scale_manifest_streams_complete_identity_through_register_preview_apply_finalize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resource = tmp_path / "resources"
    resource.mkdir()
    manifest = resource / "desktop-bundle.json"
    expected_digest = _write_real_scale_fixture_manifest(manifest)
    reads = _track_owned_manifest_reads(monkeypatch)
    profile = tmp_path / "userData"
    electron = (90071992547409931, "C:/Agora.exe")
    finalizer = (123456, "C:/Python/python.exe")
    vault = FakeVault()
    owner = OwnedDataClearOwner(
        resource_root=resource, data_root=profile / "runtime", credential_vault=vault,
        process_inspector=SequencedInspector([electron, electron, finalizer, None]),
        now=lambda: 1000.0, sleep=lambda _: None, finalizer_wait_seconds=1,
    )

    assert owner.register_for_start() is True
    marker = json.loads(owner.paths.marker.read_text(encoding="utf-8"))
    assert marker["releaseBinding"]["bundleManifestSha256"] == expected_digest
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    prepared = owner.apply(_apply_request(preview.details["previewId"]))
    assert prepared.status == "PREPARED"
    completed = owner.finalize(
        _finalize_request(prepared.details["operationId"], prepared.details["finalizerCapability"]),
        ready=lambda *_: None,
    )

    assert completed.status == "CLEARED"
    assert not profile.exists()
    assert manifest.stat().st_size == 16_174_784
    assert owned_clear_module._release_binding(resource)["bundleManifestSha256"] == expected_digest
    assert len(reads) > 64
    assert max(reads) == owned_clear_module.RELEASE_MANIFEST_CHUNK_BYTES
    assert vault.reads == 0


def test_real_scale_manifest_tail_change_after_preview_is_bound_by_full_streaming_cas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resource = tmp_path / "resources"
    resource.mkdir()
    manifest = resource / "desktop-bundle.json"
    initial_digest = _write_real_scale_fixture_manifest(manifest)
    _track_owned_manifest_reads(monkeypatch)
    profile = tmp_path / "userData"
    owner = OwnedDataClearOwner(resource_root=resource, data_root=profile / "runtime", credential_vault=FakeVault(), now=lambda: 1000.0)
    assert owner.register_for_start() is True
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    before_journal = owner.paths.journal.read_bytes()
    with manifest.open("r+b") as handle:
        handle.seek(-5, os.SEEK_END)
        assert handle.read(1) == b"a"
        handle.seek(-1, os.SEEK_CUR)
        handle.write(b"b")

    assert owned_clear_module._release_binding(resource)["bundleManifestSha256"] != initial_digest
    with pytest.raises(DesktopOperationError) as error:
        owner.apply(_apply_request(preview.details["previewId"]))

    assert error.value.reason_code == "desktop_data_clear_release_mismatch"
    assert owner.paths.journal.read_bytes() == before_journal
    assert profile.is_dir()
    assert not owner.paths.receipt.exists()


def test_manifest_over_streaming_limit_is_rejected_from_metadata_without_opening_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resource = tmp_path / "resources"
    resource.mkdir()
    manifest = resource / "desktop-bundle.json"
    with manifest.open("wb") as handle:
        handle.truncate(owned_clear_module.RELEASE_MANIFEST_MAX_BYTES + 1)
    owner = OwnedDataClearOwner(resource_root=resource, data_root=tmp_path / "userData" / "runtime", credential_vault=FakeVault())
    monkeypatch.setattr(owned_clear_module, "open", lambda *_args, **_kwargs: pytest.fail("oversized manifest must not be read"), raising=False)

    with pytest.raises(DesktopOperationError) as error:
        owner.register_for_start()

    assert error.value.reason_code == "desktop_data_clear_release_invalid"
    assert not owner.paths.marker.exists()
    assert not owner.paths.receipt.exists()


def test_manifest_streaming_deadline_is_finite_and_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resource = tmp_path / "resources"
    resource.mkdir()
    manifest = resource / "desktop-bundle.json"
    manifest.write_bytes(b"{}")
    ticks = iter((100.0, 100.0, 100.0 + owned_clear_module.RELEASE_MANIFEST_HASH_SECONDS + 1.0))
    monkeypatch.setattr(owned_clear_module.time, "monotonic", lambda: next(ticks, 1000.0))

    with pytest.raises(DesktopOperationError) as error:
        owned_clear_module._release_binding(resource)

    assert error.value.reason_code == "desktop_data_clear_release_timeout"
    assert manifest.read_bytes() == b"{}"


@pytest.mark.parametrize("version", [True, False, 1.0, "1", None, 2])
@pytest.mark.parametrize("request_kind", ["apply", "finalize"])
def test_clear_protocol_version_requires_exact_integer_one(version, request_kind: str) -> None:
    if request_kind == "apply":
        request = _apply_request("p" * 43)
        parse = owned_clear_module._parse_apply
    else:
        request = _finalize_request("a" * 32, "c" * 64)
        parse = owned_clear_module._parse_finalize
    request["version"] = version

    with pytest.raises(DesktopOperationError) as error:
        parse(request)

    assert error.value.reason_code == "desktop_data_clear_protocol_version_invalid"



def test_finalizer_prescan_and_delete_share_one_finite_total_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    electron = (90071992547409931, "C:/Agora.exe")
    finalizer = (123456, "C:/Python/python.exe")
    owner, _resource, profile, _data_root = _fixture(
        tmp_path, vault=FakeVault(), inspector=SequencedInspector([electron, electron, finalizer, None]),
    )
    preview = owner.preview(stop_runtime=_stop, timeout=10)
    prepared = owner.apply(_apply_request(preview.details["previewId"]))
    clock = [100.0]
    deadlines: list[float] = []
    original_scan = owned_clear_module._scan_tree
    original_delete = owned_clear_module._delete_collected_entry
    monkeypatch.setattr(owned_clear_module.time, "monotonic", lambda: clock[0])

    def consume_scan_budget(root, *, deadline=None):
        assert deadline is not None
        deadlines.append(deadline)
        clock[0] += owned_clear_module.FINALIZER_DELETE_SECONDS * 0.4
        return original_scan(root, deadline=deadline)

    def expire_in_delete(root, root_identity, directory_records, record, *, deadline):
        deadlines.append(deadline)
        clock[0] += owned_clear_module.FINALIZER_DELETE_SECONDS * 0.2 + 1.0
        return original_delete(root, root_identity, directory_records, record, deadline=deadline)

    monkeypatch.setattr(owned_clear_module, "_scan_tree", consume_scan_budget)
    monkeypatch.setattr(owned_clear_module, "_delete_collected_entry", expire_in_delete)
    with pytest.raises(DesktopOperationError) as error:
        owner.finalize(
            _finalize_request(prepared.details["operationId"], prepared.details["finalizerCapability"]),
            ready=lambda *_: None,
        )

    assert deadlines == [100.0 + owned_clear_module.FINALIZER_DELETE_SECONDS] * 3
    assert error.value.reason_code == "desktop_data_clear_delete_timeout"
    assert profile.exists()
    assert owner.status().status == "FAILED_PARTIAL"
    assert not owner.paths.receipt.exists()
    assert owned_clear_module.FINALIZER_WAIT_SECONDS + owned_clear_module.FINALIZER_DELETE_SECONDS < owned_clear_module.FINALIZER_TTL_SECONDS


def test_standalone_scan_has_its_own_finite_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "standalone-scan-profile"
    root.mkdir()
    clock = [100.0]
    monkeypatch.setattr(owned_clear_module.time, "monotonic", lambda: clock[0])
    original_check = owned_clear_module._check_operation_deadline
    checked: list[float] = []

    def expire_scan_budget(deadline, code):
        checked.append(deadline)
        clock[0] = deadline + 1.0
        return original_check(deadline, code)

    monkeypatch.setattr(owned_clear_module, "_check_operation_deadline", expire_scan_budget)
    with pytest.raises(DesktopOperationError) as error:
        owned_clear_module._scan_tree(root)

    assert checked == [100.0 + owned_clear_module.TREE_OPERATION_SECONDS]
    assert error.value.reason_code == "desktop_data_clear_scan_timeout"
    assert root.is_dir()
