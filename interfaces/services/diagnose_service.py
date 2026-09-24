from __future__ import annotations

import os
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from framework.llm import DEFAULT_MODELS_CONFIG_PATH, load_openai_compatible_deployment
from framework.llm.clients.openai_compatible import LLMConfigurationError
from framework.execution_environment.composition import RuntimeExecutionComposition
from framework.execution_environment.errors import ExecutionEnvironmentError
from backend.layers.signal.source_config import SourceConfigError, build_default_source_registry

_LLM_ENV_OVERRIDE_KEYS = (
    "NEWS_LLM_API_KEY",
    "NEWS_LLM_API_KEY_ENV",
    "NEWS_LLM_BASE_URL",
    "NEWS_LLM_MODEL",
    "NEWS_LLM_PROVIDER",
    "NEWS_LLM_PROVIDER_NAME",
    "NEWS_MODELS_CONFIG",
)


CheckStatus = Literal["ok", "warning", "error", "skipped"]
DiagnoseStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True)
class DiagnoseCheck:
    check_id: str
    name: str
    status: CheckStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class DiagnoseResult:
    status: DiagnoseStatus
    checks: list[DiagnoseCheck]
    summary: str

    @classmethod
    def from_checks(cls, checks: list[DiagnoseCheck]) -> "DiagnoseResult":
        if any(check.status == "error" for check in checks):
            status: DiagnoseStatus = "error"
        elif any(check.status == "warning" for check in checks):
            status = "warning"
        else:
            status = "ok"
        return cls(
            status=status,
            checks=checks,
            summary=f"{sum(1 for check in checks if check.status == 'ok')} ok, "
            f"{sum(1 for check in checks if check.status == 'warning')} warning, "
            f"{sum(1 for check in checks if check.status == 'error')} error, "
            f"{sum(1 for check in checks if check.status == 'skipped')} skipped",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "checks": [check.to_dict() for check in self.checks],
        }


class DiagnosticApplicationService:
    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        checks: list[Callable[[], DiagnoseCheck]] | None = None,
        runtime_execution_composition: RuntimeExecutionComposition | None = None,
    ) -> None:
        self.env = env if env is not None else os.environ
        if runtime_execution_composition is not None and not isinstance(
            runtime_execution_composition, RuntimeExecutionComposition
        ):
            raise TypeError(
                "runtime_execution_composition must be RuntimeExecutionComposition"
            )
        self.runtime_execution_composition = runtime_execution_composition
        self.checks = checks or [
            *([self._check_runtime_composition] if runtime_execution_composition else []),
            self._check_source_config,
            self._check_model_config,
            self._check_dashscope_key,
            self._check_redis,
            self._check_qdrant,
            self._check_postgres,
        ]

    def _check_runtime_composition(self) -> DiagnoseCheck:
        composition = self.runtime_execution_composition
        if composition is None:
            return DiagnoseCheck(
                check_id="runtime_composition",
                name="Runtime execution composition",
                status="error",
                message="Runtime execution composition is not configured.",
                details={"configured": False},
                remediation="Build the process-scoped RuntimeExecutionComposition at startup.",
            )
        try:
            diagnostics = composition.diagnostics()
        except ExecutionEnvironmentError as exc:
            return DiagnoseCheck(
                check_id="runtime_composition",
                name="Runtime execution composition",
                status="error",
                message="Runtime execution composition is not ready.",
                details={
                    "configured": True,
                    "reason_code": exc.reason_code,
                    "details": dict(exc.details),
                },
                remediation="Resolve the manifest/provider drift before starting the process.",
            )
        if diagnostics.get("status") != "ready":
            try:
                composition.require_ready()
            except ExecutionEnvironmentError as exc:
                return DiagnoseCheck(
                    check_id="runtime_composition",
                    name="Runtime execution composition",
                    status="error",
                    message="Runtime execution composition is not ready.",
                    details={
                        "configured": True,
                        "reason_code": exc.reason_code,
                        "details": dict(exc.details),
                        "unavailable_providers": diagnostics.get(
                            "unavailable_providers", []
                        ),
                        "deployment_capability_evidence_issues": diagnostics.get(
                            "deployment_capability_evidence_issues", []
                        ),
                    },
                    remediation=(
                        "Provision each role-required execution provider and resolve "
                        "deployment qualification evidence."
                    ),
                )
            return DiagnoseCheck(
                check_id="runtime_composition",
                name="Runtime execution composition",
                status="error",
                message="Runtime execution composition is not ready.",
                details={
                    "configured": True,
                    "manifest_fingerprint": diagnostics["manifest_fingerprint"],
                    "unavailable_providers": diagnostics.get(
                        "unavailable_providers", []
                    ),
                    "required_providers": diagnostics.get("required_providers", []),
                    "providers": diagnostics.get("providers", []),
                    "deployment_capability_evidence_issues": diagnostics.get(
                        "deployment_capability_evidence_issues", []
                    ),
                },
                remediation=(
                    "Provision each role-required execution provider and resolve "
                    "deployment qualification evidence."
                ),
            )
        return DiagnoseCheck(
            check_id="runtime_composition",
            name="Runtime execution composition",
            status="ok",
            message="Runtime execution composition is valid.",
            details={
                "configured": True,
                "manifest_fingerprint": diagnostics["manifest_fingerprint"],
                "providers": diagnostics.get("providers", []),
                "profiles": diagnostics.get("profiles", []),
            },
        )

    def run(self) -> DiagnoseResult:
        checks = []
        for check in self.checks:
            try:
                checks.append(check())
            except Exception as exc:
                checks.append(
                    DiagnoseCheck(
                        check_id="diagnostic_exception",
                        name="Diagnostic Check",
                        status="error",
                        message=f"{type(exc).__name__}: {exc}",
                        remediation="Inspect the failing diagnostic check and local service configuration.",
                    )
                )
        return DiagnoseResult.from_checks(checks)

    def _check_dashscope_key(self) -> DiagnoseCheck:
        if self.env.get("DASHSCOPE_API_KEY"):
            return DiagnoseCheck(
                check_id="dashscope_api_key",
                name="DashScope API key",
                status="ok",
                message="DashScope API key is configured.",
                details={"provider": "dashscope", "configured": True},
            )
        return DiagnoseCheck(
            check_id="dashscope_api_key",
            name="DashScope API key",
            status="warning",
            message="DashScope API key is not configured.",
            details={"provider": "dashscope", "configured": False},
            remediation="Set DASHSCOPE_API_KEY before using the live LLM profile.",
        )

    def _check_source_config(self) -> DiagnoseCheck:
        configured = bool(self.env.get("NEWS_SOURCES_CONFIG"))
        path = Path(self.env.get("NEWS_SOURCES_CONFIG") or "configs/sources.yaml")
        try:
            registry = build_default_source_registry(source_config_path=path)
        except SourceConfigError as exc:
            return DiagnoseCheck(
                check_id="source_config",
                name="Live source config",
                status="error",
                message=str(exc),
                details={"path": str(path), "configured": configured},
                remediation="Fix NEWS_SOURCES_CONFIG or configs/sources.yaml before using the live profile.",
            )
        return DiagnoseCheck(
            check_id="source_config",
            name="Live source config",
            status="ok",
            message="Live source config is valid.",
            details={
                "path": str(path),
                "configured": configured,
                "source_count": len(registry.list_sources(enabled_only=False)),
            },
        )

    def _check_model_config(self) -> DiagnoseCheck:
        configured = bool(self.env.get("NEWS_MODELS_CONFIG"))
        path = Path(self.env.get("NEWS_MODELS_CONFIG") or DEFAULT_MODELS_CONFIG_PATH)
        try:
            with _llm_env_overrides(self.env):
                deployment = load_openai_compatible_deployment(
                    path,
                    route_id="daily-intelligence-writer",
                )
        except LLMConfigurationError as exc:
            return DiagnoseCheck(
                check_id="model_config",
                name="Live model config",
                status="error",
                message=str(exc),
                details={"path": str(path), "configured": configured},
                remediation="Fix NEWS_MODELS_CONFIG or configs/models.yaml before using the live profile.",
            )
        api_key_env = deployment.config.api_key_env
        capability_details = deployment.capabilities.to_dict()
        route_details = {
            "required_capabilities": list(deployment.required_capabilities),
            "fallback_deployment_ids": list(getattr(deployment, "fallback_deployment_ids", ()) or ()),
            "cooldown_seconds": getattr(deployment, "cooldown_seconds", None),
            "budget_policy": dict(getattr(deployment, "budget_policy", {}) or {}),
            "enabled": getattr(deployment, "enabled", True),
            "max_retries": deployment.max_retries,
        }
        if not self.env.get(api_key_env):
            return DiagnoseCheck(
                check_id="model_config",
                name="Live model config",
                status="warning",
                message=f"Live model config is valid, but {api_key_env} is not configured.",
                details={
                    "path": str(path),
                    "configured": configured,
                    "deployment_id": deployment.deployment_id,
                    "provider": deployment.config.provider,
                    "model": deployment.config.model,
                    "api_key_env": api_key_env,
                    **route_details,
                    "capabilities": capability_details,
                },
                remediation=f"Set {api_key_env} before using the live LLM profile.",
            )
        return DiagnoseCheck(
            check_id="model_config",
            name="Live model config",
            status="ok",
            message="Live model config is valid.",
            details={
                "path": str(path),
                "configured": configured,
                "deployment_id": deployment.deployment_id,
                "provider": deployment.config.provider,
                "model": deployment.config.model,
                "api_key_env": api_key_env,
                **route_details,
                "capabilities": capability_details,
            },
        )

    def _check_redis(self) -> DiagnoseCheck:
        import redis

        redis_url = self.env.get("NEWS_REDIS_URL", "redis://127.0.0.1:6379/0")
        client = redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        return DiagnoseCheck(
            check_id="redis",
            name="Redis",
            status="ok",
            message="Redis ping succeeded.",
            details={"configured": bool(self.env.get("NEWS_REDIS_URL"))},
        )

    def _check_qdrant(self) -> DiagnoseCheck:
        url = self.env.get("NEWS_QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
        with urllib.request.urlopen(f"{url}/healthz", timeout=2) as response:
            content = response.read().decode("utf-8", errors="replace")
        return DiagnoseCheck(
            check_id="qdrant",
            name="Qdrant",
            status="ok",
            message="Qdrant health check succeeded.",
            details={"configured": bool(self.env.get("NEWS_QDRANT_URL")), "response": content[:80]},
        )

    def _check_postgres(self) -> DiagnoseCheck:
        dsn = self.env.get("NEWS_DATABASE_DSN")
        if not dsn:
            return DiagnoseCheck(
                check_id="postgres",
                name="PostgreSQL",
                status="skipped",
                message="NEWS_DATABASE_DSN is not configured; local JSON fallback is active.",
                details={"configured": False},
            )

        import psycopg

        from infrastructure.storage.postgres.dsn import normalize_dsn

        with psycopg.connect(normalize_dsn(dsn), connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return DiagnoseCheck(
            check_id="postgres",
            name="PostgreSQL",
            status="ok",
            message="PostgreSQL connection check succeeded.",
            details={"configured": True},
        )


@contextmanager
def _llm_env_overrides(env: Any) -> Iterator[None]:
    if env is os.environ:
        yield
        return
    snapshot = {key: os.environ.get(key) for key in _LLM_ENV_OVERRIDE_KEYS}
    try:
        for key in _LLM_ENV_OVERRIDE_KEYS:
            if key in env:
                os.environ[key] = str(env[key])
            else:
                os.environ.pop(key, None)
        yield
    finally:
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
