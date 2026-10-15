from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import fields
from pathlib import Path

import pytest

from scripts.desktop_app.credentials import (
    APPLICATION_ID,
    FIXED_CREDENTIAL_ENV,
    FIXED_CREDENTIAL_REF,
    AppCredentialProvider,
    CredentialDescriptor,
    CredentialError,
    CredentialPresence,
    CredentialPromptResult,
    RESEARCH_CREDENTIAL_SLOT,
    WindowsCredentialVault,
    bind_launch_environment,
    delete_slot,
    descriptor_for,
    inspect_slot,
    read_slot_for_owner,
    replace_slot_with_native_prompt,
)


class FakeVault:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.inspected: list[CredentialDescriptor] = []
        self.reads: list[CredentialDescriptor] = []
        self.deletes: list[CredentialDescriptor] = []
        self.prompts: list[tuple[CredentialDescriptor, int]] = []

    def inspect(self, descriptor: CredentialDescriptor) -> CredentialPresence:
        self.inspected.append(descriptor)
        return CredentialPresence(descriptor.target_name in self.values)

    def read(self, descriptor: CredentialDescriptor) -> str | None:
        self.reads.append(descriptor)
        return self.values.get(descriptor.target_name)

    def write(self, descriptor: CredentialDescriptor, secret: str) -> None:
        self.values[descriptor.target_name] = secret

    def delete(self, descriptor: CredentialDescriptor) -> bool:
        self.deletes.append(descriptor)
        return self.values.pop(descriptor.target_name, None) is not None

    def prompt_and_replace(self, descriptor: CredentialDescriptor, *, parent_window: int = 0) -> CredentialPromptResult:
        self.prompts.append((descriptor, parent_window))
        self.values[descriptor.target_name] = "fixture-secret-never-projected"
        return CredentialPromptResult(changed=True, present=True, cancelled=False)


def test_descriptor_is_the_single_fixed_research_binding_in_app_and_lexical_root_namespace(tmp_path: Path) -> None:
    data_root = tmp_path / "user-data" / "runtime"
    descriptor = descriptor_for(data_root)

    assert descriptor.slot is RESEARCH_CREDENTIAL_SLOT
    assert descriptor.credential_ref == FIXED_CREDENTIAL_REF
    assert descriptor.environment_name == FIXED_CREDENTIAL_ENV
    assert descriptor.target_name.startswith(f"{APPLICATION_ID}/v1/")
    assert descriptor.target_name.endswith(f"/{FIXED_CREDENTIAL_REF}")
    assert str(data_root) not in descriptor.target_name
    assert descriptor == descriptor_for(data_root / ".")
    assert descriptor.target_name != descriptor_for(tmp_path / "other-runtime").target_name
    assert list(type(RESEARCH_CREDENTIAL_SLOT)) == [RESEARCH_CREDENTIAL_SLOT]


def test_descriptor_rejects_unvalidated_lexical_roots_and_any_other_slot(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="absolute"):
        descriptor_for(Path("relative/runtime"))
    with pytest.raises(CredentialError, match="filesystem root"):
        descriptor_for(Path(tmp_path.anchor))
    with pytest.raises(CredentialError, match="parent traversal"):
        descriptor_for(tmp_path / "child" / ".." / "runtime")
    with pytest.raises(CredentialError, match="slot"):
        descriptor_for(tmp_path / "runtime", "attacker-slot")  # type: ignore[arg-type]


def test_public_presence_and_native_mutations_never_project_secret(tmp_path: Path) -> None:
    data_root = tmp_path / "runtime"
    descriptor = descriptor_for(data_root)
    vault = FakeVault({descriptor.target_name: "super-secret"})

    projection = inspect_slot(data_root, vault=vault)
    assert projection == CredentialPresence(True)
    assert [field.name for field in fields(projection)] == ["present"]
    assert "super-secret" not in repr(projection)
    assert read_slot_for_owner(data_root, vault=vault) == "super-secret"

    prompted = replace_slot_with_native_prompt(data_root, parent_window=1234, vault=vault)
    assert prompted == CredentialPromptResult(changed=True, present=True, cancelled=False)
    assert "fixture-secret" not in repr(prompted)
    assert vault.prompts == [(descriptor, 1234)]
    assert delete_slot(data_root, vault=vault) == CredentialPresence(False)


def test_delete_fails_closed_when_vault_still_reports_the_fixed_slot(tmp_path: Path) -> None:
    data_root = tmp_path / "runtime"
    descriptor = descriptor_for(data_root)

    class StubbornVault(FakeVault):
        def delete(self, selected: CredentialDescriptor) -> bool:
            self.deletes.append(selected)
            return True

    vault = StubbornVault({descriptor.target_name: "secret-must-not-escape"})
    with pytest.raises(CredentialError) as error:
        delete_slot(data_root, vault=vault)

    assert error.value.code == "desktop_credential_delete_unconfirmed"
    assert vault.deletes == [descriptor]
    assert "secret-must-not-escape" not in str(error.value)


def test_provider_matches_runtime_config_configured_and_project_environment_protocol(tmp_path: Path) -> None:
    data_root = tmp_path / "runtime"
    descriptor = descriptor_for(data_root)
    vault = FakeVault({descriptor.target_name: "research-secret"})
    provider = AppCredentialProvider(data_root, vault=vault)

    assert provider.configured(FIXED_CREDENTIAL_REF) is True
    assert provider.project_environment(FIXED_CREDENTIAL_REF, environment_name=FIXED_CREDENTIAL_ENV) == {FIXED_CREDENTIAL_ENV: "research-secret"}
    with pytest.raises(CredentialError, match="binding"):
        provider.configured("other/ref")
    with pytest.raises(CredentialError, match="binding"):
        provider.project_environment(FIXED_CREDENTIAL_REF, environment_name="OPENAI_API_KEY")


def test_launch_binding_clears_case_insensitive_inherited_managed_key_and_injects_only_fixed_key(tmp_path: Path) -> None:
    descriptor = descriptor_for(tmp_path / "runtime")
    vault = FakeVault({descriptor.target_name: "research-secret"})
    environment = bind_launch_environment(
        {"PATH": "safe", "newsroom_desktop_research_api_key": "inherited", "OPENAI_API_KEY": "unmanaged"},
        data_root=tmp_path / "runtime",
        vault=vault,
    )
    assert environment == {"PATH": "safe", "OPENAI_API_KEY": "unmanaged", FIXED_CREDENTIAL_ENV: "research-secret"}
    assert vault.reads == [descriptor]


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Credential Manager")
def test_real_windows_vault_temporary_namespace_write_read_delete_without_secret_projection(tmp_path: Path) -> None:
    # A unique lexical data root creates a fixture-owned target namespace. The secret is
    # deliberately never included in assertion messages, reprs, stdout, or logs.
    data_root = tmp_path / f"vault-fixture-{uuid.uuid4().hex}"
    descriptor = descriptor_for(data_root)
    vault = WindowsCredentialVault()
    secret = f"fixture-{uuid.uuid4().hex}"
    try:
        vault.delete(descriptor)
        assert vault.inspect(descriptor) == CredentialPresence(False)
        vault.write(descriptor, secret)
        assert vault.inspect(descriptor) == CredentialPresence(True)
        assert vault.read(descriptor) == secret
    finally:
        assert vault.delete(descriptor) is True
    assert vault.inspect(descriptor) == CredentialPresence(False)


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_descriptor_rejects_data_root_and_ancestor_junction_before_any_vault_access(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(outside)], capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        pytest.skip("Windows junction creation unavailable")
    vault = FakeVault()
    try:
        with pytest.raises(CredentialError, match="junction"):
            descriptor_for(alias / "runtime")
        with pytest.raises(CredentialError, match="junction"):
            inspect_slot(alias / "runtime", vault=vault)
        assert vault.inspected == []
        assert vault.reads == []
    finally:
        os.rmdir(alias)


def test_real_vault_adapter_rejects_forged_descriptor_before_os_access(tmp_path: Path) -> None:
    legitimate = descriptor_for(tmp_path / "runtime")
    forged = CredentialDescriptor(
        slot=legitimate.slot,
        target_name=f"{APPLICATION_ID}/v1/attacker/other",
        credential_ref=legitimate.credential_ref,
        environment_name=legitimate.environment_name,
        data_root=legitimate.data_root,
    )
    vault = object.__new__(WindowsCredentialVault)
    with pytest.raises(CredentialError, match="descriptor"):
        vault.inspect(forged)
    with pytest.raises(CredentialError, match="descriptor"):
        vault.read(forged)
    with pytest.raises(CredentialError, match="descriptor"):
        vault.delete(forged)
