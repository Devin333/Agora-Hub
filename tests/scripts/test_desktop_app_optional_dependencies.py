from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _run_without_pillow(body: str) -> subprocess.CompletedProcess[str]:
    script = f"""
import importlib
import importlib.abc
import sys


class BlockPillow(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "PIL" or fullname.startswith("PIL."):
            raise ModuleNotFoundError(
                "Pillow import blocked by desktop optional-dependency test",
                name=fullname,
            )
        return None


sys.meta_path.insert(0, BlockPillow())
sys.path.insert(0, {json.dumps(str(_REPOSITORY_ROOT))})

{body}
"""
    return subprocess.run(
        [sys.executable, "-B", "-I", "-c", script],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_desktop_api_import_does_not_require_pillow() -> None:
    result = _run_without_pillow(
        """
module = importlib.import_module("interfaces.api.app")
assert module.app is not None
assert not any(
    name == "PIL" or name.startswith("PIL.") for name in sys.modules
)
"""
    )

    assert result.returncode == 0, result.stderr


def test_marker_raster_use_fails_explicitly_without_pillow() -> None:
    result = _run_without_pillow(
        """
import tempfile
from pathlib import Path

marker_assets = importlib.import_module("backend.research.document.marker_assets")
payload = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
    "x8AAusB9Wl2n7sAAAAASUVORK5CYII="
)
with tempfile.TemporaryDirectory() as temporary:
    artifact_root = Path(temporary) / "artifacts"
    try:
        marker_assets.write_embedded_marker_image(
            payload,
            target_dir=artifact_root / "paper" / "figures",
            artifact_root=artifact_root,
        )
    except ModuleNotFoundError as exc:
        assert exc.name == "PIL"
        assert str(exc) == "Pillow import blocked by desktop optional-dependency test"
        assert exc.__cause__ is None
    else:
        raise AssertionError("Marker raster validation must fail when Pillow is absent")
    assert not artifact_root.exists()
"""
    )

    assert result.returncode == 0, result.stderr
