from __future__ import annotations

from pathlib import Path

from interfaces.services.research_desktop_control import LocalJsonResearchDesktopRequestStore


def build_local_desktop_drain_controller(
    *,
    control_path: str | Path,
) -> LocalJsonResearchDesktopRequestStore:
    """Return the application-owned controller for admission drain and fencing."""

    return LocalJsonResearchDesktopRequestStore(control_path)


__all__ = ["build_local_desktop_drain_controller"]
