from __future__ import annotations

import json
import subprocess
from pathlib import Path

from framework.shared.local_desktop import CapabilityState, ProbeBudget, resolve_local_desktop_config
from scripts.local_desktop.doctor import LocalDesktopDoctor, _research_parser_contract


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
