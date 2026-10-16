from __future__ import annotations

import base64
import csv
import json
import shutil
import subprocess
import zipfile
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from scripts.desktop_app import runtime_profile as runtime
from scripts.local_desktop.common import DesktopOperationError


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def profile():
    real = runtime.load_desktop_runtime_profile()
    return replace(real, constraints={**real.constraints, "packages": [{"name": "alpha-engine", "version": "1.0"}, {"name": "beta-kit", "version": "2.0"}]})


@pytest.fixture
def profile_root(tmp_path, monkeypatch):
    source = Path(runtime.__file__).with_name("profiles")
    root = tmp_path / "profiles"
    shutil.copytree(source, root)
    monkeypatch.setattr(runtime, "_PROFILE_ROOT", root)
    return root


def assert_reason(reason, call):
    with pytest.raises(DesktopOperationError) as caught:
        call()
    assert caught.value.reason_code == reason


def make_wheel(root, name, version, requires=()):
    wheel = root / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(f"Requires-Dist: {item}\n" for item in requires)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{name.replace('-', '_')}-{version}.dist-info/METADATA", metadata)
    return {"name": name, "version": version, "file": wheel.name, "sha256": sha256(wheel.read_bytes()).hexdigest(), "requires_dist": list(requires)}


def wheelhouse(root, profile):
    artifacts = [make_wheel(root, "alpha-engine", "1.0", ("beta-kit>=2",)), make_wheel(root, "beta-kit", "2.0")]
    return {"schema": runtime.WHEELHOUSE_SCHEMA, "profile": profile.identity(), "artifacts": artifacts, "artifacts_sha256": digest({"artifacts": artifacts})}


def test_default_profile_is_explicit_and_bound_to_constraints_and_requirements():
    selected = runtime.load_desktop_runtime_profile()
    assert selected.profile_id == "windows-x64-text-research-v1"
    assert selected.constraints["python"]["version"] == "3.12.14"
    assert "interfaces.api.app" in selected.required_imports
    assert "pymupdf" in selected.required_imports
    assert {"numpy", "scipy", "torch"}.issubset(selected.forbidden_default_modules)
    assert not {name.lower() for name, _ in selected.packages}.intersection(selected.excluded_default_distributions)
    assert all(len(value) == 64 for key, value in selected.identity().items() if key.endswith("sha256"))


@pytest.mark.parametrize("name", ["../foreign", "a/b", "a\\b", ""])
def test_profile_name_cannot_escape_owned_directory(name):
    assert_reason("desktop_runtime_profile_invalid", lambda: runtime.load_desktop_runtime_profile(name))


@pytest.mark.parametrize("suffix,reason", [("constraints.json", "desktop_runtime_constraints_mismatch"), ("requirements.txt", "desktop_runtime_requirements_mismatch")])
def test_profile_rejects_same_length_wrong_input_content(profile_root, suffix, reason):
    path = profile_root / f"{runtime.DEFAULT_PROFILE_NAME}.{suffix}"
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    assert_reason(reason, runtime.load_desktop_runtime_profile)


def test_hash_valid_requirements_still_must_match_package_constraints(profile_root):
    path = profile_root / f"{runtime.DEFAULT_PROFILE_NAME}.requirements.txt"
    path.write_text("wrong-package==1.0\n", encoding="utf-8")
    document_path = profile_root / f"{runtime.DEFAULT_PROFILE_NAME}.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document["requirements"]["sha256"] = sha256(path.read_bytes()).hexdigest()
    document_path.write_text(json.dumps(document), encoding="utf-8")
    assert_reason("desktop_runtime_requirements_mismatch", runtime.load_desktop_runtime_profile)


def test_wheelhouse_accepts_only_exact_hashed_metadata_bound_set(tmp_path, profile):
    receipt = wheelhouse(tmp_path, profile)
    paths, artifacts = runtime._validate_wheelhouse(profile, tmp_path, receipt)
    assert len(paths) == len(artifacts) == 2
    assert artifacts[0]["requires_dist"] == ["beta-kit>=2"]
    assert all(path.is_file() for path in paths)


@pytest.mark.parametrize("mutation,reason", [("content", "desktop_wheelhouse_artifact_mismatch"), ("metadata", "desktop_wheelhouse_artifact_mismatch"), ("extra", "desktop_wheelhouse_manifest_invalid"), ("missing", "desktop_wheelhouse_artifact_mismatch"), ("profile", "desktop_wheelhouse_profile_mismatch"), ("receipt", "desktop_wheelhouse_manifest_invalid")])
def test_wheelhouse_rejects_drift_without_installing(tmp_path, profile, mutation, reason):
    receipt = wheelhouse(tmp_path, profile)
    first = receipt["artifacts"][0]
    path = tmp_path / first["file"]
    if mutation == "content":
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    elif mutation == "metadata":
        first["requires_dist"] = []
    elif mutation == "extra":
        (tmp_path / "unreceipted.whl").write_bytes(b"extra")
    elif mutation == "missing":
        path.unlink()
    elif mutation == "profile":
        receipt["profile"] = {}
    else:
        receipt["artifacts_sha256"] = "0" * 64
    assert_reason(reason, lambda: runtime._validate_wheelhouse(profile, tmp_path, receipt))


def test_wheelhouse_rejects_path_escape_before_reading_external_artifact(tmp_path, profile):
    receipt = wheelhouse(tmp_path, profile)
    receipt["artifacts"][0]["file"] = "../outside.whl"
    assert_reason("desktop_wheelhouse_manifest_invalid", lambda: runtime._validate_wheelhouse(profile, tmp_path, receipt))


def test_selected_distribution_set_rejects_missing_extra_or_wrong_version(profile):
    runtime._validate_locked_packages(profile, (("alpha_engine", "1.0"), ("beta-kit", "2.0")))
    for packages in [(("alpha-engine", "1.0"),), (("alpha-engine", "1.0"), ("beta-kit", "2.1")), (("alpha-engine", "1.0"), ("beta-kit", "2.0"), ("torch", "1"))]:
        assert_reason("desktop_runtime_package_constraints_mismatch", lambda: runtime._validate_locked_packages(profile, packages))


def test_base_copy_excludes_development_environment_without_mutating_source(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    for relative, data in {"python.exe": b"runtime", "Lib/os.py": b"stdlib", "Lib/site-packages/dev.py": b"development", "Scripts/pip.exe": b"installer", "LICENSE.txt": b"license", "portable-python.json": b"receipt"}.items():
        file = source / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
    before = {file.relative_to(source).as_posix(): file.read_bytes() for file in source.rglob("*") if file.is_file()}
    runtime._copy_regular_tree(source, target, excluded={"portable-python.json"}, excluded_prefixes=("Lib/site-packages", "Scripts"))
    assert {file.relative_to(target).as_posix() for file in target.rglob("*") if file.is_file()} == {"python.exe", "Lib/os.py", "LICENSE.txt"}
    assert before == {file.relative_to(source).as_posix(): file.read_bytes() for file in source.rglob("*") if file.is_file()}


def test_distribution_record_validates_file_hash_and_escape(tmp_path):
    payload = tmp_path / "alpha.py"
    payload.write_bytes(b"module")
    info = tmp_path / "alpha-1.dist-info"
    info.mkdir()
    record = info / "RECORD"
    encoded = base64.urlsafe_b64encode(sha256(payload.read_bytes()).digest()).rstrip(b"=").decode()
    with record.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows([["alpha.py", f"sha256={encoded}", "6"], ["alpha-1.dist-info/RECORD", "", ""]])
    assert runtime._validate_distribution_records(tmp_path) == 2
    payload.write_bytes(b"MODULE")
    assert_reason("desktop_runtime_record_mismatch", lambda: runtime._validate_distribution_records(tmp_path))
    record.write_text("../outside.py,,\n", encoding="utf-8")
    assert_reason("desktop_runtime_record_missing", lambda: runtime._validate_distribution_records(tmp_path))


@pytest.mark.parametrize("cache_tag,platform,reason", [("cpython-312", "win-amd64", None), ("cpython-314", "win-amd64", "desktop_runtime_build_installer_incompatible"), ("cpython-312", "linux-x86_64", "desktop_runtime_build_installer_incompatible")])
def test_installer_is_explicit_and_matches_target_abi(tmp_path, profile, cache_tag, platform, reason):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"controlled-build-tool")
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        output = b"pip 26.2.1 from controlled (python 3.12)\n" if argv[-1] == "--version" else json.dumps({"version": "3.12.7", "implementation": "CPython", "cache_tag": cache_tag, "platform": platform}).encode()
        return subprocess.CompletedProcess(argv, 0, output, b"")
    action = lambda: runtime._build_installer_identity(executable, profile, runner)
    if reason:
        assert_reason(reason, action)
    else:
        identity = action()
        assert identity["executable_sha256"] == sha256(executable.read_bytes()).hexdigest()
        assert identity["pip_version"] == "26.2.1"
        assert len(calls) == 2
        assert all(command[0] == str(executable) and command[1] == "-I" for command in calls)


def test_incomplete_or_foreign_portable_attestation_is_not_accepted(profile):
    assert_reason("desktop_app_portable_runtime_attestation_invalid", lambda: runtime.validate_portable_runtime_attestation({}, profile))
    assert_reason("desktop_app_runtime_profile_mismatch", lambda: runtime.validate_portable_runtime_attestation({"schema": runtime.PORTABLE_PYTHON_SCHEMA, "portable": True, "profile": {}}, profile))


def test_base_probe_keeps_windows_tls_system_environment_but_not_credentials(tmp_path, profile, monkeypatch):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"controlled-runtime")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("PRIVATE_API_KEY", "must-not-leak")
    def runner(argv, **kwargs):
        assert {key.upper(): value for key, value in kwargs["env"].items()}["SYSTEMROOT"] == r"C:\Windows"
        assert "PRIVATE_API_KEY" not in kwargs["env"]
        assert "PYTHONHOME" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(dict(profile.constraints["python"])).encode(), b"")
    assert runtime._probe_selected_python(executable, profile, runner)["abi"] == ""


def test_record_permits_only_hashed_prefix_scripts_not_arbitrary_parent_files(tmp_path):
    site = tmp_path / "Lib" / "site-packages"
    info = site / "alpha-1.dist-info"
    info.mkdir(parents=True)
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    tool = scripts / "alpha.exe"
    tool.write_bytes(b"entrypoint")
    encoded = base64.urlsafe_b64encode(sha256(tool.read_bytes()).digest()).rstrip(b"=").decode()
    (info / "RECORD").write_text(f"../../Scripts/alpha.exe,sha256={encoded},10\n", encoding="utf-8")
    assert runtime._validate_distribution_records(site, runtime_root=tmp_path) == 1
    assert_reason("desktop_runtime_record_missing", lambda: runtime._validate_distribution_records(site))
    (tmp_path / "unexpected.exe").write_bytes(b"entrypoint")
    (info / "RECORD").write_text(f"../../unexpected.exe,sha256={encoded},10\n", encoding="utf-8")
    assert_reason("desktop_runtime_record_missing", lambda: runtime._validate_distribution_records(site, runtime_root=tmp_path))
