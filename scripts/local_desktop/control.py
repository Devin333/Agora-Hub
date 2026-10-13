from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from framework.shared.local_desktop import LocalDesktopConfig, validate_lifecycle_timeout
from scripts.local_desktop.common import DesktopOperationError


class DrainController(Protocol):
    def begin_drain(self, *, timeout_seconds: float) -> dict[str, Any]: ...
    def resume_admission(self) -> dict[str, Any]: ...


@dataclass
class ApplicationControlPlane:
    """Adapter over the application-owned request generation and drain controls."""

    config: LocalDesktopConfig
    drain_controller: DrainController | None = None

    def _request_store(self):
        try:
            from interfaces.services.research_desktop_control import LocalJsonResearchDesktopRequestStore
        except ImportError as exc:
            raise DesktopOperationError("desktop_control_service_unavailable", "research desktop request control store is unavailable") from exc
        return LocalJsonResearchDesktopRequestStore(self.config.control_path)

    def status(self) -> dict[str, Any]:
        store = self._request_store()
        return {"generation": store.generation(), "admission_open": store.admission_open()}

    def _controller(self) -> DrainController:
        if self.drain_controller is None:
            try:
                from interfaces.composition.local_desktop_control import build_local_desktop_drain_controller
            except ImportError as exc:
                raise DesktopOperationError(
                    "desktop_drain_control_unavailable",
                    "application admission drain control is unavailable; owned processes were not stopped",
                    exit_code=4,
                ) from exc
            self.drain_controller = build_local_desktop_drain_controller(control_path=self.config.control_path)
        return self.drain_controller

    def begin_drain(self, *, timeout_seconds: float) -> dict[str, Any]:
        timeout_seconds = validate_lifecycle_timeout(timeout_seconds, field_name="drain_timeout")
        result = dict(self._controller().begin_drain(timeout_seconds=timeout_seconds))
        if result.get("status") not in {"drained", "already_drained"}:
            raise DesktopOperationError(
                "desktop_drain_busy",
                "active business operations did not drain before the deadline",
                exit_code=4,
                details={"status": result.get("status"), "active_count": result.get("active_count")},
            )
        return result

    def resume_admission(self) -> dict[str, Any]:
        return dict(self._controller().resume_admission())

    def rotate_generation(self, *, reason: str) -> dict[str, Any]:
        generation = self._request_store().rotate_generation()
        return {"generation": generation, "reason": reason}


__all__ = ["ApplicationControlPlane", "DrainController"]
