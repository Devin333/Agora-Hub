from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pytest

from infrastructure.execution_environment.docker import DockerExecutionEnvironment
from tests.infrastructure.harness.test_child_process_recovery import (
    _await,
    _finish,
    _start,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("NEWSROOM_DOCKER_INTEGRATION") != "1",
    reason="set NEWSROOM_DOCKER_INTEGRATION=1 for real Docker process recovery",
)


def _docker(*args):
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _running(container_name):
    result = _docker(
        "container", "inspect", "--format", "{{.State.Running}}", container_name
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def test_killed_controller_preserves_live_docker_effect_and_blocks_reexecution(
    tmp_path: Path,
):
    image = _docker(
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        os.environ.get("NEWSROOM_DOCKER_TEST_IMAGE", "redis:7-alpine"),
    )
    assert image.returncode == 0 and image.stdout.strip().startswith("sha256:")
    provider = DockerExecutionEnvironment()
    assert provider.capabilities.available
    execution_id = f"process-recovery-{uuid4().hex}"
    container_name = provider._container_name(execution_id)
    effect_root = tmp_path / "container-effects"
    effect_root.mkdir()
    effects = effect_root / "effects"
    release = tmp_path / "release-controller"
    options = {
        "image": image.stdout.strip(),
        "execution_id": execution_id,
        "effect_root": str(effect_root),
        "release_path": str(release),
    }
    first, _, signal = _start(
        tmp_path, "docker_effect", owner="docker-owner", options=options
    )
    try:
        assert _await(signal)["spawned"] == "independent-child"
        deadline = time.monotonic() + 15
        while not effects.exists() and time.monotonic() < deadline:
            assert first.poll() is None, "controller exited before the external effect"
            time.sleep(0.02)
        assert effects.read_text(encoding="utf-8").splitlines() == ["effect"]
        assert _running(container_name)
        _finish(first, terminate=True)
        # Docker is detached: killing the Python owner does not terminate it.
        assert _running(container_name)
        outcome_path = tmp_path / "docker-recovered.json"
        recovered, _, signal = _start(
            tmp_path,
            "recover_docker",
            owner="docker-recovered",
            options={**options, "clock_offset": 60, "outcome_path": str(outcome_path)},
        )
        try:
            assert _await(signal)["recovered"] == "LOST"
            assert recovered.wait(timeout=15) == 0
        finally:
            _finish(recovered, terminate=True)
        result = json.loads(outcome_path.read_text(encoding="utf-8"))
        assert result["receipt"]["termination_confirmed"] is False
        assert result["receipt"]["reason_code"] == "termination_unconfirmed"
        assert result["result"] is None
        assert _running(container_name)
        assert effects.read_text(encoding="utf-8").splitlines() == ["effect"]
        replacement, _, signal = _start(
            tmp_path,
            "takeover",
            owner="docker-replacement",
            options={"clock_offset": 61},
        )
        try:
            assert _await(signal)["code"] == "child_capacity_exhausted"
            assert replacement.wait(timeout=15) == 0
        finally:
            _finish(replacement, terminate=True)
        assert effects.read_text(encoding="utf-8").splitlines() == ["effect"]
    finally:
        _finish(first, terminate=True)
        # Restrict cleanup to the exact unique container created by this test.
        removed = _docker("rm", "-f", "-v", container_name)
        assert removed.returncode == 0 or "No such container" in removed.stderr
        assert _docker("container", "inspect", container_name).returncode != 0
