from __future__ import annotations

from framework.harness.rag.evidence_typing import MetadataKeyEvidenceTypeResolver


def test_metadata_key_resolver_returns_first_mapped_scalar_value() -> None:
    resolver = MetadataKeyEvidenceTypeResolver({
        "section_role": {"method": "method"},
        "chunk_type": {"table": "experiment"},
    })

    assert resolver.resolve({"section_role": "method", "chunk_type": "table"}) == "method"


def test_metadata_key_resolver_returns_first_mapped_list_value() -> None:
    resolver = MetadataKeyEvidenceTypeResolver({
        "section_role": {"experiment": "experiment", "method": "method"},
    })

    assert resolver.resolve({"section_role": ["unknown", "experiment", "method"]}) == "experiment"


def test_metadata_key_resolver_uses_mapping_key_order_before_later_keys() -> None:
    resolver = MetadataKeyEvidenceTypeResolver({
        "section_role": {"method": "method"},
        "chunk_type": {"table": "experiment"},
    })

    assert resolver.resolve({"section_role": ["method"], "chunk_type": "table"}) == "method"


def test_metadata_key_resolver_returns_none_when_no_content_signal_matches() -> None:
    resolver = MetadataKeyEvidenceTypeResolver({
        "section_role": {"method": "method"},
        "chunk_type": {"table": "experiment"},
    })

    assert resolver.resolve({"section_role": ["background"], "chunk_type": "paragraph"}) is None


def test_metadata_key_resolver_returns_every_mapped_type_in_order() -> None:
    resolver = MetadataKeyEvidenceTypeResolver({
        "chunk_type": {"table": "experiment"},
        "section_role": {"method": "method", "experiment": "experiment"},
    })

    assert resolver.resolve_types({"chunk_type": "table", "section_role": ["method", "experiment"]}) == (
        "experiment",
        "method",
    )
    assert resolver.resolve_types({"chunk_type": "paragraph"}) == ()
