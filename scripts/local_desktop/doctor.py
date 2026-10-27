from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from framework.shared.hashing import hash_bytes
from framework.shared.local_desktop import (
    CapabilityProjection,
    CapabilityState,
    LocalDesktopConfig,
    ProbeBudget,
    utc_timestamp,
)
from framework.shared.redaction import redact_sensitive_values
from scripts.local_desktop.config import load_root_environment, sanitized_environment
from interfaces.composition.research_errors import ResearchConfigurationError, ResearchRuntimeUnavailableError
from framework.llm import LLMConfigurationError


class CommandRunner(Protocol):
    def __call__(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class DoctorCheck:
    check_id: str
    status: str
    reason_code: str
    message: str
    remediation: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "message": self.message,
            "remediation": self.remediation,
            "details": _redact_doctor_details(self.details or {}),
        }


@dataclass(frozen=True)
class DoctorReport:
    status: str
    checks: tuple[DoctorCheck, ...]
    capabilities: tuple[CapabilityProjection, ...]
    probe_budget: ProbeBudget | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "newsroom.local-desktop-doctor/v1",
            "status": self.status,
            "checked_at": utc_timestamp(),
            "checks": [item.to_dict() for item in self.checks],
            "capabilities": [item.to_dict() for item in self.capabilities],
            "probe_budget": self.probe_budget.to_dict() if self.probe_budget else None,
        }


@dataclass(frozen=True)
class _ActiveReleaseInspection:
    state: str
    path: Path | None = None
    manifest: Mapping[str, Any] | None = None


class LocalDesktopDoctor:
    def __init__(
        self,
        config: LocalDesktopConfig,
        *,
        environ: Mapping[str, str] | None = None,
        command_runner: CommandRunner | None = None,
        port_checker: Callable[[str, int], bool] | None = None,
        startup_required_capability_ids: frozenset[str] | None = None,
    ) -> None:
        self.config = config
        raw_environment = load_root_environment(environ=environ)
        self.environ = sanitized_environment(config, base=raw_environment)
        self.root_database_dsn_shadowed = (
            self.environ.get("NEWSROOM_DESKTOP_ROOT_DATABASE_DSN_SHADOWED") == "1"
        )
        if command_runner is None:
            self.command_runner = lambda argv, timeout: _run_command(argv, timeout, env=self.environ)
        else:
            self.command_runner = command_runner
        self.port_checker = port_checker or _port_is_listening
        self.startup_required_capability_ids = startup_required_capability_ids

    def run(
        self,
        *,
        probe_network: bool = False,
        budget: ProbeBudget | None = None,
        verified_release: tuple[Path, Mapping[str, Any]] | None = None,
    ) -> DoctorReport:
        """Run the offline checks once.

        ``verified_release`` is the active release a caller already verified in the
        same operation (start verifies it under the instance lock right before the
        doctor runs). Passing it avoids hashing every release resource a second time;
        callers must never pass a result retained from an earlier operation.
        """
        actual_budget = budget or ProbeBudget()
        active_release = (
            _ActiveReleaseInspection("verified", path=verified_release[0], manifest=verified_release[1])
            if verified_release is not None
            else self._inspect_active_release()
        )
        checks = [
            self._profile_check(),
            self._python_check(),
            self._node_check(),
            self._frontend_lock_check(active_release),
            self._release_check(active_release),
            self._storage_check(),
            self._port_check("frontend_port", self.config.frontend_host, self.config.frontend_port),
            self._port_check("api_port", self.config.api_host, self.config.api_port),
        ]
        capabilities = self._offline_capabilities()
        if self.config.parser_mode == "marker":
            docker_check, parser_capability = self._docker_check(capabilities)
            checks.append(docker_check)
            capabilities = tuple(parser_capability if item.capability_id == "pdf_import" else item for item in capabilities)
        if probe_network:
            capabilities = self._probe_network(capabilities, actual_budget)
        status = _aggregate_status(
            checks,
            capabilities,
            startup_required_capability_ids=self.startup_required_capability_ids,
        )
        return DoctorReport(
            status=status,
            checks=tuple(checks),
            capabilities=tuple(capabilities),
            probe_budget=actual_budget if probe_network else None,
        )

    def _profile_check(self) -> DoctorCheck:
        return DoctorCheck(
            check_id="deployment_profile",
            status="ok",
            reason_code="desktop_profile_valid",
            message="local-desktop profile is constrained to the configured loopback origins",
            details={
                "profile": self.config.deployment_profile,
                "canonical_origin": self.config.canonical_origin,
                "api_base_url": self.config.api_base_url,
            },
        )

    def _python_check(self) -> DoctorCheck:
        expected = self.config.python_executable.resolve(strict=False)
        actual = Path(sys.executable).resolve(strict=False)
        if not expected.is_file():
            return DoctorCheck(
                "python",
                "error",
                "desktop_project_python_missing",
                "project virtual environment interpreter is missing",
                "Create the project .venv and install the pinned project dependencies before build/start.",
                {"expected": str(expected)},
            )
        if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
            return DoctorCheck(
                "python",
                "error",
                "desktop_wrong_python",
                "local desktop CLI is not running with the project interpreter",
                "Use scripts/local-desktop.ps1 so .venv/Scripts/python.exe is selected explicitly.",
                {"expected": str(expected), "actual": str(actual)},
            )
        return DoctorCheck("python", "ok", "desktop_project_python_verified", "project virtual environment interpreter is active", details={"path": str(expected), "version": sys.version.split()[0]})

    def _node_check(self) -> DoctorCheck:
        node = shutil.which("node")
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if not node or not npm:
            return DoctorCheck("node", "error", "desktop_node_missing", "Node.js or npm is unavailable", "Install the project-supported Node.js and rerun doctor.", {"node_found": bool(node), "npm_found": bool(npm)})
        return DoctorCheck("node", "ok", "desktop_node_configured", "Node.js and npm executables are available", details={"node": node, "npm": npm})

    def _inspect_active_release(self) -> _ActiveReleaseInspection:
        from scripts.local_desktop.common import DesktopOperationError
        from scripts.local_desktop.release import ReleaseManager

        try:
            path, manifest = ReleaseManager(self.config).active()
        except DesktopOperationError as exc:
            if exc.reason_code == "desktop_release_not_activated":
                return _ActiveReleaseInspection("absent")
            return _ActiveReleaseInspection("invalid")
        except (KeyError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return _ActiveReleaseInspection("invalid")
        return _ActiveReleaseInspection("verified", path=path, manifest=manifest)

    def _verified_frontend_mode(self, active_release: _ActiveReleaseInspection) -> str | None:
        if active_release.state != "verified" or not isinstance(active_release.manifest, Mapping):
            return None
        entrypoints = active_release.manifest.get("entrypoints")
        if not isinstance(entrypoints, Mapping):
            return None
        frontend = entrypoints.get("frontend")
        if frontend == ["node", "server.js"]:
            return "next-standalone"
        if frontend == ["next", "start", "-H", self.config.frontend_host, "-p", str(self.config.frontend_port)]:
            return "next-start"
        return None

    def _frontend_lock_check(self, active_release: _ActiveReleaseInspection | None = None) -> DoctorCheck:
        inspected = active_release or self._inspect_active_release()
        if inspected.state == "invalid":
            return DoctorCheck(
                "frontend_lock",
                "error",
                "desktop_active_release_invalid",
                "the active production release could not be verified",
                "Run the explicit build command to prepare and activate a fresh release.",
            )
        if inspected.state == "verified":
            mode = self._verified_frontend_mode(inspected)
            if mode == "next-standalone":
                release_id = str(inspected.manifest.get("identity", {}).get("release_id") or "")
                return DoctorCheck(
                    "frontend_lock",
                    "ok",
                    "desktop_frontend_standalone_verified",
                    "active standalone frontend dependency closure is verified",
                    details={"release_id": release_id, "runtime_mode": mode},
                )
            if mode is None:
                return DoctorCheck(
                    "frontend_lock",
                    "error",
                    "desktop_active_release_invalid",
                    "the active production release frontend mode is unsupported",
                    "Run the explicit build command to prepare and activate a fresh release.",
                )

        lockfile = self.config.frontend_root / "package-lock.json"
        package = self.config.frontend_root / "package.json"
        if not lockfile.is_file() or not package.is_file():
            return DoctorCheck("frontend_lock", "error", "desktop_frontend_lock_missing", "frontend package manifest or lockfile is missing", "Restore frontend/package.json and frontend/package-lock.json before building.")
        installed_lock = self.config.frontend_root / "node_modules" / ".package-lock.json"
        return DoctorCheck(
            "frontend_lock",
            "ok" if installed_lock.is_file() else "warning",
            "desktop_frontend_dependencies_present" if installed_lock.is_file() else "desktop_frontend_dependencies_unprepared",
            "frontend lockfile is present" if installed_lock.is_file() else "frontend lockfile exists but installed dependency identity is unavailable",
            None if installed_lock.is_file() else "Run the explicit local desktop build command; normal start never installs dependencies.",
            {"lock_sha256": hash_bytes(lockfile.read_bytes()), "installed_lock_present": installed_lock.is_file()},
        )

    def _release_check(self, active_release: _ActiveReleaseInspection | None = None) -> DoctorCheck:
        inspected = active_release or self._inspect_active_release()
        if inspected.state == "absent":
            return DoctorCheck(
                "production_release",
                "warning",
                "desktop_release_not_prepared",
                "no active production release is recorded",
                "Run the explicit build command before start.",
            )
        mode = self._verified_frontend_mode(inspected)
        if inspected.state != "verified" or mode is None or not isinstance(inspected.manifest, Mapping):
            return DoctorCheck(
                "production_release",
                "error",
                "desktop_active_release_invalid",
                "the active production release manifest or resources are incomplete",
                "Run the explicit build command to prepare and activate a fresh release.",
            )
        release_id = str(inspected.manifest.get("identity", {}).get("release_id") or "")
        return DoctorCheck(
            "production_release",
            "ok",
            "desktop_active_release_prepared",
            "active production release resources match their manifest",
            details={"release_id": release_id, "runtime_mode": mode},
        )

    def _storage_check(self) -> DoctorCheck:
        from scripts.local_desktop.common import DesktopOperationError
        from scripts.local_desktop.recovery import StorageInventoryResolver

        roots = (self.config.runtime_root, self.config.release_root, self.config.backup_root)
        unavailable = [str(path) for path in roots if path.exists() and (not path.is_dir() or not os.access(path, os.R_OK | os.W_OK))]
        existing_parent = next((parent for parent in self.config.runtime_root.parents if parent.exists()), self.config.project_root)
        free = shutil.disk_usage(existing_parent).free
        try:
            inventory = StorageInventoryResolver(self.config, environ=self.environ).resolve(require_existing=False)
        except DesktopOperationError as exc:
            return DoctorCheck("storage", "error", exc.reason_code, "configured application storage cannot be safely owned by local desktop", "Use supported local storage roots before start, backup, or restore.", exc.details)
        missing_required = [owner.owner_id for owner in inventory.owners if owner.required and not owner.path.exists()]
        owner_unavailable = [str(owner.path) for owner in inventory.owners if owner.path.exists() and not os.access(owner.path, os.R_OK | os.W_OK)]
        unavailable.extend(owner_unavailable)
        if unavailable:
            return DoctorCheck("storage", "error", "desktop_storage_unavailable", "one or more desktop roots are not usable", "Correct the configured path or its current-user permissions.", {"paths": unavailable, "free_bytes": free})
        if missing_required:
            return DoctorCheck("storage", "warning", "desktop_storage_uninitialized", "required application storage owners are not initialized yet", "Start only after configuration is reviewed; backup remains unavailable until real stores exist.", {"missing_owner_ids": missing_required, "owner_count": len(inventory.owners), "free_bytes": free, "storage_mode": self.config.storage_mode, "root_database_dsn_shadowed": self.root_database_dsn_shadowed})
        if free < 2 * 1024 * 1024 * 1024:
            return DoctorCheck("storage", "warning", "desktop_disk_space_low", "less than 2 GiB is available for release and backup operations", "Free disk space before build or backup.", {"free_bytes": free, "owner_count": len(inventory.owners)})
        return DoctorCheck("storage", "ok", "desktop_storage_ready", "desktop and application storage roots are usable", details={"free_bytes": free, "owner_count": len(inventory.owners), "storage_mode": self.config.storage_mode, "root_database_dsn_shadowed": self.root_database_dsn_shadowed})

    def _port_check(self, check_id: str, host: str, port: int) -> DoctorCheck:
        occupied = self.port_checker(host, port)
        return DoctorCheck(
            check_id,
            "warning" if occupied else "ok",
            "desktop_port_occupied" if occupied else "desktop_port_available",
            f"{host}:{port} is already accepting connections" if occupied else f"{host}:{port} is available",
            "Stop the owning application or use an explicit consistent desktop configuration; start will not kill or silently change ports." if occupied else None,
            {"host": host, "port": port, "occupied": occupied},
        )

    def _offline_capabilities(self) -> tuple[CapabilityProjection, ...]:
        checked = utc_timestamp()
        settings_reason: str | None = None
        try:
            runtime_settings = self._research_settings()
        except ResearchConfigurationError:
            runtime_settings = None
            settings_reason = "desktop_research_config_invalid"
        except ResearchRuntimeUnavailableError:
            runtime_settings = None
            settings_reason = "desktop_research_runtime_unavailable"
        except (ImportError, OSError, TypeError, ValueError):
            runtime_settings = None
            settings_reason = "desktop_research_config_unreadable"
        source_configured = runtime_settings is not None and runtime_settings.source.provider == "arxiv" and bool(runtime_settings.source.api_url)
        artifact_root = runtime_settings.artifact.root if runtime_settings is not None else self.config.project_root / ".newsroom" / "runs"
        artifact_parent = next((parent for parent in (artifact_root, *artifact_root.parents) if parent.exists()), self.config.project_root)
        artifact_configured = artifact_parent.is_dir() and os.access(artifact_parent, os.R_OK | os.W_OK)
        parser_installed = importlib.util.find_spec("pymupdf") is not None
        llm = runtime_settings is not None and self._llm_configured(runtime_settings)
        return (
            CapabilityProjection("real_source", True, _state(source_configured), reason_code=None if source_configured else (settings_reason or "desktop_source_config_missing"), checked_at=checked, remediation=(() if source_configured else ("Correct the Research source settings resolved by ResearchRuntimeSettings.",))),
            CapabilityProjection("pdf_import", True, _state(parser_installed if self.config.parser_mode == "pymupdf" else True), reachable=_state(parser_installed) if self.config.parser_mode == "pymupdf" else CapabilityState.UNKNOWN, verified=CapabilityState.UNKNOWN, reason_code=None if parser_installed or self.config.parser_mode != "pymupdf" else "desktop_pdf_parser_missing", checked_at=checked, remediation=(() if parser_installed or self.config.parser_mode != "pymupdf" else ("Install the selected PDF parser in the project .venv.",)), details={"parser_mode": self.config.parser_mode}),
            CapabilityProjection("single_paper_analysis", True, _state(llm), reason_code=None if llm else (settings_reason or "desktop_llm_not_configured"), checked_at=checked, remediation=(() if llm else ("Configure a model route and its referenced API key environment variable.",))),
            CapabilityProjection("paper_question_answering", True, _state(llm), reason_code=None if llm else (settings_reason or "desktop_llm_not_configured"), checked_at=checked, remediation=(() if llm else ("Configure a model route and its referenced API key environment variable.",))),
            CapabilityProjection("artifact_storage", True, _state(artifact_configured), reachable=_state(artifact_configured), verified=CapabilityState.UNKNOWN, reason_code=None if artifact_configured else "desktop_artifact_storage_unavailable", checked_at=checked, details={"root": str(artifact_root)}),
            CapabilityProjection("ocr", False, _state(self.config.parser_mode == "marker"), reason_code="desktop_optional_capability_not_selected" if self.config.parser_mode == "pymupdf" else None, checked_at=checked),
            CapabilityProjection("dense_retrieval", False, _state(bool(self.environ.get("NEWS_QDRANT_URL"))), reason_code=None if self.environ.get("NEWS_QDRANT_URL") else "desktop_optional_capability_not_configured", checked_at=checked),
        )

    def _research_settings(self):
        from interfaces.composition.research_settings import ResearchRuntimeSettings

        return ResearchRuntimeSettings.from_env(self.environ, cwd=self.config.project_root)

    def _llm_configured(self, settings) -> bool:
        try:
            from framework.llm import load_openai_compatible_deployment

            deployment = load_openai_compatible_deployment(settings.llm.models_config_path, route_id=settings.llm.route_id, apply_environment_overrides=False)
            return (
                deployment.config.model == settings.llm.model
                and deployment.config.base_url.rstrip("/") == settings.llm.base_url.rstrip("/")
                and bool(self.environ.get(deployment.config.api_key_env))
            )
        except (LLMConfigurationError, ImportError, OSError, RuntimeError, TypeError, ValueError):
            return False

    def _docker_check(self, capabilities: tuple[CapabilityProjection, ...]) -> tuple[DoctorCheck, CapabilityProjection]:
        docker = shutil.which("docker.exe") or shutil.which("docker")
        if not docker:
            return (
                DoctorCheck("docker", "error", "desktop_docker_cli_missing", "selected parser requires Docker but the CLI is missing", "Install Docker Desktop and explicitly prepare the selected parser image."),
                CapabilityProjection("pdf_import", True, CapabilityState.YES, CapabilityState.NO, CapabilityState.NO, "desktop_docker_cli_missing", utc_timestamp(), ("Install Docker Desktop and prepare the selected parser image.",), {"parser_mode": self.config.parser_mode}),
            )
        try:
            result = self.command_runner([docker, "version", "--format", "{{.Server.Version}}"], 5.0)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        reachable = result is not None and result.returncode == 0 and bool(result.stdout.strip())
        contract = _research_parser_contract(self.config.parser_mode, self.environ)
        image = contract["image"]
        image_result = None
        if reachable:
            try:
                image_result = self.command_runner([docker, "image", "inspect", "--format", "{{.Id}}", image], 5.0)
            except (OSError, subprocess.TimeoutExpired):
                image_result = None
        image_id = image_result.stdout.strip() if image_result is not None and image_result.returncode == 0 else ""
        prepared = reachable and bool(image_id)
        reason = None if prepared else ("desktop_parser_image_missing" if reachable else "desktop_docker_engine_unreachable")
        return (
            DoctorCheck(
                "docker",
                "ok" if prepared else "error",
                reason or "desktop_parser_image_prepared",
                "selected parser image is locally prepared" if prepared else ("selected parser image is not locally prepared" if reachable else "Docker CLI exists but the engine is unreachable"),
                None if prepared else "Prepare the configured parser image explicitly; start never downloads images.",
                {**contract, "image_id": image_id or None},
            ),
            CapabilityProjection(
                "pdf_import",
                True,
                CapabilityState.YES,
                _state(prepared),
                CapabilityState.UNKNOWN,
                reason,
                utc_timestamp(),
                (() if prepared else ("Prepare the selected parser image and rerun doctor.",)),
                {**contract, "image_id": image_id or None},
            ),
        )

    def _probe_network(self, capabilities: tuple[CapabilityProjection, ...], budget: ProbeBudget) -> tuple[CapabilityProjection, ...]:
        remaining = budget.max_requests
        replacements: dict[str, CapabilityProjection] = {}
        source_request_budget = 2
        if remaining >= source_request_budget:
            remaining -= source_request_budget
            replacements["real_source"] = self._probe_source(budget, request_budget=source_request_budget)
        else:
            replacements["real_source"] = CapabilityProjection(
                "real_source",
                True,
                CapabilityState.YES,
                CapabilityState.UNKNOWN,
                CapabilityState.UNKNOWN,
                "desktop_source_probe_request_budget_insufficient",
                utc_timestamp(),
                ("Allow two requests so the probe can enforce robots.txt and fetch one canonical source without retries.",),
                {"required_requests": source_request_budget, "max_requests": budget.max_requests},
            )
        if remaining > 0:
            llm_reachability = self._probe_llm(budget)
            for capability_id in ("single_paper_analysis", "paper_question_answering"):
                replacements[capability_id] = CapabilityProjection(
                    capability_id,
                    True,
                    llm_reachability.configured,
                    llm_reachability.reachable,
                    CapabilityState.UNKNOWN,
                    "desktop_business_capability_not_verified" if llm_reachability.reachable is CapabilityState.YES else llm_reachability.reason_code,
                    llm_reachability.checked_at,
                    ("Complete the real authenticated application flow to mark this capability verified.",),
                    dict(llm_reachability.details),
                )
        return tuple(replacements.get(item.capability_id, item) for item in capabilities)

    def _probe_source(self, budget: ProbeBudget, *, request_budget: int) -> CapabilityProjection:
        checked = utc_timestamp()
        argv = [
            str(self.config.python_executable),
            "-m",
            "scripts.local_desktop.probe",
            "source",
            "--timeout",
            str(budget.timeout_seconds),
            "--max-requests",
            str(request_budget),
        ]
        try:
            result = self.command_runner(argv, budget.timeout_seconds + 1.0)
            payload = json.loads(result.stdout or "{}")
            reachable = result.returncode == 0 and payload.get("reachable") is True
            return CapabilityProjection(
                "real_source",
                True,
                CapabilityState.YES,
                _state(reachable),
                CapabilityState.UNKNOWN,
                "desktop_source_reachable_not_verified" if reachable else "desktop_source_probe_failed",
                checked,
                ("Complete a real source search/import application flow to mark this capability verified.",) if reachable else ("Check source policy, network access, and the selected source configuration.",),
                {"checked_count": payload.get("checked_count", 0), "request_budget": request_budget},
            )
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            return CapabilityProjection("real_source", True, CapabilityState.YES, CapabilityState.NO, CapabilityState.UNKNOWN, "desktop_source_probe_budget_exhausted", checked, ("Check source network access or increase the explicit bounded probe timeout.",), {"error_type": type(exc).__name__, "timeout_seconds": budget.timeout_seconds, "request_budget": request_budget})

    def _probe_llm(self, budget: ProbeBudget) -> CapabilityProjection:
        checked = utc_timestamp()
        price_value = str(self.environ.get("NEWSROOM_DESKTOP_PROBE_MAX_COST_PER_1K_TOKENS_USD") or "").strip()
        try:
            price = float(price_value)
        except ValueError:
            price = -1.0
        maximum_tokens = budget.max_input_tokens + budget.max_output_tokens
        estimated_cost = maximum_tokens / 1000 * price if math.isfinite(price) and price >= 0 else None
        if estimated_cost is None or estimated_cost > budget.max_cost_usd:
            return CapabilityProjection(
                "llm_transport",
                True,
                CapabilityState.YES,
                CapabilityState.UNKNOWN,
                CapabilityState.UNKNOWN,
                "desktop_probe_cost_budget_unverifiable" if estimated_cost is None else "desktop_probe_cost_budget_exceeded",
                checked,
                ("Set NEWSROOM_DESKTOP_PROBE_MAX_COST_PER_1K_TOKENS_USD to a conservative provider price within the explicit cost budget.",),
                {"max_cost_usd": budget.max_cost_usd, "estimated_cost_usd": estimated_cost, "max_tokens": maximum_tokens},
            )
        argv = [
            str(self.config.python_executable),
            "-m",
            "scripts.local_desktop.probe",
            "llm",
            "--timeout",
            str(budget.timeout_seconds),
            "--max-input-tokens",
            str(budget.max_input_tokens),
            "--max-output-tokens",
            str(budget.max_output_tokens),
            "--max-cost-usd",
            str(budget.max_cost_usd),
        ]
        try:
            result = self.command_runner(argv, budget.timeout_seconds + 1.0)
            payload = json.loads(result.stdout or "{}")
            reachable = result.returncode == 0 and payload.get("reachable") is True
            return CapabilityProjection("llm_transport", True, CapabilityState.YES, _state(reachable), CapabilityState.UNKNOWN, "desktop_llm_reachable_not_business_verified" if reachable else "desktop_llm_probe_failed", checked, details={"estimated_cost_usd": estimated_cost, "request_budget": 1})
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            return CapabilityProjection("llm_transport", True, CapabilityState.YES, CapabilityState.NO, CapabilityState.UNKNOWN, "desktop_llm_probe_budget_exhausted", checked, ("Check the provider endpoint or increase the explicit bounded probe timeout.",), {"error_type": type(exc).__name__, "timeout_seconds": budget.timeout_seconds, "request_budget": 1})

def _research_parser_contract(parser_mode: str, environ: Mapping[str, str]) -> dict[str, Any]:
    from interfaces.composition.runtime_execution import RESEARCH_MARKER_PROFILE_ID

    if parser_mode == "marker":
        return {
            "parser_mode": parser_mode,
            "profile_id": RESEARCH_MARKER_PROFILE_ID,
            "provider_id": "docker",
            "network_policy": "deny",
            "allowed_argv_prefix": ["marker_single"],
            "image": str(environ.get("NEWSROOM_MARKER_DOCKER_IMAGE") or "newsroom-marker:latest").strip(),
            "model_source": "huggingface-cache",
            "cache_configured": bool(str(environ.get("NEWSROOM_MARKER_CACHE_DIR") or "").strip()),
            "runtime_owner": "ResearchParserExecutionAdapter",
        }
    raise ValueError(f"unsupported docker parser mode: {parser_mode}")


def _redact_doctor_details(value: Mapping[str, Any]) -> dict[str, Any]:
    sensitive = {"authorization", "cookie", "password", "secret", "credential", "api_key", "apikey"}
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).casefold().replace("-", "_")
        result[str(key)] = "***REDACTED***" if normalized in sensitive or normalized.endswith("_api_key") else redact_sensitive_values(item)
    return result


def _aggregate_status(
    checks: list[DoctorCheck],
    capabilities: tuple[CapabilityProjection, ...],
    *,
    startup_required_capability_ids: frozenset[str] | None = None,
) -> str:
    startup_capabilities = (
        tuple(item for item in capabilities if item.required)
        if startup_required_capability_ids is None
        else tuple(item for item in capabilities if item.capability_id in startup_required_capability_ids)
    )
    if any(item.status == "error" for item in checks) or any(
        item.configured is CapabilityState.NO for item in startup_capabilities
    ):
        return "error"
    if any(item.status == "warning" for item in checks) or any(
        item.required and not item.available for item in capabilities
    ):
        return "warning"
    return "ok"


def _state(value: bool) -> CapabilityState:
    return CapabilityState.YES if value else CapabilityState.NO


def _port_is_listening(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def _run_command(argv: list[str], timeout: float, *, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, shell=False, env=dict(env) if env is not None else None)


__all__ = ["DoctorCheck", "DoctorReport", "LocalDesktopDoctor"]
