from __future__ import annotations

import pytest

from interfaces.services.research_desktop_auth import (
    ResearchDesktopAuthError,
    desktop_canonical_origin,
    desktop_legacy_operation_path,
    local_desktop_profile_enabled,
)


def test_desktop_profile_is_strict_and_conflicting_origins_fail_closed() -> None:
    assert local_desktop_profile_enabled({"NEWSROOM_DEPLOYMENT_PROFILE": "local-desktop"}) is True
    assert local_desktop_profile_enabled({"NEWSROOM_DEPLOYMENT_PROFILE": "default"}) is False
    with pytest.raises(ResearchDesktopAuthError) as invalid:
        local_desktop_profile_enabled({"NEWSROOM_DEPLOYMENT_PROFILE": "desktop"})
    assert invalid.value.code == "deployment_profile_invalid"
    with pytest.raises(ResearchDesktopAuthError) as conflict:
        desktop_canonical_origin({
            "NEWSROOM_DESKTOP_CANONICAL_ORIGIN": "http://127.0.0.1:3010",
            "NEWSROOM_CANONICAL_ORIGIN": "http://127.0.0.1:3000",
        })
    assert conflict.value.code == "desktop_origin_conflict"


def test_local_desktop_disables_legacy_mutations_but_keeps_reads() -> None:
    assert desktop_legacy_operation_path("/api/v1/research/papers/analyze", "POST") is True
    assert desktop_legacy_operation_path("/api/v1/research/papers/paper-1/ask", "POST") is True
    assert desktop_legacy_operation_path("/api/v1/research/runs/run-1/trace", "GET") is False
    assert desktop_legacy_operation_path("/api/v1/research/desktop/operations/analyze", "POST") is False
