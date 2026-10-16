from __future__ import annotations

import argparse
import base64
import csv
import importlib.metadata
from email.parser import Parser
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from scripts.local_desktop.common import DesktopOperationError, read_bytes_long_path, windows_extended_path

PROFILE_SCHEMA = "newsroom.desktop-python-profile/v1"
CONSTRAINTS_SCHEMA = "newsroom.desktop-python-constraints/v1"
BASE_PYTHON_SCHEMA = "newsroom.desktop-base-python/v1"
WHEELHOUSE_SCHEMA = "newsroom.desktop-wheelhouse/v1"
PORTABLE_PYTHON_SCHEMA = "newsroom.portable-python/v1"
DEFAULT_PROFILE_NAME = "windows-x64-text-research-v1"
_PROFILE_ROOT = Path(__file__).with_name("profiles")


@dataclass(frozen=True)
class DesktopRuntimeProfile:
    path: Path
    document: Mapping[str, Any]
    constraints_path: Path
    constraints: Mapping[str, Any]
    requirements_path: Path
    profile_sha256: str
    constraints_sha256: str
    requirements_sha256: str

    @property
    def profile_id(self) -> str:
        return str(self.document["profile_id"])

    @property
    def required_imports(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self.document["required_imports"])

    @property
    def required_attributes(self) -> tuple[tuple[str, str], ...]:
        return tuple((str(item["module"]), str(item["attribute"])) for item in self.document["required_attributes"])

    @property
    def packages(self) -> tuple[tuple[str, str], ...]:
        return tuple((str(item["name"]), str(item["version"])) for item in self.constraints["packages"])

    @property
    def excluded_default_distributions(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self.document["excluded_default_distributions"])

    @property
    def forbidden_default_modules(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self.document["forbidden_default_modules"])

    def identity(self) -> dict[str, str]:
        return {
            "profile_id": self.profile_id,
            "profile_sha256": self.profile_sha256,
            "constraints_sha256": self.constraints_sha256,
            "requirements_sha256": self.requirements_sha256,
        }


def load_desktop_runtime_profile(name: str = DEFAULT_PROFILE_NAME) -> DesktopRuntimeProfile:
    if not name or any(part in name for part in ("/", "\\", "..")):
        raise DesktopOperationError("desktop_runtime_profile_invalid", "desktop runtime profile name is invalid")
    path = _PROFILE_ROOT / f"{name}.json"
    document = _read_json(path, "desktop_runtime_profile_invalid")
    if document.get("schema") != PROFILE_SCHEMA or document.get("profile_id") != name:
        raise DesktopOperationError("desktop_runtime_profile_invalid", "desktop runtime profile identity is invalid")
    constraints_identity = document.get("constraints")
    if not isinstance(constraints_identity, Mapping):
        raise DesktopOperationError("desktop_runtime_profile_invalid", "desktop runtime constraints identity is missing")
    constraints_relative = _safe_relative(constraints_identity.get("path"), reason_code="desktop_runtime_profile_invalid")
    constraints_path = path.parent / constraints_relative
    constraints_sha256 = _file_sha256(constraints_path)
    if constraints_sha256 != constraints_identity.get("sha256"):
        raise DesktopOperationError("desktop_runtime_constraints_mismatch", "desktop runtime constraints do not match the profile")
    constraints = _read_json(constraints_path, "desktop_runtime_constraints_invalid")
    if constraints.get("schema") != CONSTRAINTS_SCHEMA or constraints.get("profile_id") != name:
        raise DesktopOperationError("desktop_runtime_constraints_invalid", "desktop runtime constraints identity is invalid")
    requirements_identity = document.get("requirements")
    if not isinstance(requirements_identity, Mapping):
        raise DesktopOperationError("desktop_runtime_profile_invalid", "desktop runtime requirements identity is missing")
    requirements_relative = _safe_relative(requirements_identity.get("path"), reason_code="desktop_runtime_profile_invalid")
    requirements_path = path.parent / requirements_relative
    requirements_sha256 = _file_sha256(requirements_path)
    if requirements_sha256 != requirements_identity.get("sha256"):
        raise DesktopOperationError("desktop_runtime_requirements_mismatch", "desktop runtime requirements do not match the profile")
    _validate_profile_document(document, constraints)
    expected_requirements = [f"{item['name']}=={item['version']}" for item in constraints["packages"]]
    actual_requirements = [line.strip() for line in requirements_path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if actual_requirements != expected_requirements:
        raise DesktopOperationError("desktop_runtime_requirements_mismatch", "desktop runtime requirements do not match the constraints")
    return DesktopRuntimeProfile(
        path, document, constraints_path, constraints, requirements_path, _file_sha256(path), constraints_sha256, requirements_sha256
    )



def prepare_base_python(
    *,
    source_root: str | Path,
    output_root: str | Path,
    profile_name: str = DEFAULT_PROFILE_NAME,
    command_runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
) -> dict[str, Any]:
    profile = load_desktop_runtime_profile(profile_name)
    source = _absolute_directory(source_root, "portable Python source root")
    receipt = _read_json(source / "portable-python.json", "desktop_base_python_source_invalid")
    if receipt.get("schema") != PORTABLE_PYTHON_SCHEMA or receipt.get("portable") is not True:
        raise DesktopOperationError("desktop_base_python_source_invalid", "portable Python source receipt is invalid")
    source_executable = source / "python.exe"
    if (
        receipt.get("executable") != "python.exe"
        or not source_executable.is_file()
        or not _valid_sha256(receipt.get("executable_sha256"))
        or _file_sha256(source_executable) != receipt.get("executable_sha256")
    ):
        raise DesktopOperationError("desktop_base_python_source_invalid", "portable Python source executable does not match its receipt")
    output = Path(output_root).expanduser().absolute()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise DesktopOperationError("desktop_runtime_output_not_empty", "base Python output must be absent or empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="base-python-", dir=output.parent))
    try:
        _copy_regular_tree(
            source,
            stage,
            excluded={"portable-python.json"},
            excluded_prefixes=("Lib/site-packages", "Scripts"),
        )
        executable = stage / "python.exe"
        probe = _probe_selected_python(executable, profile, command_runner or subprocess.run)
        resources = _resource_entries(stage, excluded=set())
        manifest = {
            "schema": BASE_PYTHON_SCHEMA,
            "source_kind": "python-build-standalone-portable-runtime",
            "source_receipt_sha256": _file_sha256(source / "portable-python.json"),
            "source_sha256": _json_digest({
                "source_receipt_sha256": _file_sha256(source / "portable-python.json"),
                "source_executable_sha256": str(receipt["executable_sha256"]),
            }),
            "version": probe["version"],
            "implementation": probe["implementation"],
            "cache_tag": probe["cache_tag"],
            "abi": probe["abi"],
            "platform": probe["platform"],
            "executable": "python.exe",
            "executable_sha256": _file_sha256(executable),
            "excluded_source_paths": ["Lib/site-packages", "Scripts", "portable-python.json"],
            "resources": resources,
            "resources_sha256": _json_digest({"resources": resources}),
        }
        _write_json(stage / "base-python.json", manifest)
        if output.exists():
            output.rmdir()
        os.replace(stage, output)
        return manifest
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def create_wheelhouse_receipt(
    *,
    wheelhouse_root: str | Path,
    profile_name: str = DEFAULT_PROFILE_NAME,
) -> dict[str, Any]:
    profile = load_desktop_runtime_profile(profile_name)
    root = _absolute_directory(wheelhouse_root, "desktop wheelhouse root")
    artifacts: list[dict[str, Any]] = []
    for wheel in sorted(root.glob("*.whl"), key=lambda item: item.name.casefold()):
        name, version, requires_dist = _wheel_metadata(wheel)
        artifacts.append(
            {
                "name": name,
                "version": version,
                "file": wheel.name,
                "sha256": _file_sha256(wheel),
                "requires_dist": requires_dist,
            }
        )
    artifacts.sort(key=lambda item: _normalize_distribution(str(item["name"])))
    receipt = {
        "schema": WHEELHOUSE_SCHEMA,
        "profile": profile.identity(),
        "artifacts": artifacts,
        "artifacts_sha256": _json_digest({"artifacts": artifacts}),
    }
    _validate_wheelhouse(profile, root, receipt)
    _write_json(root / "desktop-wheelhouse.json", receipt)
    return receipt


def write_requirements(*, output: str | Path, profile_name: str = DEFAULT_PROFILE_NAME) -> Path:
    profile = load_desktop_runtime_profile(profile_name)
    destination = Path(output).expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(read_bytes_long_path(profile.requirements_path))
    return destination

def build_selected_runtime(
    *,
    base_python_root: str | Path,
    wheelhouse_root: str | Path,
    output_root: str | Path,
    installer_python: str | Path,
    profile_name: str = DEFAULT_PROFILE_NAME,
    command_runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
) -> dict[str, Any]:
    profile = load_desktop_runtime_profile(profile_name)
    base_root = _absolute_directory(base_python_root, "base Python root")
    wheel_root = _absolute_directory(wheelhouse_root, "desktop wheelhouse root")
    output = Path(output_root).expanduser().absolute()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise DesktopOperationError("desktop_runtime_output_not_empty", "selected runtime output must be absent or empty")
    base_identity = _verified_input_manifest(base_root, "base-python.json", BASE_PYTHON_SCHEMA)
    wheels_identity = _verified_input_manifest(wheel_root, "desktop-wheelhouse.json", WHEELHOUSE_SCHEMA)
    _validate_base_python(profile, base_root, base_identity)
    wheel_paths, wheel_receipt = _validate_wheelhouse(profile, wheel_root, wheels_identity)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="selected-python-", dir=output.parent))
    runner = command_runner or subprocess.run
    installer_identity = _build_installer_identity(installer_python, profile, runner)
    _validate_wheel_dependency_closure(profile, wheel_receipt, installer_identity, runner)
    wheel_install_root = Path(tempfile.mkdtemp(prefix="desktop-wheel-install-", dir=output.parent))
    try:
        install_wheels = _copy_verified_wheels(wheel_paths, wheel_receipt, wheel_install_root)
        _copy_regular_tree(base_root, stage, excluded={"base-python.json"})
        site_packages = stage / "Lib" / "site-packages"
        site_packages.mkdir(parents=True, exist_ok=True)
        argv = [
            str(installer_identity["executable"]),
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-compile",
            "--no-index",
            "--no-deps",
            "--no-warn-script-location",
            "--only-binary=:all:",
            "--ignore-installed",
            "--prefix",
            str(stage),
            *[str(path) for path in install_wheels],
        ]
        _run_builder_command(runner, argv, cwd=wheel_root, timeout=900)
        packages = _installed_packages(site_packages)
        _validate_locked_packages(profile, packages)
        records = _validate_distribution_records(site_packages, runtime_root=stage)
        licenses = _license_inventory(site_packages, profile.packages)
        executable = stage / "python.exe"
        probe = _probe_selected_python(executable, profile, runner)
        attestation = {
            "schema": PORTABLE_PYTHON_SCHEMA,
            "portable": True,
            "executable": "python.exe",
            "executable_sha256": _file_sha256(executable),
            "version": probe["version"],
            "implementation": probe["implementation"],
            "cache_tag": probe["cache_tag"],
            "abi": probe["abi"],
            "platform": probe["platform"],
            "profile": profile.identity(),
            "builder": {
                "implementation": installer_identity["implementation"],
                "version": installer_identity["version"],
                "cache_tag": installer_identity["cache_tag"],
                "platform": installer_identity["platform"],
                "executable_sha256": installer_identity["executable_sha256"],
                "pip_version": installer_identity["pip_version"],
            },
            "base_python": {
                "source_sha256": str(base_identity["source_sha256"]),
                "manifest_sha256": _file_sha256(base_root / "base-python.json"),
            },
            "wheelhouse": {
                "manifest_sha256": _file_sha256(wheel_root / "desktop-wheelhouse.json"),
                "receipt_sha256": _json_digest({"artifacts": wheel_receipt}),
                "artifact_count": len(wheel_paths),
                "artifacts": wheel_receipt,
            },
            "packages": [[name, version] for name, version in packages],
            "record_count": records,
            "licenses": licenses,
        }
        _write_json(stage / "portable-python.json", attestation)
        if output.exists():
            output.rmdir()
        os.replace(stage, output)
        return attestation
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(wheel_install_root, ignore_errors=True)


def validate_portable_runtime_attestation(attestation: Mapping[str, Any], profile: DesktopRuntimeProfile) -> None:
    if attestation.get("schema") != PORTABLE_PYTHON_SCHEMA or attestation.get("portable") is not True:
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python attestation is invalid")
    if attestation.get("profile") != profile.identity():
        raise DesktopOperationError("desktop_app_runtime_profile_mismatch", "portable Python runtime was built from a different desktop profile")
    expected_python = profile.constraints["python"]
    if (
        attestation.get("executable") != "python.exe"
        or not _valid_sha256(attestation.get("executable_sha256"))
        or any(attestation.get(key) != expected_python.get(key) for key in ("version", "implementation", "cache_tag", "abi", "platform"))
    ):
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python interpreter identity is invalid")
    wheelhouse = attestation.get("wheelhouse")
    base_python = attestation.get("base_python")
    builder = attestation.get("builder")
    if not isinstance(wheelhouse, Mapping) or not isinstance(base_python, Mapping) or not isinstance(builder, Mapping):
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python provenance is missing")
    artifacts = wheelhouse.get("artifacts")
    if (
        not isinstance(artifacts, list)
        or wheelhouse.get("artifact_count") != len(artifacts)
        or wheelhouse.get("receipt_sha256") != _json_digest({"artifacts": artifacts})
        or not _valid_sha256(wheelhouse.get("manifest_sha256"))
        or not _valid_sha256(base_python.get("source_sha256"))
        or not _valid_sha256(base_python.get("manifest_sha256"))
        or not _valid_sha256(builder.get("executable_sha256"))
        or builder.get("implementation") != "CPython"
        or builder.get("cache_tag") != profile.constraints["python"]["cache_tag"]
        or builder.get("platform") != profile.constraints["python"]["platform"]
        or not str(builder.get("version") or "")
        or not str(builder.get("pip_version") or "")
    ):
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python provenance receipt is invalid")
    normalized_artifacts: list[dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python wheel receipt entry is invalid")
        declared_requirements = item.get("requires_dist")
        if not isinstance(declared_requirements, list) or any(not isinstance(value, str) or not value for value in declared_requirements):
            raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python wheel dependency metadata is invalid")
        artifact = {
            "name": str(item.get("name") or ""),
            "version": str(item.get("version") or ""),
            "file": _safe_relative(item.get("file"), reason_code="desktop_app_portable_runtime_attestation_invalid").as_posix(),
            "sha256": str(item.get("sha256") or ""),
            "requires_dist": list(declared_requirements),
        }
        if not artifact["name"] or not artifact["version"] or not _valid_sha256(artifact["sha256"]):
            raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python wheel receipt entry is invalid")
        normalized_artifacts.append(artifact)
    if normalized_artifacts != artifacts:
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python wheel receipt is not canonical")
    _validate_locked_packages(profile, tuple((item["name"], item["version"]) for item in normalized_artifacts))
    packages = attestation.get("packages")
    if not isinstance(packages, list) or any(
        not isinstance(item, list)
        or len(item) != 2
        or not all(isinstance(value, str) and value for value in item)
        for item in packages
    ):
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python package attestation is missing")
    _validate_locked_packages(profile, tuple((item[0], item[1]) for item in packages))
    if not isinstance(attestation.get("record_count"), int) or int(attestation["record_count"]) < len(packages):
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python RECORD attestation is invalid")
    licenses = attestation.get("licenses")
    if not isinstance(licenses, list) or len(licenses) != len(packages) or any(
        not isinstance(item, Mapping) or not str(item.get("name") or "") or not str(item.get("version") or "")
        for item in licenses
    ):
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable Python license attestation is invalid")
    _validate_locked_packages(profile, tuple((str(item["name"]), str(item["version"])) for item in licenses))


def _validate_profile_document(document: Mapping[str, Any], constraints: Mapping[str, Any]) -> None:
    imports = document.get("required_imports")
    attributes = document.get("required_attributes")
    capabilities = document.get("capabilities")
    excluded = document.get("excluded_default_distributions")
    forbidden_modules = document.get("forbidden_default_modules")
    packages = constraints.get("packages")
    if not isinstance(imports, list) or not imports or len(imports) != len(set(imports)):
        raise DesktopOperationError("desktop_runtime_profile_invalid", "desktop runtime imports are invalid")
    if not isinstance(attributes, list) or not all(isinstance(item, Mapping) and item.get("module") and item.get("attribute") for item in attributes):
        raise DesktopOperationError("desktop_runtime_profile_invalid", "desktop runtime attribute probes are invalid")
    if not isinstance(capabilities, Mapping) or not capabilities:
        raise DesktopOperationError("desktop_runtime_profile_invalid", "desktop runtime capabilities are missing")
    if not isinstance(excluded, list) or not isinstance(forbidden_modules, list) or not forbidden_modules:
        raise DesktopOperationError("desktop_runtime_profile_invalid", "desktop optional distribution exclusions are invalid")
    if not isinstance(packages, list) or not packages:
        raise DesktopOperationError("desktop_runtime_constraints_invalid", "desktop runtime package constraints is empty")
    normalized: set[str] = set()
    for item in packages:
        if not isinstance(item, Mapping) or not item.get("name") or not item.get("version"):
            raise DesktopOperationError("desktop_runtime_constraints_invalid", "desktop runtime package entry is invalid")
        name = _normalize_distribution(str(item["name"]))
        if name in normalized:
            raise DesktopOperationError("desktop_runtime_constraints_invalid", "desktop runtime package constraints contains duplicates")
        normalized.add(name)
    overlap = normalized.intersection(_normalize_distribution(str(item)) for item in excluded)
    if overlap:
        raise DesktopOperationError("desktop_runtime_profile_invalid", "optional distributions cannot also be constrained by the default profile")


def _validate_base_python(profile: DesktopRuntimeProfile, root: Path, identity: Mapping[str, Any]) -> None:
    if (
        identity.get("source_kind") != "python-build-standalone-portable-runtime"
        or identity.get("executable") != "python.exe"
        or identity.get("excluded_source_paths") != ["Lib/site-packages", "Scripts", "portable-python.json"]
        or not _valid_sha256(identity.get("source_receipt_sha256"))
    ):
        raise DesktopOperationError("desktop_base_python_manifest_invalid", "base Python source provenance is invalid")
    expected_python = profile.constraints.get("python")
    if not isinstance(expected_python, Mapping):
        raise DesktopOperationError("desktop_runtime_constraints_invalid", "desktop runtime Python identity is missing")
    for key in ("version", "implementation", "cache_tag", "abi", "platform"):
        if identity.get(key) != expected_python.get(key):
            raise DesktopOperationError("desktop_base_python_mismatch", f"base Python {key} does not match the desktop profile")
    resources = identity.get("resources")
    if not isinstance(resources, list) or not resources:
        raise DesktopOperationError("desktop_base_python_manifest_invalid", "base Python resource inventory is missing")
    expected_resources = _resource_map(resources, reason_code="desktop_base_python_manifest_invalid")
    actual_resources = _regular_file_inventory(root, excluded={"base-python.json"})
    if actual_resources != expected_resources or identity.get("resources_sha256") != _json_digest({"resources": resources}):
        raise DesktopOperationError("desktop_base_python_mismatch", "base Python files do not match the input manifest")
    executable = root / "python.exe"
    if not executable.is_file() or _file_sha256(executable) != identity.get("executable_sha256"):
        raise DesktopOperationError("desktop_base_python_mismatch", "base Python executable does not match its input manifest")
    source_sha256 = str(identity.get("source_sha256") or "")
    if len(source_sha256) != 64 or any(character not in "0123456789abcdef" for character in source_sha256.casefold()):
        raise DesktopOperationError("desktop_base_python_manifest_invalid", "base Python source archive hash is invalid")


def _validate_wheelhouse(
    profile: DesktopRuntimeProfile,
    root: Path,
    identity: Mapping[str, Any],
) -> tuple[tuple[Path, ...], list[dict[str, Any]]]:
    if identity.get("profile") != profile.identity():
        raise DesktopOperationError("desktop_wheelhouse_profile_mismatch", "desktop wheelhouse was prepared for a different runtime profile")
    artifacts = identity.get("artifacts")
    if not isinstance(artifacts, list):
        raise DesktopOperationError("desktop_wheelhouse_manifest_invalid", "desktop wheelhouse artifact list is missing")
    expected = {_normalize_distribution(name): version for name, version in profile.packages}
    selected: dict[str, Path] = {}
    receipt: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise DesktopOperationError("desktop_wheelhouse_manifest_invalid", "desktop wheelhouse artifact entry is invalid")
        name = _normalize_distribution(str(item.get("name") or ""))
        version = str(item.get("version") or "")
        relative = _safe_relative(item.get("file"), reason_code="desktop_wheelhouse_manifest_invalid")
        artifact = root / relative
        if name not in expected or expected[name] != version or name in selected:
            raise DesktopOperationError("desktop_wheelhouse_constraints_mismatch", "desktop wheelhouse does not exactly match the runtime constraints")
        declared_requirements = item.get("requires_dist")
        if not isinstance(declared_requirements, list) or any(not isinstance(value, str) or not value for value in declared_requirements):
            raise DesktopOperationError("desktop_wheelhouse_manifest_invalid", "desktop wheel dependency metadata is invalid")
        if (
            artifact.suffix.casefold() != ".whl"
            or not artifact.is_file()
            or not _valid_sha256(item.get("sha256"))
            or _file_sha256(artifact) != item.get("sha256")
        ):
            raise DesktopOperationError("desktop_wheelhouse_artifact_mismatch", f"desktop wheel artifact is missing or changed: {relative}")
        metadata_name, metadata_version, metadata_requirements = _wheel_metadata(artifact)
        if _normalize_distribution(metadata_name) != name or metadata_version != version or metadata_requirements != declared_requirements:
            raise DesktopOperationError("desktop_wheelhouse_artifact_mismatch", f"desktop wheel metadata changed: {relative}")
        selected[name] = artifact
        receipt[name] = {
            "name": str(item["name"]),
            "version": version,
            "file": relative.as_posix(),
            "sha256": str(item["sha256"]),
            "requires_dist": list(declared_requirements),
        }
    normalized_receipt = [receipt[name] for name in sorted(receipt)]
    declared_receipt_digest = identity.get("artifacts_sha256")
    if declared_receipt_digest != _json_digest({"artifacts": normalized_receipt}):
        raise DesktopOperationError("desktop_wheelhouse_manifest_invalid", "desktop wheelhouse artifact receipt digest is invalid")
    wheelhouse_files = set(_regular_file_inventory(root, excluded={"desktop-wheelhouse.json"}))
    receipt_files = {item["file"] for item in normalized_receipt}
    if wheelhouse_files != receipt_files:
        raise DesktopOperationError(
            "desktop_wheelhouse_manifest_invalid",
            "desktop wheelhouse contains unreceipted or missing files",
            details={"missing": sorted(receipt_files - wheelhouse_files), "extra": sorted(wheelhouse_files - receipt_files)},
        )
    if set(selected) != set(expected):
        raise DesktopOperationError(
            "desktop_wheelhouse_constraints_mismatch",
            "desktop wheelhouse does not contain the exact constrained distribution set",
            details={"missing": sorted(set(expected) - set(selected)), "extra": sorted(set(selected) - set(expected))},
        )
    ordered_names = sorted(selected)
    return tuple(selected[name] for name in ordered_names), normalized_receipt



def _build_installer_identity(
    installer_python: str | Path,
    profile: DesktopRuntimeProfile,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> dict[str, str]:
    executable = Path(installer_python).expanduser().absolute()
    if not executable.is_file():
        raise DesktopOperationError("desktop_runtime_build_installer_missing", "controlled build installer Python is missing")
    _reject_reparse(executable)
    script = (
        "import json,platform,sys,sysconfig;"
        "print(json.dumps({'version':sys.version.split()[0],'implementation':platform.python_implementation(),"
        "'cache_tag':sys.implementation.cache_tag,'platform':sysconfig.get_platform()},sort_keys=True,separators=(',',':')))"
    )
    identity_output = _run_builder_probe(runner, [str(executable), "-I", "-c", script], "Python identity")
    try:
        identity = json.loads(identity_output)
    except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise DesktopOperationError("desktop_runtime_build_installer_invalid", "controlled build installer returned invalid identity") from exc
    expected_python = profile.constraints["python"]
    if (
        not isinstance(identity, Mapping)
        or identity.get("implementation") != "CPython"
        or identity.get("cache_tag") != expected_python.get("cache_tag")
        or identity.get("platform") != expected_python.get("platform")
        or not str(identity.get("version") or "")
    ):
        raise DesktopOperationError(
            "desktop_runtime_build_installer_incompatible",
            "controlled build installer must be CPython 3.12 for the constrained Windows platform",
        )
    pip_output = _run_builder_probe(runner, [str(executable), "-I", "-m", "pip", "--version"], "pip identity")
    pip_parts = pip_output.split()
    pip_version = pip_parts[1] if len(pip_parts) >= 2 and pip_parts[0].casefold() == "pip" else ""
    if not pip_version:
        raise DesktopOperationError("desktop_runtime_build_installer_invalid", "controlled build installer pip identity is invalid")
    return {
        "executable": str(executable.resolve(strict=True)),
        "executable_sha256": _file_sha256(executable),
        "implementation": str(identity["implementation"]),
        "version": str(identity["version"]),
        "cache_tag": str(identity["cache_tag"]),
        "platform": str(identity["platform"]),
        "pip_version": pip_version,
    }


def _run_builder_probe(
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    argv: list[str],
    label: str,
) -> str:
    try:
        result = runner(argv, capture_output=True, text=False, timeout=30, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopOperationError("desktop_runtime_build_installer_unavailable", f"controlled build installer {label} could not be inspected") from exc
    if result.returncode != 0:
        raise DesktopOperationError(
            "desktop_runtime_build_installer_unavailable",
            f"controlled build installer {label} probe failed",
            details={"returncode": result.returncode},
        )
    return _decode_output(result.stdout).strip()


def _validate_wheel_dependency_closure(
    profile: DesktopRuntimeProfile,
    receipt: Sequence[Mapping[str, Any]],
    installer_identity: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    selected = {_normalize_distribution(name): version for name, version in profile.packages}
    requirements = [
        requirement
        for item in receipt
        for requirement in item.get("requires_dist", [])
        if isinstance(requirement, str)
    ]
    payload = json.dumps(
        {
            "selected": selected,
            "requirements": requirements,
            "environment": {
                "implementation_name": "cpython",
                "platform_python_implementation": "CPython",
                "python_full_version": str(profile.constraints["python"]["version"]),
                "python_version": ".".join(str(profile.constraints["python"]["version"]).split(".")[:2]),
                "sys_platform": "win32",
                "platform_system": "Windows",
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    script = (
        "import json,sys;"
        "from pip._vendor.packaging.requirements import Requirement;"
        "from pip._vendor.packaging.markers import default_environment;"
        f"p=json.loads({payload!r});"
        "env=default_environment();env.update(p['environment']);"
        "bad=[];"
        "[(bad.append(str(r)) if ((r.marker is None or r.marker.evaluate(env)) and "
        "(r.name.lower().replace('_','-').replace('.','-') not in p['selected'] or "
        "(r.specifier and p['selected'].get(r.name.lower().replace('_','-').replace('.','-'),'') not in r.specifier))) else None) "
        "for r in map(Requirement,p['requirements'])];"
        "print(json.dumps({'invalid':bad},sort_keys=True,separators=(',',':')));"
        "sys.exit(1 if bad else 0)"
    )
    try:
        result = runner(
            [str(installer_identity["executable"]), "-I", "-c", script],
            capture_output=True,
            text=False,
            timeout=60,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopOperationError("desktop_wheelhouse_metadata_unavailable", "desktop wheel dependency metadata could not be evaluated") from exc
    if result.returncode != 0:
        details: dict[str, Any] = {"returncode": result.returncode}
        try:
            evaluated = json.loads(_decode_output(result.stdout))
            if isinstance(evaluated, Mapping) and isinstance(evaluated.get("invalid"), list):
                details["invalid"] = evaluated["invalid"]
        except (UnicodeError, json.JSONDecodeError, TypeError):
            pass
        raise DesktopOperationError(
            "desktop_wheelhouse_dependency_closure_invalid",
            "desktop wheel metadata requires packages outside the selected runtime constraints",
            details=details,
        )



def _copy_verified_wheels(
    wheel_paths: Sequence[Path],
    receipt: Sequence[Mapping[str, Any]],
    destination: Path,
) -> tuple[Path, ...]:
    if len(wheel_paths) != len(receipt):
        raise DesktopOperationError("desktop_wheelhouse_manifest_invalid", "desktop wheelhouse receipt cardinality changed")
    copied: list[Path] = []
    for source, item in zip(wheel_paths, receipt, strict=True):
        data = read_bytes_long_path(source)
        expected = str(item.get("sha256") or "")
        if sha256(data).hexdigest() != expected or source.name != item.get("file"):
            raise DesktopOperationError("desktop_wheelhouse_artifact_mismatch", f"desktop wheel artifact changed before installation: {source.name}")
        target = destination / source.name
        target.write_bytes(data)
        copied.append(target)
    return tuple(copied)


def _probe_selected_python(
    executable: Path,
    profile: DesktopRuntimeProfile,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> dict[str, Any]:
    expected = profile.constraints["python"]
    script = (
        "import json,platform,sqlite3,ssl,sys,sysconfig;"
        "c=sqlite3.connect(':memory:');c.execute('pragma journal_mode=memory');c.close();ssl.create_default_context();"
        "print(json.dumps({'version':sys.version.split()[0],'implementation':platform.python_implementation(),"
        "'cache_tag':sys.implementation.cache_tag,'abi':sysconfig.get_config_var('SOABI') or '',"
        "'platform':sysconfig.get_platform()},sort_keys=True,separators=(',',':')))"
    )
    try:
        result = runner(
            [str(executable), "-B", "-I", "-c", script],
            capture_output=True,
            text=False,
            timeout=60,
            check=False,
            shell=False,
            env={**{key: value for key, value in os.environ.items() if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopOperationError("desktop_selected_python_probe_failed", "selected Python runtime could not be probed") from exc
    if result.returncode != 0:
        raise DesktopOperationError("desktop_selected_python_probe_failed", "selected Python runtime probe failed", details={"returncode": result.returncode})
    try:
        payload = json.loads(_decode_output(result.stdout))
    except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise DesktopOperationError("desktop_selected_python_probe_invalid", "selected Python runtime probe returned invalid JSON") from exc
    if not isinstance(payload, Mapping) or any(payload.get(key) != expected.get(key) for key in ("version", "implementation", "cache_tag", "abi", "platform")):
        raise DesktopOperationError("desktop_selected_python_probe_mismatch", "selected Python runtime does not match the constrained platform")
    return dict(payload)


def _installed_packages(site_packages: Path) -> tuple[tuple[str, str], ...]:
    packages = sorted(
        (str(distribution.metadata.get("Name") or ""), distribution.version)
        for distribution in importlib.metadata.distributions(path=[str(site_packages)])
        if distribution.metadata.get("Name")
    )
    return tuple(packages)


def _validate_locked_packages(profile: DesktopRuntimeProfile, packages: Sequence[tuple[str, str]]) -> None:
    expected = {_normalize_distribution(name): version for name, version in profile.packages}
    actual = {_normalize_distribution(name): version for name, version in packages}
    if actual != expected:
        raise DesktopOperationError(
            "desktop_runtime_package_constraints_mismatch",
            "selected Python distributions do not exactly match the desktop runtime constraints",
            details={
                "missing": sorted(set(expected) - set(actual)),
                "extra": sorted(set(actual) - set(expected)),
                "version_mismatch": sorted(name for name in set(actual).intersection(expected) if actual[name] != expected[name]),
            },
        )


def _validate_distribution_records(site_packages: Path, *, runtime_root: Path | None = None) -> int:
    site_root = site_packages.resolve()
    script_root = (runtime_root / "Scripts").resolve() if runtime_root is not None else None
    record_count = 0
    for record in sorted(site_packages.glob("*.dist-info/RECORD")):
        dist_root = record.parent.parent
        with record.open("r", encoding="utf-8", newline="") as handle:
            for relative, digest_value, size_value in csv.reader(handle):
                if not relative:
                    raise DesktopOperationError("desktop_runtime_record_invalid", f"empty RECORD path in {record.parent.name}")
                target = (dist_root / relative).resolve(strict=False)
                if (not target.is_relative_to(site_root) and (script_root is None or not target.is_relative_to(script_root))) or not target.is_file():
                    raise DesktopOperationError("desktop_runtime_record_missing", f"constrained distribution resource is missing: {relative}")
                data = read_bytes_long_path(target)
                if size_value and int(size_value) != len(data):
                    raise DesktopOperationError("desktop_runtime_record_mismatch", f"constrained distribution resource size changed: {relative}")
                if digest_value:
                    algorithm, encoded = digest_value.split("=", 1)
                    if algorithm != "sha256":
                        raise DesktopOperationError("desktop_runtime_record_invalid", f"unsupported RECORD digest: {algorithm}")
                    actual = base64.urlsafe_b64encode(sha256(data).digest()).rstrip(b"=").decode("ascii")
                    if actual != encoded:
                        raise DesktopOperationError("desktop_runtime_record_mismatch", f"constrained distribution resource hash changed: {relative}")
                record_count += 1
    if record_count == 0:
        raise DesktopOperationError("desktop_runtime_record_invalid", "selected runtime contains no distribution RECORD entries")
    return record_count


def _license_inventory(site_packages: Path, packages: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    expected = {_normalize_distribution(name): version for name, version in packages}
    inventory: list[dict[str, Any]] = []
    for distribution in importlib.metadata.distributions(path=[str(site_packages)]):
        name = str(distribution.metadata.get("Name") or "")
        normalized = _normalize_distribution(name)
        if normalized not in expected:
            continue
        files = sorted(
            str(item).replace("\\", "/")
            for item in (distribution.files or ())
            if any(part.casefold().startswith(("license", "copying", "notice")) for part in Path(str(item)).parts)
        )
        declared = str(distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "").strip()
        classifiers = sorted(value for value in distribution.metadata.get_all("Classifier", []) if value.startswith("License ::"))
        if not declared and not classifiers and not files:
            raise DesktopOperationError("desktop_runtime_license_missing", f"constrained distribution has no license metadata or file: {name}")
        inventory.append({"name": name, "version": distribution.version, "declared": declared, "classifiers": classifiers, "files": files})
    if len(inventory) != len(expected):
        raise DesktopOperationError("desktop_runtime_license_missing", "license inventory does not cover every constrained distribution")
    return sorted(inventory, key=lambda item: _normalize_distribution(str(item["name"])))


def _verified_input_manifest(root: Path, name: str, schema: str) -> dict[str, Any]:
    manifest = _read_json(root / name, "desktop_runtime_input_manifest_invalid")
    if manifest.get("schema") != schema:
        raise DesktopOperationError("desktop_runtime_input_manifest_invalid", f"unsupported runtime input manifest: {name}")
    return manifest


def _copy_regular_tree(
    source: Path,
    destination: Path,
    *,
    excluded: set[str],
    excluded_prefixes: tuple[str, ...] = (),
) -> None:
    for directory, names, files in os.walk(source, topdown=True, followlinks=False):
        current = Path(directory)
        _reject_reparse(current)
        retained_names: list[str] = []
        for name in names:
            child = current / name
            _reject_reparse(child)
            if not child.is_dir():
                raise DesktopOperationError("desktop_runtime_input_type_invalid", f"runtime input is not a directory: {child.relative_to(source).as_posix()}")
            relative = child.relative_to(source).as_posix()
            if relative in excluded or any(relative == prefix or relative.startswith(prefix + "/") for prefix in excluded_prefixes):
                continue
            retained_names.append(name)
        names[:] = retained_names
        target_directory = destination / current.relative_to(source)
        target_directory.mkdir(parents=True, exist_ok=True)
        for name in files:
            relative = (current / name).relative_to(source).as_posix()
            if relative in excluded or any(relative == prefix or relative.startswith(prefix + "/") for prefix in excluded_prefixes):
                continue
            source_file = current / name
            _reject_reparse(source_file)
            if not source_file.is_file():
                raise DesktopOperationError("desktop_runtime_input_type_invalid", f"runtime input is not a regular file: {relative}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(windows_extended_path(source_file), windows_extended_path(target), follow_symlinks=False)



def _wheel_metadata(path: Path) -> tuple[str, str, list[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise DesktopOperationError("desktop_wheelhouse_artifact_invalid", f"wheel metadata is ambiguous: {path.name}")
            metadata = archive.read(metadata_names[0]).decode("utf-8")
    except (OSError, zipfile.BadZipFile, UnicodeError) as exc:
        raise DesktopOperationError("desktop_wheelhouse_artifact_invalid", f"wheel could not be inspected: {path.name}") from exc
    message = Parser().parsestr(metadata)
    name = str(message.get("Name") or "").strip()
    version = str(message.get("Version") or "").strip()
    requirements = sorted(
        (str(value).strip() for value in message.get_all("Requires-Dist", []) if str(value).strip()),
        key=str.casefold,
    )
    if not name or not version:
        raise DesktopOperationError("desktop_wheelhouse_artifact_invalid", f"wheel package identity is missing: {path.name}")
    return name, version, requirements


def _regular_file_inventory(root: Path, *, excluded: set[str]) -> dict[str, tuple[int, str]]:
    inventory: dict[str, tuple[int, str]] = {}
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        _reject_reparse(current)
        names.sort(key=str.casefold)
        files.sort(key=str.casefold)
        for name in names:
            child = current / name
            _reject_reparse(child)
            if not child.is_dir():
                raise DesktopOperationError("desktop_runtime_input_type_invalid", f"runtime input is not a directory: {child.relative_to(root).as_posix()}")
        for name in files:
            path = current / name
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            _reject_reparse(path)
            if not path.is_file():
                raise DesktopOperationError("desktop_runtime_input_type_invalid", f"runtime input is not a regular file: {relative}")
            data = read_bytes_long_path(path)
            inventory[relative] = (len(data), sha256(data).hexdigest())
    return inventory


def _resource_entries(root: Path, *, excluded: set[str]) -> list[dict[str, Any]]:
    return [
        {"path": path, "size": identity[0], "sha256": identity[1]}
        for path, identity in sorted(_regular_file_inventory(root, excluded=excluded).items())
    ]


def _resource_map(resources: Sequence[Any], *, reason_code: str) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for item in resources:
        if not isinstance(item, Mapping):
            raise DesktopOperationError(reason_code, "runtime resource entry is invalid")
        relative = _safe_relative(item.get("path"), reason_code=reason_code).as_posix()
        size = item.get("size")
        digest = item.get("sha256")
        if relative in result or not isinstance(size, int) or size < 0 or not _valid_sha256(digest):
            raise DesktopOperationError(reason_code, "runtime resource entry is invalid")
        result[relative] = (size, digest)
    return result

def _run_builder_command(
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
) -> None:
    try:
        result = runner(argv, cwd=cwd, capture_output=True, text=False, timeout=timeout, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopOperationError("desktop_runtime_build_failed", "selected runtime wheel installation could not complete") from exc
    if result.returncode != 0:
        raise DesktopOperationError(
            "desktop_runtime_build_failed",
            "selected runtime wheel installation failed",
            details={"returncode": result.returncode, "stderr": _decode_output(result.stderr)[-2000:]},
        )


def _absolute_directory(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().absolute()
    if not path.is_dir():
        raise DesktopOperationError("desktop_runtime_input_missing", f"{label} is missing")
    _reject_reparse(path)
    return path.resolve(strict=True)


def _reject_reparse(path: Path) -> None:
    metadata = windows_extended_path(path).lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    if stat.S_ISLNK(metadata.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise DesktopOperationError("desktop_reparse_point_rejected", f"runtime input reparse point is not allowed: {path.name}")


def _safe_relative(value: Any, *, reason_code: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise DesktopOperationError(reason_code, "runtime input relative path is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in path.parts):
        raise DesktopOperationError(reason_code, "runtime input relative path is unsafe")
    return path


def _normalize_distribution(value: str) -> str:
    return value.strip().casefold().replace("_", "-").replace(".", "-")


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value.casefold())


def _read_json(path: Path, reason_code: str) -> dict[str, Any]:
    try:
        value = json.loads(read_bytes_long_path(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopOperationError(reason_code, f"invalid JSON document: {path.name}") from exc
    if not isinstance(value, dict):
        raise DesktopOperationError(reason_code, f"JSON document must be an object: {path.name}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _json_digest(value: Mapping[str, Any]) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return sha256(read_bytes_long_path(path)).hexdigest()


def _decode_output(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    raise TypeError("subprocess output must be bytes or text")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the constrained Agora desktop Python runtime from verified offline inputs.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-base")
    prepare.add_argument("--source-root", required=True)
    prepare.add_argument("--output-root", required=True)
    prepare.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    requirements = subparsers.add_parser("requirements")
    requirements.add_argument("--output", required=True)
    requirements.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    receipt = subparsers.add_parser("wheelhouse-receipt")
    receipt.add_argument("--wheelhouse-root", required=True)
    receipt.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    build = subparsers.add_parser("build")
    build.add_argument("--base-python-root", required=True)
    build.add_argument("--wheelhouse-root", required=True)
    build.add_argument("--output-root", required=True)
    build.add_argument("--installer-python", required=True)
    build.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "prepare-base":
        result = prepare_base_python(source_root=args.source_root, output_root=args.output_root, profile_name=args.profile)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "requirements":
        print(str(write_requirements(output=args.output, profile_name=args.profile)))
        return 0
    if args.command == "wheelhouse-receipt":
        result = create_wheelhouse_receipt(wheelhouse_root=args.wheelhouse_root, profile_name=args.profile)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "build":
        result = build_selected_runtime(
            base_python_root=args.base_python_root,
            wheelhouse_root=args.wheelhouse_root,
            output_root=args.output_root,
            installer_python=args.installer_python,
            profile_name=args.profile,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BASE_PYTHON_SCHEMA",
    "DEFAULT_PROFILE_NAME",
    "DesktopRuntimeProfile",
    "CONSTRAINTS_SCHEMA",
    "PORTABLE_PYTHON_SCHEMA",
    "PROFILE_SCHEMA",
    "WHEELHOUSE_SCHEMA",
    "build_selected_runtime",
    "create_wheelhouse_receipt",
    "load_desktop_runtime_profile",
    "main",
    "prepare_base_python",
    "validate_portable_runtime_attestation",
    "write_requirements",
]
