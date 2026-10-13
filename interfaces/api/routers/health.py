from __future__ import annotations

from fastapi import APIRouter

from framework.execution_environment.errors import ExecutionEnvironmentError
from interfaces.api.deps import ApiRouteHelpers, ApiServices
from interfaces.services.research_desktop_auth import (
    ResearchDesktopAuthError,
    local_desktop_runtime_identity,
)


def create_router(services: ApiServices, helpers: ApiRouteHelpers) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict:
        try:
            desktop_identity = local_desktop_runtime_identity()
        except ResearchDesktopAuthError as exc:
            return helpers.error(status_code=exc.status_code, code=exc.code, message=str(exc))
        payload = {"status": "ok", "service": "agora-hub-api"}
        if desktop_identity is not None:
            payload["local_desktop"] = desktop_identity
        return helpers.success(payload)

    @router.get("/health/live")
    def live_health() -> dict:
        return helpers.success({"status": "ok", "service": "agora-hub-api", "live": True})

    @router.get("/health/ready")
    def ready_health() -> dict:
        composition = services.runtime_execution_composition
        if composition is not None:
            try:
                diagnostics = composition.diagnostics()
                if diagnostics.get("status") != "ready":
                    composition.require_ready()
            except Exception as exc:
                reason_code = getattr(exc, "reason_code", "runtime_composition_unavailable")
                details = getattr(exc, "details", {})
                return helpers.error(
                    status_code=503,
                    code=str(reason_code),
                    message="runtime execution composition is not ready",
                    details=dict(details),
                    retryable=isinstance(exc, ExecutionEnvironmentError),
                )
            try:
                desktop_identity = local_desktop_runtime_identity()
            except ResearchDesktopAuthError as exc:
                return helpers.error(status_code=exc.status_code, code=exc.code, message=str(exc))
            payload = {
                "status": "ok",
                "service": "agora-hub-api",
                "ready": True,
                "runtime_composition": diagnostics,
            }
            if desktop_identity is not None:
                payload["local_desktop"] = desktop_identity
            return helpers.success(payload)
        return helpers.error(
            status_code=503,
            code="runtime_composition_missing",
            message="runtime execution composition is not configured",
            details={"configured": False},
            retryable=False,
        )

    @router.get("/health/dependencies")
    def dependency_health() -> dict:
        return helpers.success(services.diagnostic_service_factory().run().to_dict())

    @router.get("/api/v1/admin/diagnose")
    def diagnose():
        return helpers.success(services.diagnostic_service_factory().run().to_dict())

    return router
