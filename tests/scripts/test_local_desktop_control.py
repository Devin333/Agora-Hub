from __future__ import annotations

from pathlib import Path

import pytest

from framework.shared.local_desktop import resolve_local_desktop_config
from scripts.local_desktop.control import ApplicationControlPlane


class Controller:
    def __init__(self) -> None:
        self.resumed = 0

    def begin_drain(self, *, timeout_seconds: float):
        return {"status": "drained", "active_count": 0}

    def resume_admission(self):
        self.resumed += 1
        return {"status": "accepting", "active_count": 0}


def test_new_control_plane_rebuilds_controller_before_resuming_admission(tmp_path: Path, monkeypatch) -> None:
    config = resolve_local_desktop_config(project_root=tmp_path)
    controller = Controller()
    calls: list[Path] = []

    def build(*, control_path):
        calls.append(Path(control_path))
        return controller

    monkeypatch.setattr("interfaces.composition.local_desktop_control.build_local_desktop_drain_controller", build)
    plane = ApplicationControlPlane(config)

    result = plane.resume_admission()

    assert result["status"] == "accepting"
    assert controller.resumed == 1
    assert calls == [config.control_path]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 301])
def test_control_plane_rejects_invalid_drain_timeout_before_building_controller(tmp_path: Path, monkeypatch, value: float) -> None:
    config = resolve_local_desktop_config(project_root=tmp_path)
    monkeypatch.setattr(
        "interfaces.composition.local_desktop_control.build_local_desktop_drain_controller",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("timeout must fail first")),
    )

    with pytest.raises(ValueError, match="finite number"):
        ApplicationControlPlane(config).begin_drain(timeout_seconds=value)
