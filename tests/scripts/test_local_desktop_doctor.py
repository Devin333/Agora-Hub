from __future__ import annotations

import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from framework.shared.local_desktop import CapabilityProjection, CapabilityState, ProbeBudget, resolve_local_desktop_config
from scripts.local_desktop.common import windows_extended_path
from scripts.local_desktop.doctor import DoctorCheck, LocalDesktopDoctor, _aggregate_status, _research_parser_contract
from scripts.local_desktop.release import ACTIVE_RELEASE_SCHEMA, RELEASE_SCHEMA


def _project(tmp_path: Path) -> tuple[Path, object]:
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_bytes(b"python")
    (tmp_path / "frontend" / "node_modules").mkdir(parents=True)
    (tmp_path / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "frontend" / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "frontend" / "node_modules" / ".package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "sources.yaml").write_text("sources: []", encoding="utf-8")
    return tmp_path, resolve_local_desktop_config(project_root=tmp_path)


def _write_windows_path(path: Path, data: bytes) -> None:
    io_path = windows_extended_path(path)
    io_path.parent.mkdir(parents=True, exist_ok=True)
    with open(io_path, "wb") as handle:
        handle.write(data)


def _prepare_active_release(tmp_path: Path) -> tuple[LocalDesktopDoctor, Path, Path]:
    _, config = _project(tmp_path)
    release_id = "20261014T160620Z-b2d933ba0487-portable-860f93d1871e-desktop-54afd7ef8932"
    release = config.release_root / release_id
    long_relative = Path(
        "frontend/node_modules/@radix-ui/react-dismissable-layer/node_modules/"
        "@radix-ui/primitive/dist/internal/is-development.false.d.mts"
    )
    resources = {
        Path("app/interfaces/__init__.py"): b"",
        Path("frontend/.next/BUILD_ID"): b"doctor-long-path-build",
        Path("frontend/node_modules/next/dist/bin/next"): b"next-runtime\n",
        long_relative: b"export type IsDevelopment = false;\n",
    }
    for relative, data in resources.items():
        _write_windows_path(release / relative, data)
    manifest = {
        "schema": RELEASE_SCHEMA,
        "identity": {"release_id": release_id},
        "resources": [
            {"path": relative.as_posix(), "size": len(data), "sha256": sha256(data).hexdigest()}
            for relative, data in sorted(resources.items(), key=lambda item: item[0].as_posix())
        ],
    }
    _write_windows_path(release / "release-manifest.json", json.dumps(manifest).encode("utf-8"))
    pointer = {
        "schema": ACTIVE_RELEASE_SCHEMA,
        "release_id": release_id,
        "release_path": str(release),
    }
    _write_windows_path(config.runtime_root / "active-release.json", json.dumps(pointer).encode("utf-8"))
    doctor = LocalDesktopDoctor(
        config,
        environ={},
        command_runner=lambda _argv, _timeout: None,
        port_checker=lambda _host, _port: False,
    )
    return doctor, release, long_relative


def _create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"Windows junction creation unavailable: {result.stderr or result.stdout}")


def test_doctor_default_never_runs_live_probe(tmp_path: Path, monkeypatch) -> None:
    _, config = _project(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float):
        calls.append(argv)
        raise AssertionError("offline doctor must not invoke live commands")

    monkeypatch.setattr("scripts.local_desktop.doctor.sys.executable", str(config.python_executable))
    monkeypatch.setattr("scripts.local_desktop.doctor.shutil.which", lambda name: f"C:/tools/{name}.exe")
    report = LocalDesktopDoctor(config, environ={}, command_runner=runner, port_checker=lambda _h, _p: False).run()

    assert calls == []
    assert all(item.verified is CapabilityState.UNKNOWN for item in report.capabilities)


def test_network_probe_marks_transport_reachable_but_business_capabilities_unverified(tmp_path: Path, monkeypatch) -> None:
    _, config = _project(tmp_path)
    outputs = iter(
        [
            {"reachable": True, "checked_count": 1},
            {"reachable": True, "model": "provider-model"},
        ]
    )

    def runner(argv: list[str], timeout: float):
        payload = next(outputs)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("scripts.local_desktop.doctor.sys.executable", str(config.python_executable))
    monkeypatch.setattr("scripts.local_desktop.doctor.shutil.which", lambda name: f"C:/tools/{name}.exe")
    report = LocalDesktopDoctor(
        config,
        environ={"NEWSROOM_DESKTOP_PROBE_MAX_COST_PER_1K_TOKENS_USD": "0.01", "DASHSCOPE_API_KEY": "secret"},
        command_runner=runner,
        port_checker=lambda _h, _p: False,
    ).run(probe_network=True, budget=ProbeBudget(max_requests=3, max_cost_usd=0.01))

    by_id = {item.capability_id: item for item in report.capabilities}
    assert by_id["real_source"].reachable is CapabilityState.YES
    assert by_id["real_source"].verified is CapabilityState.UNKNOWN
    assert by_id["single_paper_analysis"].reachable is CapabilityState.YES
    assert by_id["single_paper_analysis"].verified is CapabilityState.UNKNOWN
    assert by_id["paper_question_answering"].capability_id == "paper_question_answering"


def test_llm_probe_refuses_unknown_cost_instead_of_spending(tmp_path: Path, monkeypatch) -> None:
    _, config = _project(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"reachable": true}', stderr="")

    monkeypatch.setattr("scripts.local_desktop.doctor.sys.executable", str(config.python_executable))
    monkeypatch.setattr("scripts.local_desktop.doctor.shutil.which", lambda name: f"C:/tools/{name}.exe")
    report = LocalDesktopDoctor(config, environ={}, command_runner=runner, port_checker=lambda _h, _p: False).run(
        probe_network=True,
        budget=ProbeBudget(max_requests=3),
    )

    assert len(calls) == 1
    analysis = next(item for item in report.capabilities if item.capability_id == "single_paper_analysis")
    assert analysis.reachable is CapabilityState.UNKNOWN
    assert analysis.reason_code == "desktop_probe_cost_budget_unverifiable"



def test_doctor_projects_invalid_research_settings_as_stable_capability_failure(tmp_path: Path, monkeypatch) -> None:
    from interfaces.composition.research_errors import ResearchConfigurationError

    _, config = _project(tmp_path)
    doctor = LocalDesktopDoctor(config, environ={}, command_runner=lambda _argv, _timeout: None, port_checker=lambda _h, _p: False)
    monkeypatch.setattr(doctor, "_research_settings", lambda: (_ for _ in ()).throw(ResearchConfigurationError(("research.llm.route",))))

    report = doctor.run()
    by_id = {item.capability_id: item for item in report.capabilities}
    assert by_id["real_source"].configured is CapabilityState.NO
    assert by_id["real_source"].reason_code == "desktop_research_config_invalid"
    assert by_id["single_paper_analysis"].reason_code == "desktop_research_config_invalid"


def test_doctor_treats_invalid_llm_route_as_unconfigured_instead_of_crashing(tmp_path: Path, monkeypatch) -> None:
    from framework.llm import LLMConfigurationError

    _, config = _project(tmp_path)
    doctor = LocalDesktopDoctor(config, environ={"MODEL_API_KEY": "secret"}, command_runner=lambda _argv, _timeout: None, port_checker=lambda _h, _p: False)
    settings = type("Settings", (), {"llm": type("LLM", (), {"models_config_path": tmp_path / "models.yaml", "route_id": "missing", "model": "model", "base_url": "https://provider.invalid/v1"})()})()
    monkeypatch.setattr("framework.llm.load_openai_compatible_deployment", lambda *_args, **_kwargs: (_ for _ in ()).throw(LLMConfigurationError("invalid route")))

    assert doctor._llm_configured(settings) is False



def test_parser_contract_matches_registered_research_execution_profiles() -> None:
    from interfaces.composition.runtime_execution import RESEARCH_MARKER_PROFILE_ID, RESEARCH_MINERU_PROFILE_ID

    marker = _research_parser_contract("marker", {"NEWSROOM_MARKER_DOCKER_IMAGE": "marker@sha256:abc", "NEWSROOM_MARKER_CACHE_DIR": "C:/cache/marker"})
    mineru = _research_parser_contract("mineru", {"NEWSROOM_MINERU_DOCKER_IMAGE": "mineru@sha256:def", "NEWSROOM_MINERU_MODEL_SOURCE": "modelscope", "NEWSROOM_MINERU_CACHE_DIR": "C:/cache/mineru", "NEWSROOM_MINERU_CONFIG_DIR": "C:/config/mineru"})

    assert marker == {
        "parser_mode": "marker",
        "profile_id": RESEARCH_MARKER_PROFILE_ID,
        "provider_id": "docker",
        "network_policy": "deny",
        "allowed_argv_prefix": ["marker_single"],
        "image": "marker@sha256:abc",
        "model_source": "huggingface-cache",
        "cache_configured": True,
        "runtime_owner": "ResearchParserExecutionAdapter",
    }
    assert mineru["profile_id"] == RESEARCH_MINERU_PROFILE_ID
    assert mineru["provider_id"] == "docker"
    assert mineru["network_policy"] == "deny"
    assert mineru["allowed_argv_prefix"] == ["mineru"]
    assert mineru["cache_configured"] is True
    assert mineru["config_configured"] is True


def test_pymupdf_mode_does_not_require_docker_check(tmp_path: Path, monkeypatch) -> None:
    _, config = _project(tmp_path)
    calls: list[list[str]] = []
    doctor = LocalDesktopDoctor(config, environ={}, command_runner=lambda argv, _timeout: calls.append(argv), port_checker=lambda _h, _p: False)
    monkeypatch.setattr("scripts.local_desktop.doctor.shutil.which", lambda name: None if name.startswith("docker") else f"C:/tools/{name}.exe")

    report = doctor.run()

    assert all(check.check_id != "docker" for check in report.checks)
    assert calls == []



@pytest.mark.skipif(os.name != "nt", reason="requires Windows extended-length paths")
def test_doctor_accepts_active_release_resource_beyond_max_path_with_exact_checksum(tmp_path: Path) -> None:
    doctor, release, long_relative = _prepare_active_release(tmp_path)
    resource = release / long_relative

    assert len(os.fspath(resource.absolute())) > 260
    assert windows_extended_path(resource).is_file()

    check = doctor._release_check()

    assert check.status == "ok"
    assert check.reason_code == "desktop_active_release_prepared"
    assert check.details == {"release_id": release.name}
    assert str(windows_extended_path(resource)) not in json.dumps(check.to_dict())


@pytest.mark.skipif(os.name != "nt", reason="requires Windows extended-length paths")
def test_doctor_rejects_corrupt_active_release_resource_beyond_max_path(tmp_path: Path) -> None:
    doctor, release, long_relative = _prepare_active_release(tmp_path)
    resource_io = windows_extended_path(release / long_relative)
    original = resource_io.read_bytes()
    resource_io.write_bytes(b"X" + original[1:])

    check = doctor._release_check()

    assert check.status == "error"
    assert check.reason_code == "desktop_active_release_invalid"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows extended-length paths")
def test_doctor_rejects_missing_active_release_resource_beyond_max_path(tmp_path: Path) -> None:
    doctor, release, long_relative = _prepare_active_release(tmp_path)
    windows_extended_path(release / long_relative).unlink()

    check = doctor._release_check()

    assert check.status == "error"
    assert check.reason_code == "desktop_active_release_invalid"


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_doctor_rejects_junction_inside_active_release(tmp_path: Path) -> None:
    doctor, release, _long_relative = _prepare_active_release(tmp_path)
    interfaces = release / "app" / "interfaces"
    windows_extended_path(interfaces / "__init__.py").unlink()
    windows_extended_path(interfaces).rmdir()
    target = tmp_path / "doctor-junction-target"
    target.mkdir()
    (target / "__init__.py").write_bytes(b"")
    _create_junction(interfaces, target)
    try:
        check = doctor._release_check()

        assert check.status == "error"
        assert check.reason_code == "desktop_active_release_invalid"
        assert (target / "__init__.py").read_bytes() == b""
    finally:
        if os.path.lexists(interfaces):
            os.rmdir(interfaces)


def test_doctor_does_not_treat_workspace_next_directory_as_prepared_release(tmp_path: Path, monkeypatch) -> None:
    _, config = _project(tmp_path)
    (config.frontend_root / ".next").mkdir()
    monkeypatch.setattr("scripts.local_desktop.doctor.sys.executable", str(config.python_executable))
    monkeypatch.setattr("scripts.local_desktop.doctor.shutil.which", lambda name: f"C:/tools/{name}.exe")

    report = LocalDesktopDoctor(config, environ={}, command_runner=lambda _argv, _timeout: subprocess.CompletedProcess([], 1, "", ""), port_checker=lambda _h, _p: False).run()

    release_check = next(item for item in report.checks if item.check_id == "production_release")
    assert release_check.status == "warning"
    assert release_check.reason_code == "desktop_release_not_prepared"


def test_doctor_reports_corrupt_active_release_pointer_with_stable_reason(tmp_path: Path, monkeypatch) -> None:
    _, config = _project(tmp_path)
    config.runtime_root.mkdir(parents=True)
    (config.runtime_root / "active-release.json").write_text('{"schema":"wrong"}', encoding="utf-8")
    monkeypatch.setattr("scripts.local_desktop.doctor.sys.executable", str(config.python_executable))
    monkeypatch.setattr("scripts.local_desktop.doctor.shutil.which", lambda name: f"C:/tools/{name}.exe")

    report = LocalDesktopDoctor(config, environ={}, command_runner=lambda _argv, _timeout: subprocess.CompletedProcess([], 1, "", ""), port_checker=lambda _h, _p: False).run()

    release_check = next(item for item in report.checks if item.check_id == "production_release")
    assert release_check.status == "error"
    assert release_check.reason_code == "desktop_active_release_invalid"


def test_owned_local_doctor_uses_effective_environment_and_reports_first_start_warning(tmp_path: Path) -> None:
    _project(tmp_path)
    config = resolve_local_desktop_config(
        project_root=tmp_path,
        runtime_config={"storage_mode": "owned-local"},
    )
    doctor = LocalDesktopDoctor(
        config,
        environ={"NEWS_DATABASE_DSN": "postgresql://local.invalid/newsroom"},
        command_runner=lambda _argv, _timeout: None,
        port_checker=lambda _host, _port: False,
    )

    check = doctor._storage_check()

    assert doctor.environ["NEWS_DATABASE_DSN"] == ""
    assert check.status == "warning"
    assert check.reason_code == "desktop_storage_uninitialized"
    assert check.details["storage_mode"] == "owned-local"
    assert check.details["root_database_dsn_shadowed"] is True
    assert {"auth_users", "research_history", "research_imports", "research_run_store", "desktop_control"} <= set(
        check.details["missing_owner_ids"]
    )


def test_root_environment_doctor_keeps_external_database_gate(tmp_path: Path) -> None:
    _project(tmp_path)
    config = resolve_local_desktop_config(project_root=tmp_path)
    doctor = LocalDesktopDoctor(
        config,
        environ={"NEWS_DATABASE_DSN": "postgresql://local.invalid/newsroom"},
        command_runner=lambda _argv, _timeout: None,
        port_checker=lambda _host, _port: False,
    )

    check = doctor._storage_check()

    assert doctor.environ["NEWS_DATABASE_DSN"] == "postgresql://local.invalid/newsroom"
    assert check.status == "error"
    assert check.reason_code == "desktop_inventory_external_database_unsupported"

def test_base_doctor_profile_preserves_required_business_capability_gate() -> None:
    checks = [DoctorCheck("local", "ok", "desktop_local_ready", "local prerequisites are ready")]
    capabilities = (
        CapabilityProjection(
            "single_paper_analysis",
            True,
            CapabilityState.NO,
            reason_code="desktop_llm_not_configured",
        ),
    )

    assert _aggregate_status(checks, capabilities) == "error"
    assert (
        _aggregate_status(checks, capabilities, startup_required_capability_ids=frozenset({"pdf_import"}))
        == "warning"
    )
